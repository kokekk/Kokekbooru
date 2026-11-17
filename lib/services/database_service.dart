import 'dart:convert';
import 'dart:io';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

import '../data/tag_dictionary.dart';
import '../models/models.dart';

class DatabaseService {
  static final DatabaseService instance = DatabaseService._();
  DatabaseService._();

  Database? _db;
  late Directory appDir;
  late Directory thumbsDir;
  late Directory modelsDir;
  late File settingsFile;

  bool _initialized = false;

  Future<void> init() async {
    if (_initialized) return;

    if (Platform.isWindows || Platform.isLinux || Platform.isMacOS) {
      sqfliteFfiInit();
      databaseFactory = databaseFactoryFfi;
      final home = Platform.environment['USERPROFILE'] ??
          Platform.environment['HOME'] ??
          Directory.current.path;
      appDir = Directory(p.join(home, '.kokekbooru'));
    } else {
      final supportDir = await getApplicationSupportDirectory();
      appDir = Directory(p.join(supportDir.path, 'kokekbooru'));
    }

    thumbsDir = Directory(p.join(appDir.path, 'thumbnails'));
    modelsDir = Directory(p.join(appDir.path, 'ai_models'));
    settingsFile = File(p.join(appDir.path, 'kokekbooru_settings.json'));

    for (final d in [appDir, thumbsDir, modelsDir]) {
      if (!await d.exists()) {
        await d.create(recursive: true);
      }
    }

    final dbPath = p.join(appDir.path, 'kokekbooru.db');
    _db = await databaseFactory.openDatabase(
      dbPath,
      options: OpenDatabaseOptions(
        version: 2,
        onCreate: (db, version) async {
          await _createTables(db);
        },
        onUpgrade: (db, oldVersion, newVersion) async {
          await _createTables(db);
        },
      ),
    );

    await _createTables(_db!);
    await _runMigrations(_db!);
    _initialized = true;
  }

  Database get db {
    if (_db == null) {
      throw StateError('DatabaseService not initialized');
    }
    return _db!;
  }

  Future<void> _createTables(Database db) async {
    await db.execute('''
      CREATE TABLE IF NOT EXISTS custom_categories (
        key TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        sort_order INTEGER DEFAULT 0
      )
    ''');

    await db.execute('''
      CREATE TABLE IF NOT EXISTS folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT UNIQUE NOT NULL,
        category TEXT DEFAULT 'general',
        label TEXT DEFAULT '',
        added_at REAL
      )
    ''');

    await db.execute('''
      CREATE TABLE IF NOT EXISTS images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT UNIQUE NOT NULL,
        filename TEXT NOT NULL,
        folder_path TEXT NOT NULL,
        subfolder_name TEXT DEFAULT '',
        folder_category TEXT DEFAULT 'general',
        file_md5 TEXT DEFAULT '',
        dhash TEXT DEFAULT '',
        file_size INTEGER DEFAULT 0,
        art_type TEXT DEFAULT 'image',
        is_favorite INTEGER DEFAULT 0,
        width INTEGER,
        height INTEGER,
        aspect_cat TEXT,
        dominant_hex TEXT,
        palette_json TEXT,
        pose_ready INTEGER DEFAULT 0,
        pose_confidence REAL DEFAULT 0.0,
        flip_h INTEGER DEFAULT 0,
        grayscale INTEGER DEFAULT 0,
        feature_vec TEXT,
        clip_vec TEXT,
        notes TEXT DEFAULT '',
        mtime REAL,
        indexed_at REAL
      )
    ''');

    await db.execute('''
      CREATE TABLE IF NOT EXISTS image_tags (
        image_id INTEGER NOT NULL,
        tag TEXT NOT NULL,
        source TEXT DEFAULT 'ai',
        confidence REAL DEFAULT 1.0,
        UNIQUE(image_id, tag)
      )
    ''');

    await db.execute('''
      CREATE TABLE IF NOT EXISTS tag_translations (
        tag_en TEXT PRIMARY KEY,
        tag_ru TEXT NOT NULL,
        updated_at REAL
      )
    ''');

    await db.execute('''
      CREATE TABLE IF NOT EXISTS image_translations (
        image_id INTEGER NOT NULL,
        target_lang TEXT NOT NULL,
        provider TEXT DEFAULT 'google',
        blocks_json TEXT DEFAULT '[]',
        full_text TEXT DEFAULT '',
        updated_at REAL,
        PRIMARY KEY (image_id, target_lang)
      )
    ''');

    await db.execute('''
      CREATE TABLE IF NOT EXISTS backup_item_state (
        file_md5 TEXT,
        filename TEXT,
        orig_path TEXT,
        is_favorite INTEGER DEFAULT 0,
        folder_category TEXT DEFAULT 'general',
        flip_h INTEGER DEFAULT 0,
        grayscale INTEGER DEFAULT 0,
        notes TEXT DEFAULT '',
        tags_json TEXT DEFAULT '[]',
        PRIMARY KEY (file_md5, filename)
      )
    ''');

    // Table of paths removed from UI only (so future scans skip them unless force-rescanned or restored)
    await db.execute('''
      CREATE TABLE IF NOT EXISTS ignored_paths (
        path TEXT PRIMARY KEY,
        file_md5 TEXT DEFAULT '',
        ignored_at REAL
      )
    ''');

    // Artist general notebook table
    await db.execute('''
      CREATE TABLE IF NOT EXISTS artist_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT DEFAULT '',
        updated_at REAL
      )
    ''');
  }

  Future<void> _runMigrations(Database db) async {
    await db.execute(
      "INSERT OR IGNORE INTO custom_categories (key, label, sort_order) VALUES ('general', '🗂️ Все файлы', 0)",
    );
    for (final oldKey in kLegacyPresetCategoryKeys) {
      await db.update('images', {'folder_category': 'general'},
          where: 'folder_category = ?', whereArgs: [oldKey]);
      await db.update('folders', {'category': 'general'},
          where: 'category = ?', whereArgs: [oldKey]);
      await db.delete('custom_categories', where: 'key = ?', whereArgs: [oldKey]);
    }

    // Ensure any 18+ tags in detail:* are migrated to nsfw:*
    final detailRows = await db.rawQuery(
      "SELECT DISTINCT tag FROM image_tags WHERE tag LIKE 'detail:%'",
    );
    for (final r in detailRows) {
      final dt = (r['tag'] as String?) ?? '';
      if (!dt.contains(':')) continue;
      final shortT = dt.split(':')[1];
      if (isWdTagNsfw(shortT)) {
        String newNsfw = kPublicNsfwTags[shortT] ?? 'nsfw:\$shortT';
        if (!newNsfw.startsWith('nsfw:')) {
          newNsfw = 'nsfw:\$shortT';
        }
        await db.rawUpdate(
          'UPDATE OR IGNORE image_tags SET tag = ? WHERE tag = ?',
          [newNsfw, dt],
        );
        await db.delete('image_tags', where: 'tag = ?', whereArgs: [dt]);
      }
    }
  }

  // ===================================================================
  // SETTINGS PERSISTENCE
  // ===================================================================

  Future<HubSettings> loadSettings() async {
    if (await settingsFile.exists()) {
      try {
        final raw = await settingsFile.readAsString();
        final map = jsonDecode(raw) as Map<String, dynamic>;
        return HubSettings.fromMap(map);
      } catch (_) {}
    }
    return const HubSettings();
  }

  Future<HubSettings> saveSettings(HubSettings settings) async {
    const encoder = JsonEncoder.withIndent('  ');
    await settingsFile.writeAsString(encoder.convert(settings.toMap()));
    return settings;
  }

  // ===================================================================
  // IGNORED PATHS & DELETION MODES
  // ===================================================================

  Future<Set<String>> getIgnoredPaths() async {
    final rows = await db.query('ignored_paths', columns: ['path']);
    return rows.map((r) => (r['path'] as String?) ?? '').where((s) => s.isNotEmpty).toSet();
  }

  Future<void> removeIgnoredPath(String path) async {
    await db.delete('ignored_paths', where: 'path = ?', whereArgs: [path]);
  }

  /// Deletes one or more media items according to [mode]:
  /// - [FileDeleteMode.uiOnly]: deletes from `images` & `image_tags` and inserts into `ignored_paths`
  ///   so future scans do not re-add the file, while keeping the file on disk untouched.
  /// - [FileDeleteMode.fullSystem]: deletes from DB AND deletes the physical file from disk.
  Future<int> deleteMediaItems(List<int> imageIds, FileDeleteMode mode) async {
    if (imageIds.isEmpty) return 0;
    int deletedCount = 0;
    final now = DateTime.now().millisecondsSinceEpoch / 1000.0;

    for (final id in imageIds) {
      final rows = await db.query('images', where: 'id = ?', whereArgs: [id]);
      if (rows.isEmpty) continue;
      final fpath = (rows.first['path'] as String?) ?? '';
      final fmd5 = (rows.first['file_md5'] as String?) ?? '';

      await db.delete('image_tags', where: 'image_id = ?', whereArgs: [id]);
      await db.delete('image_translations', where: 'image_id = ?', whereArgs: [id]);
      await db.delete('images', where: 'id = ?', whereArgs: [id]);

      if (mode == FileDeleteMode.uiOnly) {
        if (fpath.isNotEmpty) {
          await db.insert(
            'ignored_paths',
            {'path': fpath, 'file_md5': fmd5, 'ignored_at': now},
            conflictAlgorithm: ConflictAlgorithm.replace,
          );
        }
      } else {
        // Full physical deletion from disk/device
        if (fpath.isNotEmpty) {
          try {
            final f = File(fpath);
            if (await f.exists()) {
              await f.delete();
            }
          } catch (_) {}
          await db.delete('ignored_paths', where: 'path = ?', whereArgs: [fpath]);
        }
      }
      deletedCount++;
    }
    return deletedCount;
  }

  // ===================================================================
  // CATEGORIES & FOLDERS
  // ===================================================================

  Future<List<AppFolderCategory>> getCategoriesWithCounts(List<MediaItem> visibleItems) async {
    final rows = await db.query('custom_categories', orderBy: 'sort_order ASC, rowid ASC');
    final counts = <String, int>{};
    for (final item in visibleItems) {
      final k = item.folderCategory.isEmpty ? 'general' : item.folderCategory;
      counts[k] = (counts[k] ?? 0) + 1;
    }
    counts['general'] = visibleItems.length;

    return rows.map((r) {
      final k = (r['key'] as String?) ?? 'general';
      final lbl = (r['label'] as String?) ?? '🗂️ Все файлы';
      final ord = ((r['sort_order'] as num?) ?? 0).toInt();
      return AppFolderCategory(
        key: k,
        label: lbl,
        sortOrder: ord,
        count: counts[k] ?? 0,
      );
    }).toList();
  }

  Future<AppFolderCategory> createCategory(String rawLabel) async {
    String lbl = rawLabel.trim();
    String slug = lbl.toLowerCase().replaceAll(RegExp(r'[^a-z0-9_]'), '_').replaceAll(RegExp(r'^_+|_+$'), '');
    final suffix = DateTime.now().millisecondsSinceEpoch % 10000;
    slug = slug.isEmpty ? 'cat_$suffix' : '${slug}_$suffix';
    if (!lbl.startsWith('📁') && !lbl.startsWith('🗂')) {
      lbl = '📁 $lbl';
    }
    final maxRow = await db.rawQuery('SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_ord FROM custom_categories');
    final nextOrd = ((maxRow.first['next_ord'] as num?) ?? 1).toInt();
    await db.insert(
      'custom_categories',
      {'key': slug, 'label': lbl, 'sort_order': nextOrd},
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
    return AppFolderCategory(key: slug, label: lbl, sortOrder: nextOrd, count: 0);
  }

  Future<void> renameCategory(String key, String newLabel) async {
    if (key == 'general' || newLabel.trim().isEmpty) return;
    await db.update(
      'custom_categories',
      {'label': newLabel.trim()},
      where: 'key = ?',
      whereArgs: [key],
    );
  }

  Future<void> deleteCategory(String key) async {
    if (key == 'general') return;
    await db.update('images', {'folder_category': 'general'},
        where: 'folder_category = ?', whereArgs: [key]);
    await db.update('folders', {'category': 'general'},
        where: 'category = ?', whereArgs: [key]);
    await db.delete('custom_categories', where: 'key = ?', whereArgs: [key]);
  }

  Future<List<String>> getSavedFolderPaths() async {
    final rows = await db.query('folders', orderBy: 'id DESC');
    return rows.map((r) => (r['path'] as String?) ?? '').where((s) => s.isNotEmpty).toList();
  }

  // ===================================================================
  // TAGS, FLAGS & NOTES
  // ===================================================================

  Future<Map<String, String>> loadCachedTagTranslations() async {
    final rows = await db.query('tag_translations');
    final map = <String, String>{};
    for (final r in rows) {
      final k = (r['tag_en'] as String?) ?? '';
      final v = (r['tag_ru'] as String?) ?? '';
      if (k.isNotEmpty && v.isNotEmpty) {
        map[k] = v;
      }
    }
    return map;
  }

  Future<void> saveTagTranslation(String tagEn, String tagRu) async {
    await db.insert(
      'tag_translations',
      {
        'tag_en': tagEn.toLowerCase().trim(),
        'tag_ru': tagRu.trim(),
        'updated_at': DateTime.now().millisecondsSinceEpoch / 1000.0,
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  Future<void> updateMediaFlags(
    int imageId, {
    bool? flipH,
    bool? grayscale,
    bool? isFavorite,
    String? notes,
    String? folderCategory,
  }) async {
    final updates = <String, Object?>{};
    if (flipH != null) updates['flip_h'] = flipH ? 1 : 0;
    if (grayscale != null) updates['grayscale'] = grayscale ? 1 : 0;
    if (isFavorite != null) updates['is_favorite'] = isFavorite ? 1 : 0;
    if (notes != null) updates['notes'] = notes;
    if (folderCategory != null) updates['folder_category'] = folderCategory;
    if (updates.isEmpty) return;
    await db.update('images', updates, where: 'id = ?', whereArgs: [imageId]);
  }

  Future<String> addTagToImage(int imageId, String rawTag) async {
    String clean = rawTag.trim().toLowerCase().replaceAll(' ', '_');
    if (clean.isEmpty) return '';
    if (!clean.contains(':')) {
      clean = isWdTagNsfw(clean) ? 'nsfw:$clean' : 'detail:$clean';
    }
    await db.insert(
      'image_tags',
      {'image_id': imageId, 'tag': clean, 'source': 'user', 'confidence': 1.0},
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
    return clean;
  }

  Future<void> removeTagFromImage(int imageId, String tag) async {
    await db.delete(
      'image_tags',
      where: 'image_id = ? AND tag = ?',
      whereArgs: [imageId, tag],
    );
  }

  Future<void> bulkSetFavorite(List<int> imageIds, bool isFavorite) async {
    if (imageIds.isEmpty) return;
    final batch = db.batch();
    for (final id in imageIds) {
      batch.update('images', {'is_favorite': isFavorite ? 1 : 0}, where: 'id = ?', whereArgs: [id]);
    }
    await batch.commit(noResult: true);
  }

  Future<void> bulkMoveToCategory(List<int> imageIds, String folderCategory) async {
    if (imageIds.isEmpty) return;
    final batch = db.batch();
    for (final id in imageIds) {
      batch.update('images', {'folder_category': folderCategory}, where: 'id = ?', whereArgs: [id]);
    }
    await batch.commit(noResult: true);
  }

  Future<void> bulkAddTag(List<int> imageIds, String rawTag) async {
    if (imageIds.isEmpty) return;
    String clean = rawTag.trim().toLowerCase().replaceAll(' ', '_');
    if (clean.isEmpty) return;
    if (!clean.contains(':')) {
      clean = isWdTagNsfw(clean) ? 'nsfw:$clean' : 'detail:$clean';
    }
    final batch = db.batch();
    for (final id in imageIds) {
      batch.insert(
        'image_tags',
        {'image_id': id, 'tag': clean, 'source': 'user', 'confidence': 1.0},
        conflictAlgorithm: ConflictAlgorithm.replace,
      );
    }
    await batch.commit(noResult: true);
  }

  // ===================================================================
  // ARTIST NOTEBOOK
  // ===================================================================

  Future<List<ArtistNoteItem>> getArtistNotes() async {
    final rows = await db.query('artist_notes', orderBy: 'updated_at DESC');
    return rows.map(ArtistNoteItem.fromRow).toList();
  }

  Future<ArtistNoteItem> saveArtistNote({int? id, required String title, required String content}) async {
    final now = DateTime.now().millisecondsSinceEpoch / 1000.0;
    if (id == null) {
      final newId = await db.insert('artist_notes', {
        'title': title.trim().isEmpty ? 'Заметка' : title.trim(),
        'content': content,
        'updated_at': now,
      });
      return ArtistNoteItem(id: newId, title: title, content: content, updatedAt: now);
    } else {
      await db.update(
        'artist_notes',
        {'title': title, 'content': content, 'updated_at': now},
        where: 'id = ?',
        whereArgs: [id],
      );
      return ArtistNoteItem(id: id, title: title, content: content, updatedAt: now);
    }
  }

  Future<void> deleteArtistNote(int id) async {
    await db.delete('artist_notes', where: 'id = ?', whereArgs: [id]);
  }

  // ===================================================================
  // DUPLICATES & AUTO-CLEAN
  // ===================================================================

  int _qualityScore(Map<String, Object?> r) {
    final w = ((r['width'] as num?) ?? 0).toInt();
    final h = ((r['height'] as num?) ?? 0).toInt();
    final pixels = w * h;
    final pathStr = ((r['path'] as String?) ?? '').toLowerCase();
    final fmtRank = pathStr.endsWith('.png') ? 3 : ((pathStr.endsWith('.webp') || pathStr.endsWith('.bmp')) ? 2 : 1);
    final sz = ((r['file_size'] as num?) ?? 0).toInt();
    return pixels * 10 + fmtRank * 1000 + (sz ~/ 1024);
  }

  Future<Map<String, dynamic>> autoCleanDuplicates(FileDeleteMode deleteMode) async {
    final allRows = await db.query('images');
    final rowsById = <int, Map<String, Object?>>{};
    final md5Groups = <String, List<int>>{};
    final dhashGroups = <String, List<int>>{};

    for (final r in allRows) {
      final id = (r['id'] as num).toInt();
      rowsById[id] = r;
      final md5 = (r['file_md5'] as String?) ?? '';
      final dh = (r['dhash'] as String?) ?? '';
      if (md5.isNotEmpty) md5Groups.putIfAbsent(md5, () => []).add(id);
      if (dh.isNotEmpty) dhashGroups.putIfAbsent(dh, () => []).add(id);
    }

    final toDeleteMap = <int, int>{}; // loserId -> winnerId
    for (final groups in [md5Groups, dhashGroups]) {
      for (final ids in groups.values) {
        final active = ids.where((i) => !toDeleteMap.containsKey(i)).toList();
        if (active.length > 1) {
          active.sort((a, b) => _qualityScore(rowsById[b]!).compareTo(_qualityScore(rowsById[a]!)));
          final winner = active.first;
          for (final loser in active.skip(1)) {
            toDeleteMap[loser] = winner;
          }
        }
      }
    }

    int deletedCount = 0;
    int freedBytes = 0;

    for (final entry in toDeleteMap.entries) {
      final loserId = entry.key;
      final winnerId = entry.value;
      final loserRow = rowsById[loserId]!;

      // Transfer any unique tags from loser to winner
      final loserTags = await db.query('image_tags', where: 'image_id = ?', whereArgs: [loserId]);
      for (final lt in loserTags) {
        await db.insert(
          'image_tags',
          {
            'image_id': winnerId,
            'tag': lt['tag'],
            'source': lt['source'],
            'confidence': lt['confidence'],
          },
          conflictAlgorithm: ConflictAlgorithm.ignore,
        );
      }

      final fsize = ((loserRow['file_size'] as num?) ?? 0).toInt();
      await deleteMediaItems([loserId], deleteMode);
      freedBytes += fsize;
      deletedCount++;
    }

    return {
      'deleted': deletedCount,
      'freed_mb': double.parse((freedBytes / (1024 * 1024)).toStringAsFixed(2)),
    };
  }

  // ===================================================================
  // JSON BACKUP EXPORT & IMPORT (100% compatible with kokekbooru.py v3.0)
  // ===================================================================

  Future<Map<String, dynamic>> exportBackupPayload(HubSettings settings) async {
    final categories = await db.query('custom_categories', orderBy: 'sort_order ASC');
    final folders = await db.query('folders');
    final tagTransRows = await db.query('tag_translations');
    final ignoredRows = await db.query('ignored_paths');
    final notesRows = await db.query('artist_notes');

    final tagTrans = <String, String>{};
    for (final r in tagTransRows) {
      tagTrans[(r['tag_en'] as String)] = (r['tag_ru'] as String);
    }

    final allTagRows = await db.query('image_tags');
    final tagsByImg = <int, List<Map<String, dynamic>>>{};
    for (final r in allTagRows) {
      final iid = (r['image_id'] as num).toInt();
      tagsByImg.putIfAbsent(iid, () => []).add({
        'tag': r['tag'],
        'source': r['source'],
        'confidence': r['confidence'],
      });
    }

    final imageRows = await db.query('images');
    final itemsBackup = imageRows.map((r) {
      final iid = (r['id'] as num).toInt();
      return {
        'path': r['path'],
        'filename': r['filename'],
        'file_md5': r['file_md5'] ?? '',
        'dhash': r['dhash'] ?? '',
        'is_favorite': ((r['is_favorite'] as num?) ?? 0).toInt(),
        'folder_category': r['folder_category'] ?? 'general',
        'flip_h': ((r['flip_h'] as num?) ?? 0).toInt(),
        'grayscale': ((r['grayscale'] as num?) ?? 0).toInt(),
        'notes': r['notes'] ?? '',
        'tags': tagsByImg[iid] ?? [],
      };
    }).toList();

    return {
      'hub_backup_version': '3.0',
      'edition': 'public',
      'exported_at': DateTime.now().millisecondsSinceEpoch / 1000.0,
      'settings': settings.toMap(),
      'custom_categories': categories,
      'folders': folders,
      'ignored_paths': ignoredRows.map((r) => r['path']).toList(),
      'artist_notes': notesRows,
      'tag_translations': tagTrans,
      'items': itemsBackup,
    };
  }

  Future<Map<String, int>> importBackupPayload(Map<String, dynamic> data) async {
    if (data['settings'] is Map<String, dynamic>) {
      final s = HubSettings.fromMap(data['settings'] as Map<String, dynamic>);
      await saveSettings(s);
    }

    if (data['custom_categories'] is List) {
      for (final c in data['custom_categories'] as List) {
        if (c is Map && c['key'] != null && c['label'] != null) {
          final k = c['key'].toString();
          if (kLegacyPresetCategoryKeys.contains(k)) continue;
          final lbl = k == 'general' ? '🗂️ Все файлы' : c['label'].toString();
          await db.insert(
            'custom_categories',
            {
              'key': k,
              'label': lbl,
              'sort_order': ((c['sort_order'] as num?) ?? 0).toInt(),
            },
            conflictAlgorithm: ConflictAlgorithm.replace,
          );
        }
      }
    }

    if (data['folders'] is List) {
      for (final f in data['folders'] as List) {
        if (f is Map && f['path'] != null) {
          await db.insert(
            'folders',
            {
              'path': f['path'].toString(),
              'category': (f['category'] ?? 'general').toString(),
              'label': (f['label'] ?? '').toString(),
              'added_at': ((f['added_at'] as num?) ?? (DateTime.now().millisecondsSinceEpoch / 1000.0)).toDouble(),
            },
            conflictAlgorithm: ConflictAlgorithm.ignore,
          );
        }
      }
    }

    if (data['ignored_paths'] is List) {
      final now = DateTime.now().millisecondsSinceEpoch / 1000.0;
      for (final pItem in data['ignored_paths'] as List) {
        if (pItem != null && pItem.toString().isNotEmpty) {
          await db.insert(
            'ignored_paths',
            {'path': pItem.toString(), 'file_md5': '', 'ignored_at': now},
            conflictAlgorithm: ConflictAlgorithm.replace,
          );
        }
      }
    }

    if (data['tag_translations'] is Map) {
      final now = DateTime.now().millisecondsSinceEpoch / 1000.0;
      (data['tag_translations'] as Map).forEach((k, v) async {
        await db.insert(
          'tag_translations',
          {'tag_en': k.toString(), 'tag_ru': v.toString(), 'updated_at': now},
          conflictAlgorithm: ConflictAlgorithm.replace,
        );
      });
    }

    int restoredItems = 0;
    int restoredFavs = 0;

    final dbRows = await db.query('images', columns: ['id', 'path', 'filename', 'file_md5']);
    final byPath = <String, int>{};
    final byMd5 = <String, int>{};
    final byName = <String, int>{};

    for (final r in dbRows) {
      final id = (r['id'] as num).toInt();
      final pStr = (r['path'] as String?) ?? '';
      final fName = (r['filename'] as String?) ?? '';
      final fMd5 = (r['file_md5'] as String?) ?? '';
      if (pStr.isNotEmpty) byPath[pStr] = id;
      if (fMd5.isNotEmpty) byMd5[fMd5] = id;
      if (fName.isNotEmpty) byName[fName] = id;
    }

    if (data['items'] is List) {
      for (final it in data['items'] as List) {
        if (it is! Map) continue;
        final pStr = (it['path'] ?? '').toString();
        final fName = (it['filename'] ?? '').toString();
        final fMd5 = (it['file_md5'] ?? '').toString();
        final isFav = ((it['is_favorite'] as num?) ?? 0).toInt() != 0 ? 1 : 0;
        String fCat = (it['folder_category'] ?? 'general').toString();
        if (kLegacyPresetCategoryKeys.contains(fCat)) fCat = 'general';
        final flipH = ((it['flip_h'] as num?) ?? 0).toInt() != 0 ? 1 : 0;
        final gray = ((it['grayscale'] as num?) ?? 0).toInt() != 0 ? 1 : 0;
        final notesTxt = (it['notes'] ?? '').toString();
        final itTags = (it['tags'] is List) ? (it['tags'] as List) : const [];

        if (isFav == 1) restoredFavs++;

        if (fMd5.isNotEmpty || fName.isNotEmpty) {
          await db.insert(
            'backup_item_state',
            {
              'file_md5': fMd5,
              'filename': fName,
              'orig_path': pStr,
              'is_favorite': isFav,
              'folder_category': fCat,
              'flip_h': flipH,
              'grayscale': gray,
              'notes': notesTxt,
              'tags_json': jsonEncode(itTags),
            },
            conflictAlgorithm: ConflictAlgorithm.replace,
          );
        }

        final targetId = byPath[pStr] ?? (fMd5.isNotEmpty ? byMd5[fMd5] : null) ?? byName[fName];
        if (targetId != null) {
          await db.update(
            'images',
            {
              'is_favorite': isFav,
              'folder_category': fCat,
              'flip_h': flipH,
              'grayscale': gray,
              'notes': notesTxt,
            },
            where: 'id = ?',
            whereArgs: [targetId],
          );
          for (final tgObj in itTags) {
            if (tgObj is Map && tgObj['tag'] != null) {
              await db.insert(
                'image_tags',
                {
                  'image_id': targetId,
                  'tag': tgObj['tag'].toString(),
                  'source': (tgObj['source'] ?? 'user').toString(),
                  'confidence': ((tgObj['confidence'] as num?) ?? 1.0).toDouble(),
                },
                conflictAlgorithm: ConflictAlgorithm.replace,
              );
            }
          }
          restoredItems++;
        }
      }
    }

    return {
      'restored_items': restoredItems,
      'restored_favorites': restoredFavs,
    };
  }

  // ===================================================================
  // SERVICE STATISTICS & FULL RESET
  // ===================================================================

  Future<Map<String, dynamic>> getStorageStats() async {
    final imgCntRow = await db.rawQuery('SELECT COUNT(*) AS cnt FROM images');
    final fldCntRow = await db.rawQuery('SELECT COUNT(*) AS cnt FROM folders');
    final ignCntRow = await db.rawQuery('SELECT COUNT(*) AS cnt FROM ignored_paths');

    int cacheBytes = 0;
    if (await thumbsDir.exists()) {
      await for (final entity in thumbsDir.list()) {
        if (entity is File) {
          cacheBytes += await entity.length();
        }
      }
    }
    final dbFile = File(p.join(appDir.path, 'kokekbooru.db'));
    if (await dbFile.exists()) {
      cacheBytes += await dbFile.length();
    }

    return {
      'app_dir': appDir.path,
      'total_items': ((imgCntRow.first['cnt'] as num?) ?? 0).toInt(),
      'total_folders': ((fldCntRow.first['cnt'] as num?) ?? 0).toInt(),
      'ignored_count': ((ignCntRow.first['cnt'] as num?) ?? 0).toInt(),
      'cache_mb': double.parse((cacheBytes / (1024 * 1024)).toStringAsFixed(2)),
    };
  }

  Future<void> fullResetDatabaseAndSettings() async {
    await db.delete('image_tags');
    await db.delete('images');
    await db.delete('folders');
    await db.delete('custom_categories');
    await db.delete('image_translations');
    await db.delete('backup_item_state');
    await db.delete('ignored_paths');
    await db.delete('artist_notes');

    await db.execute(
      "INSERT OR IGNORE INTO custom_categories (key, label, sort_order) VALUES ('general', '🗂️ Все файлы', 0)",
    );

    if (await thumbsDir.exists()) {
      await for (final f in thumbsDir.list()) {
        if (f is File) {
          try {
            await f.delete();
          } catch (_) {}
        }
      }
    }
    if (await settingsFile.exists()) {
      try {
        await settingsFile.delete();
      } catch (_) {}
    }
  }
}
