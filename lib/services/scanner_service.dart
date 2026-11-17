import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';
import 'package:crypto/crypto.dart';
import 'package:image/image.dart' as img;
import 'package:path/path.dart' as p;
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

import '../data/tag_dictionary.dart';
import '../models/models.dart';
import 'ai_tagger_service.dart';
import 'database_service.dart';

class ScannerService {
  static final ScannerService instance = ScannerService._();
  ScannerService._();

  static const Set<String> imageExtensions = {
    '.jpg',
    '.jpeg',
    '.png',
    '.webp',
    '.bmp',
  };

  static const Set<String> videoExtensions = {
    '.mp4',
    '.mkv',
    '.webm',
    '.mov',
    '.avi',
    '.m4v',
    '.wmv',
  };

  static final Set<String> allMediaExtensions = {
    ...imageExtensions,
    ...videoExtensions,
  };

  bool _cancelRequested = false;

  void requestCancel() {
    _cancelRequested = true;
  }

  /// Detects Brawl Stars character tags and filename tokens (100% parity with kokekbooru.py).
  Map<String, (String, double)> detectFilenameTags(File file, {required bool allowNsfw}) {
    final found = <String, (String, double)>{};
    final parentName = p.basename(file.parent.path);
    final stem = p.basenameWithoutExtension(file.path);
    final haystack = '$parentName $stem'.toLowerCase();
    final tokens = haystack.split(RegExp(r'[^a-z0-9\-]+')).where((s) => s.isNotEmpty).toSet();

    for (final ch in kBrawlStarsChars) {
      if (tokens.contains(ch)) {
        found['char:$ch'] = ('filename', 1.0);
      }
    }
    if (allowNsfw &&
        (tokens.contains('femboy') ||
            tokens.contains('otoko_no_ko') ||
            haystack.contains('фембой'))) {
      found['nsfw:femboy'] = ('filename', 1.0);
    }
    return found;
  }

  /// Computes MD5 and 64-bit perceptual difference hash (dHash) for duplicate detection.
  Future<(String md5Hex, String dhashHex, int fileSize)> computeImageHashes(
    File file,
    img.Image decoded,
  ) async {
    final stat = await file.stat();
    final fSize = stat.size;
    String md5Hex = '';

    if (fSize <= 8 * 1024 * 1024) {
      final bytes = await file.readAsBytes();
      md5Hex = md5.convert(bytes).toString();
    } else {
      final raf = await file.open();
      try {
        const chunkSz = 262144;
        final b1 = await raf.read(chunkSz);
        await raf.setPosition(fSize ~/ 2);
        final b2 = await raf.read(chunkSz);
        await raf.setPosition(math.max(0, fSize - chunkSz));
        final b3 = await raf.read(chunkSz);
        final builder = BytesBuilder()
          ..add(utf8.encode(fSize.toString()))
          ..add(b1)
          ..add(b2)
          ..add(b3);
        md5Hex = md5.convert(builder.toBytes()).toString();
      } finally {
        await raf.close();
      }
    }

    // 9x8 grayscale dHash
    final small = img.copyResize(
      img.grayscale(decoded),
      width: 9,
      height: 8,
      interpolation: img.Interpolation.linear,
    );
    BigInt bits = BigInt.zero;
    for (int y = 0; y < 8; y++) {
      for (int x = 0; x < 8; x++) {
        final leftLum = small.getPixel(x, y).r;
        final rightLum = small.getPixel(x + 1, y).r;
        bits = (bits << 1) | (rightLum > leftLum ? BigInt.one : BigInt.zero);
      }
    }
    final dhashHex = bits.toRadixString(16).padLeft(16, '0');
    return (md5Hex, dhashHex, fSize);
  }

  Future<(String md5Hex, int fileSize)> computeFastVideoHash(File file) async {
    final stat = await file.stat();
    final fSize = stat.size;
    const chunkSz = 65536;
    final raf = await file.open();
    try {
      final b1 = await raf.read(chunkSz);
      Uint8List b2 = Uint8List(0);
      if (fSize > chunkSz * 2) {
        await raf.setPosition(math.max(0, fSize - chunkSz));
        b2 = await raf.read(chunkSz);
      }
      final builder = BytesBuilder()
        ..add(utf8.encode('$fSize:${stat.modified.millisecondsSinceEpoch ~/ 1000}'))
        ..add(b1)
        ..add(b2);
      return (md5.convert(builder.toBytes()).toString(), fSize);
    } finally {
      await raf.close();
    }
  }

  /// Extracts dominant color hex and 5-color palette from a decoded image.
  (String dominantHex, List<String> palette) extractPalette(img.Image decoded) {
    final small = img.copyResize(decoded, width: 32, height: 32);
    final buckets = <String, int>{};
    for (int y = 0; y < small.height; y++) {
      for (int x = 0; x < small.width; x++) {
        final px = small.getPixel(x, y);
        final r = (px.r.toInt() ~/ 24) * 24;
        final g = (px.g.toInt() ~/ 24) * 24;
        final b = (px.b.toInt() ~/ 24) * 24;
        final hex =
            '#${r.toRadixString(16).padLeft(2, '0')}${g.toRadixString(16).padLeft(2, '0')}${b.toRadixString(16).padLeft(2, '0')}';
        buckets[hex] = (buckets[hex] ?? 0) + 1;
      }
    }
    final sorted = buckets.entries.toList()..sort((a, b) => b.value.compareTo(a.value));
    final pal = sorted.take(5).map((e) => e.key).toList();
    while (pal.length < 5) {
      pal.add('#2a2d35');
    }
    return (pal.first, pal);
  }

  File getThumbnailFileForPath(String filePath) {
    final hash = md5.convert(utf8.encode(filePath)).toString();
    return File(p.join(DatabaseService.instance.thumbsDir.path, '$hash.jpg'));
  }

  /// Scans an entire directory on disk (Desktop or Android) with ETA, FPS, and batching.
  Future<void> scanFolder({
    required String folderPath,
    required String category,
    required bool forceRescan,
    required HubSettings settings,
    required void Function(ScanProgressState) onProgress,
  }) async {
    _cancelRequested = false;
    final rootDir = Directory(folderPath);
    if (!await rootDir.exists()) {
      onProgress(ScanProgressState(
        running: false,
        message: 'Папка не найдена: $folderPath',
      ));
      return;
    }

    final db = DatabaseService.instance.db;
    final nowSec = DateTime.now().millisecondsSinceEpoch / 1000.0;
    await db.insert(
      'folders',
      {
        'path': rootDir.path,
        'category': category.isEmpty ? 'general' : category,
        'label': p.basename(rootDir.path),
        'added_at': nowSec,
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );

    final allowedExts = settings.disableVideos ? imageExtensions : allMediaExtensions;
    final ignoredPaths = await DatabaseService.instance.getIgnoredPaths();

    final foundFiles = <File>[];
    await for (final entity in rootDir.list(recursive: true, followLinks: false)) {
      if (entity is File) {
        if (entity.path.contains('.kokekbooru')) continue;
        final ext = p.extension(entity.path).toLowerCase();
        if (allowedExts.contains(ext)) {
          // Skip files hidden via "Remove from app interface only" unless user explicitly forced a full rescan
          if (!forceRescan && ignoredPaths.contains(entity.path)) {
            continue;
          }
          foundFiles.add(entity);
        }
      }
    }

    await processFileList(
      files: foundFiles,
      rootFolderPath: rootDir.path,
      category: category,
      forceRescan: forceRescan,
      settings: settings,
      onProgress: onProgress,
    );
  }

  /// Processes either a scanned folder's file list OR individual files picked from Android Gallery (`🖼 Из галереи`).
  Future<void> processFileList({
    required List<File> files,
    required String rootFolderPath,
    required String category,
    required bool forceRescan,
    required HubSettings settings,
    required void Function(ScanProgressState) onProgress,
  }) async {
    _cancelRequested = false;
    final db = DatabaseService.instance.db;
    final total = files.length;

    onProgress(ScanProgressState(
      running: true,
      folder: rootFolderPath,
      total: total,
      current: 0,
      stage: 'Подготовка к сканированию...',
    ));

    final aiReady = await AiTaggerService.instance.ensureLoaded(settings);
    final (imgBatchSize, vidBatchSize) =
        await AiTaggerService.instance.getOptimalBatchSizes(settings);

    final existingRows = await db.query('images', columns: ['id', 'path']);
    final existingPaths = <String, int>{
      for (final r in existingRows) (r['path'] as String): (r['id'] as num).toInt(),
    };

    final aiTaggedRows =
        await db.rawQuery("SELECT DISTINCT image_id FROM image_tags WHERE source = 'wd_ai'");
    final aiTaggedIds = <int>{
      for (final r in aiTaggedRows) (r['image_id'] as num).toInt(),
    };

    if (forceRescan) {
      await db.delete('image_tags', where: "source != 'user'");
    }

    final toProcess = <File>[];
    int skippedExisting = 0;

    for (final f in files) {
      final existingId = existingPaths[f.path];
      final needsAiBackfill =
          existingId != null && aiReady && !aiTaggedIds.contains(existingId);
      if (!forceRescan && existingId != null && !needsAiBackfill) {
        skippedExisting++;
      } else {
        toProcess.add(f);
      }
    }

    final startTimeMs = DateTime.now().millisecondsSinceEpoch;
    int processedNew = 0;
    int newAdded = 0;

    final batchStep = math.max(1, imgBatchSize);
    for (int i = 0; i < toProcess.length; i += batchStep) {
      if (_cancelRequested) {
        onProgress(ScanProgressState(
          running: false,
          cancelling: false,
          folder: rootFolderPath,
          total: total,
          current: skippedExisting + processedNew,
          newAdded: newAdded,
          skippedExisting: skippedExisting,
          message: '🛑 Сканирование остановлено (+$newAdded)',
        ));
        return;
      }

      final slice = toProcess.skip(i).take(batchStep).toList();
      for (final file in slice) {
        if (_cancelRequested) break;
        final ext = p.extension(file.path).toLowerCase();
        final isVid = videoExtensions.contains(ext);

        final elapsedSec =
            math.max(0.05, (DateTime.now().millisecondsSinceEpoch - startTimeMs) / 1000.0);
        final fps = processedNew / elapsedSec;
        final remaining = math.max(0, toProcess.length - processedNew);
        final etaSec = fps > 0.05 ? (remaining / fps).ceil() : 0;
        final etaMin = (etaSec / 60.0).ceil();

        onProgress(ScanProgressState(
          running: true,
          cancelling: _cancelRequested,
          folder: rootFolderPath,
          total: total,
          current: skippedExisting + processedNew,
          newAdded: newAdded,
          skippedExisting: skippedExisting,
          currentFile: p.basename(file.path),
          stage: isVid
              ? '🎬 Анализ видео (${vidBatchSize}x батч)...'
              : '⚡ ИИ-индексация (${imgBatchSize}x батч)...',
          etaSeconds: etaSec,
          etaMinutes: etaMin,
          speedFps: double.parse(fps.toStringAsFixed(1)),
        ));

        try {
          if (isVid) {
            await _indexSingleVideo(
              file: file,
              rootFolderPath: rootFolderPath,
              category: category,
              settings: settings,
              existingPaths: existingPaths,
            );
          } else {
            await _indexSingleImage(
              file: file,
              rootFolderPath: rootFolderPath,
              category: category,
              settings: settings,
              aiReady: aiReady,
              existingPaths: existingPaths,
            );
          }
          newAdded++;
        } catch (_) {}
        processedNew++;
      }
    }

    onProgress(ScanProgressState(
      running: false,
      folder: rootFolderPath,
      total: total,
      current: total,
      newAdded: newAdded,
      skippedExisting: skippedExisting,
      stage: 'Готово!',
      message: '✅ Обработано файлов: +$newAdded (пропущено: $skippedExisting)',
    ));
  }

  Future<void> _indexSingleImage({
    required File file,
    required String rootFolderPath,
    required String category,
    required HubSettings settings,
    required bool aiReady,
    required Map<String, int> existingPaths,
  }) async {
    final bytes = await file.readAsBytes();
    final decoded = img.decodeImage(bytes);
    if (decoded == null) return;

    final width = decoded.width;
    final height = decoded.height;
    final aspectCat = width > height * 1.15
        ? 'landscape'
        : (height > width * 1.15 ? 'portrait' : 'square');

    final (fileMd5, dhash, fileSize) = await computeImageHashes(file, decoded);
    final (domHex, palette) = extractPalette(decoded);

    // Generate 420px thumbnail if not exists
    final thumbFile = getThumbnailFileForPath(file.path);
    if (!await thumbFile.exists()) {
      final thumbImg = img.copyResize(
        decoded,
        width: width >= height ? 420 : null,
        height: height > width ? 420 : null,
        interpolation: img.Interpolation.linear,
      );
      await thumbFile.writeAsBytes(img.encodeJpg(thumbImg, quality: 85));
    }

    final tags = detectFilenameTags(file, allowNsfw: settings.nsfwEnabled);
    if (aiReady && settings.aiEnabled) {
      final tensor = AiTaggerService.instance.imageToOrtTensor(decoded);
      final preds = await AiTaggerService.instance.runInferenceOnTensor(tensor);
      if (preds != null) {
        tags.addAll(AiTaggerService.instance.mapPredictionsToTags(preds, settings));
      }
    }

    await _saveMetaToDb(
      file: file,
      rootFolderPath: rootFolderPath,
      category: category,
      artType: 'image',
      fileMd5: fileMd5,
      dhash: dhash,
      fileSize: fileSize,
      width: width,
      height: height,
      aspectCat: aspectCat,
      dominantHex: domHex,
      palette: palette,
      tags: tags,
      existingPaths: existingPaths,
    );
  }

  Future<void> _indexSingleVideo({
    required File file,
    required String rootFolderPath,
    required String category,
    required HubSettings settings,
    required Map<String, int> existingPaths,
  }) async {
    final (fileMd5, fileSize) = await computeFastVideoHash(file);
    final (videoTags, thumbBytes) =
        await AiTaggerService.instance.analyzeVideoFrames(file, settings);

    final tags = <String, (String, double)>{
      ...detectFilenameTags(file, allowNsfw: settings.nsfwEnabled),
      ...videoTags,
    };

    final thumbFile = getThumbnailFileForPath(file.path);
    if (!await thumbFile.exists() && thumbBytes != null && thumbBytes.isNotEmpty) {
      await thumbFile.writeAsBytes(thumbBytes);
    }

    await _saveMetaToDb(
      file: file,
      rootFolderPath: rootFolderPath,
      category: category,
      artType: 'video',
      fileMd5: fileMd5,
      dhash: '',
      fileSize: fileSize,
      width: 1920,
      height: 1080,
      aspectCat: 'landscape',
      dominantHex: '#1e2230',
      palette: const ['#1e2230', '#7c5cff', '#2a2d35', '#101217', '#38bdf8'],
      tags: tags,
      existingPaths: existingPaths,
    );
  }

  Future<void> _saveMetaToDb({
    required File file,
    required String rootFolderPath,
    required String category,
    required String artType,
    required String fileMd5,
    required String dhash,
    required int fileSize,
    required int width,
    required int height,
    required String aspectCat,
    required String dominantHex,
    required List<String> palette,
    required Map<String, (String, double)> tags,
    required Map<String, int> existingPaths,
  }) async {
    final db = DatabaseService.instance.db;
    final fStr = file.path;
    final subName = file.parent.path != rootFolderPath
        ? p.basename(file.parent.path)
        : p.basename(rootFolderPath);
    final effCat = (category.isNotEmpty && category != 'all') ? category : 'general';
    final stat = await file.stat();
    final mtime = stat.modified.millisecondsSinceEpoch / 1000.0;
    final nowSec = DateTime.now().millisecondsSinceEpoch / 1000.0;

    // Check if backup_item_state has saved flags/tags for this file
    Map<String, Object?>? backupRow;
    if (fileMd5.isNotEmpty) {
      final bRows = await db.query(
        'backup_item_state',
        where: 'file_md5 = ? OR filename = ?',
        whereArgs: [fileMd5, p.basename(fStr)],
        limit: 1,
      );
      if (bRows.isNotEmpty) backupRow = bRows.first;
    }

    // Remove from ignored_paths if explicitly imported/rescanned
    await DatabaseService.instance.removeIgnoredPath(fStr);

    int imgId;
    if (existingPaths.containsKey(fStr)) {
      imgId = existingPaths[fStr]!;
      await db.update(
        'images',
        {
          'subfolder_name': subName,
          'file_md5': fileMd5,
          'dhash': dhash,
          'file_size': fileSize,
          'art_type': artType,
          'width': width,
          'height': height,
          'aspect_cat': aspectCat,
          'dominant_hex': dominantHex,
          'palette_json': jsonEncode(palette),
          'mtime': mtime,
          'indexed_at': nowSec,
        },
        where: 'id = ?',
        whereArgs: [imgId],
      );
      await db.delete('image_tags', where: "image_id = ? AND source != 'user'", whereArgs: [imgId]);
    } else {
      final initFav = backupRow != null ? ((backupRow['is_favorite'] as num?) ?? 0).toInt() : 0;
      final initCat = backupRow != null
          ? ((backupRow['folder_category'] as String?) ?? effCat)
          : effCat;
      final initFlip = backupRow != null ? ((backupRow['flip_h'] as num?) ?? 0).toInt() : 0;
      final initGray = backupRow != null ? ((backupRow['grayscale'] as num?) ?? 0).toInt() : 0;
      final initNotes = backupRow != null ? ((backupRow['notes'] as String?) ?? '') : '';

      imgId = await db.insert('images', {
        'path': fStr,
        'filename': p.basename(fStr),
        'folder_path': rootFolderPath,
        'subfolder_name': subName,
        'folder_category': initCat,
        'file_md5': fileMd5,
        'dhash': dhash,
        'file_size': fileSize,
        'art_type': artType,
        'is_favorite': initFav,
        'width': width,
        'height': height,
        'aspect_cat': aspectCat,
        'dominant_hex': dominantHex,
        'palette_json': jsonEncode(palette),
        'pose_ready': 0,
        'pose_confidence': 0.0,
        'flip_h': initFlip,
        'grayscale': initGray,
        'feature_vec': '[]',
        'notes': initNotes,
        'mtime': mtime,
        'indexed_at': nowSec,
      });
      existingPaths[fStr] = imgId;

      if (backupRow != null && backupRow['tags_json'] != null) {
        try {
          final savedTags = jsonDecode(backupRow['tags_json'] as String) as List;
          for (final st in savedTags) {
            if (st is Map && st['tag'] != null) {
              await db.insert(
                'image_tags',
                {
                  'image_id': imgId,
                  'tag': st['tag'].toString(),
                  'source': (st['source'] ?? 'user').toString(),
                  'confidence': ((st['confidence'] as num?) ?? 1.0).toDouble(),
                },
                conflictAlgorithm: ConflictAlgorithm.ignore,
              );
            }
          }
        } catch (_) {}
      }
    }

    final batch = db.batch();
    tags.forEach((tagName, meta) {
      batch.insert(
        'image_tags',
        {
          'image_id': imgId,
          'tag': tagName,
          'source': meta.$1,
          'confidence': meta.$2,
        },
        conflictAlgorithm: ConflictAlgorithm.ignore,
      );
    });
    await batch.commit(noResult: true);
  }
}
