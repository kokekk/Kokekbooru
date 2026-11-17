import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'package:google_mlkit_text_recognition/google_mlkit_text_recognition.dart';
import 'package:google_mlkit_translation/google_mlkit_translation.dart';
import 'package:http/http.dart' as http;
import 'package:image/image.dart' as img;
import 'package:path/path.dart' as p;
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

import '../models/models.dart';
import 'ai_tagger_service.dart';
import 'database_service.dart';

class RawOcrLine {
  final String text;
  final double x; // 0..100 %
  final double y; // 0..100 %
  final double w; // 0..100 %
  final double h; // 0..100 %
  final double confidence;

  const RawOcrLine({
    required this.text,
    required this.x,
    required this.y,
    required this.w,
    required this.h,
    this.confidence = 0.9,
  });
}

class OcrTranslationService {
  static final OcrTranslationService instance = OcrTranslationService._();
  OcrTranslationService._();

  static const String nllbRepoBase =
      'https://huggingface.co/mijuanlo/nllb-200-distilled-600M-ct2-int8/resolve/main';
  static const List<(String, String)> nllbFiles = [
    ('model.bin', '$nllbRepoBase/model.bin'),
    ('sentencepiece.bpe.model', '$nllbRepoBase/sentencepiece.bpe.model'),
    ('shared_vocabulary.txt', '$nllbRepoBase/shared_vocabulary.txt'),
    ('config.json', '$nllbRepoBase/config.json'),
  ];

  Directory get transModelDir =>
      Directory(p.join(DatabaseService.instance.modelsDir.path, 'nllb_200_ct2_int8'));

  bool get isLocalNllbInstalled {
    final mBin = File(p.join(transModelDir.path, 'model.bin'));
    final spModel = File(p.join(transModelDir.path, 'sentencepiece.bpe.model'));
    return mBin.existsSync() && mBin.lengthSync() > 100 * 1024 * 1024 && spModel.existsSync();
  }

  Future<void> downloadLocalNllbModel(
    void Function(ModelDownloadProgress) onProgress,
  ) async {
    if (!await transModelDir.exists()) {
      await transModelDir.create(recursive: true);
    }

    try {
      for (int idx = 0; idx < nllbFiles.length; idx++) {
        final (fname, url) = nllbFiles[idx];
        final dest = File(p.join(transModelDir.path, fname));
        if (await dest.exists() && (fname != 'model.bin' || await dest.length() > 100 * 1024 * 1024)) {
          continue;
        }

        final req = http.Request('GET', Uri.parse(url));
        final streamed = await req.send();
        if (streamed.statusCode != 200) {
          throw Exception('HTTP ${streamed.statusCode} при скачивании $fname');
        }
        final total = streamed.contentLength ?? (fname == 'model.bin' ? 615000000 : 2500000);
        int received = 0;
        final sink = dest.openWrite();

        await for (final chunk in streamed.stream) {
          sink.add(chunk);
          received += chunk.length;
          final frac = (received / total).clamp(0.0, 1.0);
          final overallPct = 5.0 + ((idx + frac) / nllbFiles.length) * 95.0;
          onProgress(ModelDownloadProgress(
            downloading: true,
            percent: double.parse(overallPct.toStringAsFixed(1)),
            stepText: 'Скачивание NLLB-200 ($fname)...',
            downloadedMb: double.parse((received / (1024 * 1024)).toStringAsFixed(1)),
            totalMb: double.parse((total / (1024 * 1024)).toStringAsFixed(1)),
          ));
        }
        await sink.flush();
        await sink.close();
      }

      onProgress(const ModelDownloadProgress(
        downloading: false,
        percent: 100.0,
        stepText: 'Локальная модель NLLB-200 установлена!',
      ));
    } catch (e) {
      onProgress(ModelDownloadProgress(
        downloading: false,
        percent: 0.0,
        stepText: 'Ошибка скачивания NLLB-200',
        error: e.toString(),
      ));
      rethrow;
    }
  }

  Future<void> deleteLocalNllbModel() async {
    if (await transModelDir.exists()) {
      await for (final f in transModelDir.list()) {
        if (f is File) {
          try {
            await f.delete();
          } catch (_) {}
        }
      }
    }
  }

  /// Detects when a Latin-only OCR engine misread Cyrillic text as garbled Latin/digits.
  bool _looksLikeGarbledCyrillic(List<RawOcrLine> lines) {
    if (lines.isEmpty) return false;
    final full = lines.map((l) => l.text).join(' ');
    if (RegExp(r'[а-яА-ЯёЁ]').hasMatch(full)) return false;
    if (RegExp(r'[A-Za-z][0-9][A-Za-z]').hasMatch(full)) return true;
    if (RegExp(r'[a-z][A-Z]').hasMatch(full)) return true;
    if (RegExp(r'[A-Z]{2,}[a-z][A-Z]{2,}').hasMatch(full)) return true;
    final avgConf =
        lines.fold<double>(0.0, (s, e) => s + e.confidence) / lines.length;
    if (avgConf < 0.78 && full.trim().length >= 4) return true;
    return false;
  }

  /// Full OCR + Bubble Merging + Translation pipeline for an image (`文A` button).
  Future<List<OcrBubbleBlock>> recognizeAndTranslateImage({
    required MediaItem item,
    required HubSettings settings,
    bool forceRefresh = false,
  }) async {
    final db = DatabaseService.instance.db;
    String targetLang = settings.translateTargetLang.trim().isNotEmpty
        ? settings.translateTargetLang.trim()
        : settings.language;
    if (settings.language == 'en' && targetLang == 'ru') {
      targetLang = 'en';
    }

    if (!forceRefresh) {
      final cachedRows = await db.query(
        'image_translations',
        where: 'image_id = ? AND target_lang = ?',
        whereArgs: [item.id, targetLang],
      );
      if (cachedRows.isNotEmpty) {
        final rawJson = (cachedRows.first['blocks_json'] as String?) ?? '[]';
        try {
          final parsed = (jsonDecode(rawJson) as List)
              .map((e) => OcrBubbleBlock.fromMap(Map<String, dynamic>.from(e as Map)))
              .toList();
          if (parsed.isNotEmpty) return parsed;
        } catch (_) {}
      }
    }

    final file = File(item.path);
    if (!await file.exists()) return [];

    final bytes = await file.readAsBytes();
    final decoded = img.decodeImage(bytes);
    if (decoded == null) return [];

    final origW = decoded.width.toDouble();
    final origH = decoded.height.toDouble();

    // 1. Extract raw OCR lines using platform-appropriate engine + Cyrillic fallback
    List<RawOcrLine> rawLines = await _extractOcrLines(
      file: file,
      decoded: decoded,
      origW: origW,
      origH: origH,
      settings: settings,
      targetLang: targetLang,
    );

    if (rawLines.isEmpty) return [];

    // 2. Sort lines in natural reading order (top-to-bottom, left-to-right) and merge speech bubbles
    rawLines.sort((a, b) {
      final rowA = (a.y / 4.0).round();
      final rowB = (b.y / 4.0).round();
      if (rowA != rowB) return rowA.compareTo(rowB);
      return a.x.compareTo(b.x);
    });

    final mergedBubbles = <Map<String, dynamic>>[];
    for (final b in rawLines) {
      final cleanLine = b.text.replaceAll(RegExp(r'\s+'), ' ').trim();
      if (cleanLine.isEmpty) continue;
      bool merged = false;

      for (final prev in mergedBubbles) {
        final prevX = prev['x'] as double;
        final prevY = prev['y'] as double;
        final prevW = prev['w'] as double;
        final prevH = prev['h'] as double;
        final prevBottom = prevY + prevH;
        final vertGap = b.y - prevBottom;
        final horizOverlap =
            math.min(prevX + prevW, b.x + b.w) - math.max(prevX, b.x);
        final prevCx = prevX + prevW * 0.5;
        final curCx = b.x + b.w * 0.5;

        if (vertGap >= -2.0 &&
            vertGap <= math.max(4.5, b.h * 1.15) &&
            horizOverlap >= -4.0 &&
            (prevCx - curCx).abs() <= 22.0) {
          final prevOrig = prev['orig'] as String;
          prev['orig'] = prevOrig.endsWith('-')
              ? '${prevOrig.substring(0, prevOrig.length - 1)}$cleanLine'
              : '$prevOrig $cleanLine';
          final nx0 = math.min(prevX, b.x);
          final ny0 = math.min(prevY, b.y);
          final nx1 = math.max(prevX + prevW, b.x + b.w);
          final ny1 = math.max(prevY + prevH, b.y + b.h);
          prev['x'] = nx0;
          prev['y'] = ny0;
          prev['w'] = nx1 - nx0;
          prev['h'] = ny1 - ny0;
          prev['lines'] = (prev['lines'] as int) + 1;
          merged = true;
          break;
        }
      }

      if (!merged) {
        mergedBubbles.add({
          'orig': cleanLine,
          'x': b.x,
          'y': b.y,
          'w': b.w,
          'h': b.h,
          'lines': 1,
        });
      }
    }

    // 3. Sample background luminance and translate each merged speech bubble
    final outBlocks = <OcrBubbleBlock>[];
    final fullLines = <String>[];

    for (final bub in mergedBubbles) {
      final bx = (bub['x'] as double).clamp(0.0, 96.0);
      final by = (bub['y'] as double).clamp(0.0, 96.0);
      final bw = math.max(4.0, (bub['w'] as double).clamp(4.0, 100.0 - bx));
      final bh = math.max(2.5, (bub['h'] as double).clamp(2.5, 100.0 - by));

      final sx = ((bx / 100.0) * decoded.width).round().clamp(0, decoded.width - 1);
      final sy = ((by / 100.0) * decoded.height).round().clamp(0, decoded.height - 1);
      final px = decoded.getPixel(sx, sy);
      final rC = px.r.toInt();
      final gC = px.g.toInt();
      final bC = px.b.toInt();
      final lum = 0.299 * rC + 0.587 * gC + 0.114 * bC;
      final bgHex =
          '#${rC.toRadixString(16).padLeft(2, '0')}${gC.toRadixString(16).padLeft(2, '0')}${bC.toRadixString(16).padLeft(2, '0')}';
      final fgHex = lum >= 150 ? '#111827' : '#F8FAFC';

      final origText = bub['orig'] as String;
      final transText = await translateTextSingle(
        text: origText,
        targetLang: targetLang,
        settings: settings,
      );

      outBlocks.add(OcrBubbleBlock(
        orig: origText,
        trans: transText,
        x: double.parse(bx.toStringAsFixed(2)),
        y: double.parse(by.toStringAsFixed(2)),
        w: double.parse(bw.toStringAsFixed(2)),
        h: double.parse(bh.toStringAsFixed(2)),
        lines: bub['lines'] as int,
        bgHex: bgHex,
        fgHex: fgHex,
      ));
      fullLines.add('$origText → $transText');
    }

    if (outBlocks.isNotEmpty) {
      await db.insert(
        'image_translations',
        {
          'image_id': item.id,
          'target_lang': targetLang,
          'provider': '${settings.translateProvider}_v37',
          'blocks_json': jsonEncode(outBlocks.map((b) => b.toMap()).toList()),
          'full_text': fullLines.join('\n'),
          'updated_at': DateTime.now().millisecondsSinceEpoch / 1000.0,
        },
        conflictAlgorithm: ConflictAlgorithm.replace,
      );
    }

    return outBlocks;
  }

  Future<List<RawOcrLine>> _extractOcrLines({
    required File file,
    required img.Image decoded,
    required double origW,
    required double origH,
    required HubSettings settings,
    required String targetLang,
  }) async {
    // 1. Android: Offline Google ML Kit Text Recognition first, then OCR.space fallback for Cyrillic
    if (Platform.isAndroid || Platform.isIOS) {
      try {
        final inputImage = InputImage.fromFilePath(file.path);
        final textRecognizer = TextRecognizer(script: TextRecognitionScript.latin);
        final recognized = await textRecognizer.processImage(inputImage);
        await textRecognizer.close();

        final mlLines = <RawOcrLine>[];
        for (final block in recognized.blocks) {
          for (final line in block.lines) {
            final rect = line.boundingBox;
            final txt = line.text.trim();
            if (txt.isEmpty) continue;
            mlLines.add(RawOcrLine(
              text: txt,
              x: (rect.left / origW) * 100.0,
              y: (rect.top / origH) * 100.0,
              w: (rect.width / origW) * 100.0,
              h: (rect.height / origH) * 100.0,
              confidence: line.confidence ?? 0.85,
            ));
          }
        }
        if (mlLines.isNotEmpty && !_looksLikeGarbledCyrillic(mlLines)) {
          return mlLines;
        }
      } catch (_) {}
    }

    // 2. Windows: Built-in offline Windows.Media.Ocr (PowerShell with forced UTF-8 and ru-RU support)
    if (Platform.isWindows) {
      final prefLang = targetLang != 'ru' ? 'ru-RU' : '';
      final winLines = await _extractWindowsWinRtOcr(
        file: file,
        origW: origW,
        origH: origH,
        preferredLang: prefLang,
      );
      if (winLines.isNotEmpty && !_looksLikeGarbledCyrillic(winLines)) {
        return winLines;
      }
      if (prefLang.isEmpty) {
        final winRuLines = await _extractWindowsWinRtOcr(
          file: file,
          origW: origW,
          origH: origH,
          preferredLang: 'ru-RU',
        );
        if (winRuLines.isNotEmpty) return winRuLines;
      }
    }

    // 3. Desktop RapidOCR CLI/Python bridge if installed on Linux/Windows
    if (Platform.isLinux || Platform.isWindows) {
      try {
        final res = await Process.run('python3', [
          '-c',
          'import sys, json; from rapidocr_onnxruntime import RapidOCR; '
              'ocr = RapidOCR(); r, _ = ocr(sys.argv[1]); '
              'print(json.dumps([[b, t, float(s)] for b, t, s in (r or [])]))',
          file.path,
        ]).timeout(const Duration(seconds: 10));
        if (res.exitCode == 0 && res.stdout.toString().trim().startsWith('[')) {
          final parsed = jsonDecode(res.stdout.toString().trim()) as List;
          final rapidLines = <RawOcrLine>[];
          for (final item in parsed) {
            final box = item[0] as List;
            final txt = item[1].toString().trim();
            final conf = (item[2] as num).toDouble();
            if (txt.isEmpty || conf < 0.30) continue;
            final xs = box.map((pt) => (pt[0] as num).toDouble()).toList();
            final ys = box.map((pt) => (pt[1] as num).toDouble()).toList();
            final x0 = xs.reduce(math.min);
            final x1 = xs.reduce(math.max);
            final y0 = ys.reduce(math.min);
            final y1 = ys.reduce(math.max);
            rapidLines.add(RawOcrLine(
              text: txt,
              x: (x0 / origW) * 100.0,
              y: (y0 / origH) * 100.0,
              w: ((x1 - x0) / origW) * 100.0,
              h: ((y1 - y0) / origH) * 100.0,
              confidence: conf,
            ));
          }
          if (rapidLines.isNotEmpty && !_looksLikeGarbledCyrillic(rapidLines)) {
            return rapidLines;
          }
        }
      } catch (_) {}
    }

    // 4. Cloud fallback: OCR.space API with Cyrillic support (`language=rus`)
    return _extractOcrSpaceCloud(
      decoded: decoded,
      settings: settings,
      targetLang: targetLang,
    );
  }

  Future<List<RawOcrLine>> _extractWindowsWinRtOcr({
    required File file,
    required double origW,
    required double origH,
    String preferredLang = '',
  }) async {
    final safePath = file.path.replaceAll("'", "''");
    final safeLang = preferredLang.replaceAll("'", "");
    final psScript = '''
\$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
\$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
\$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
\$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
\$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
\$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { \$_.Name -eq 'AsTask' -and \$_.GetParameters().Count -eq 1 -and \$_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await(\$WinRtTask, \$ResultType) {
    \$asTask = \$asTaskGeneric.MakeGenericMethod(\$ResultType)
    \$netTask = \$asTask.Invoke(\$null, @(\$WinRtTask))
    \$netTask.Wait(-1) | Out-Null
    \$netTask.Result
}
\$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('$safePath')) ([Windows.Storage.StorageFile])
\$stream = Await (\$file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
\$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync(\$stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
\$bitmap = Await (\$decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
\$engine = \$null
\$prefLang = '$safeLang'
if (\$prefLang) {
    try {
        \$langObj = [Windows.Globalization.Language]::new(\$prefLang)
        if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported(\$langObj)) {
            \$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(\$langObj)
        }
    } catch {}
}
if (\$null -eq \$engine) { \$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if (\$null -eq \$engine) { \$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US')) }
\$ocrResult = Await (\$engine.RecognizeAsync(\$bitmap)) ([Windows.Media.Ocr.OcrResult])
\$outLines = @()
foreach (\$ln in \$ocrResult.Lines) {
    \$x0 = 99999; \$y0 = 99999; \$x1 = 0; \$y1 = 0
    foreach (\$w in \$ln.Words) {
        \$r = \$w.BoundingRect
        if (\$r.X -lt \$x0) { \$x0 = \$r.X }
        if (\$r.Y -lt \$y0) { \$y0 = \$r.Y }
        if ((\$r.X + \$r.Width) -gt \$x1) { \$x1 = \$r.X + \$r.Width }
        if ((\$r.Y + \$r.Height) -gt \$y1) { \$y1 = \$r.Y + \$r.Height }
    }
    \$outLines += [PSCustomObject]@{ text = \$ln.Text; x = \$x0; y = \$y0; w = (\$x1 - \$x0); h = (\$y1 - \$y0) }
}
\$outLines | ConvertTo-Json -Compress
''';

    try {
      final res = await Process.run(
        'powershell',
        ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', psScript],
        stdoutEncoding: utf8,
      ).timeout(const Duration(seconds: 12));
      if (res.exitCode == 0 && res.stdout.toString().trim().isNotEmpty) {
        final rawJson = jsonDecode(res.stdout.toString().trim());
        final list = rawJson is List ? rawJson : [rawJson];
        final out = <RawOcrLine>[];
        for (final ln in list) {
          if (ln is! Map) continue;
          final txt = (ln['text'] ?? '').toString().trim();
          if (txt.isEmpty) continue;
          final x0 = ((ln['x'] as num?) ?? 0).toDouble();
          final y0 = ((ln['y'] as num?) ?? 0).toDouble();
          final bw = math.max(8.0, ((ln['w'] as num?) ?? 40).toDouble());
          final bh = math.max(8.0, ((ln['h'] as num?) ?? 16).toDouble());
          out.add(RawOcrLine(
            text: txt,
            x: (x0 / origW) * 100.0,
            y: (y0 / origH) * 100.0,
            w: (bw / origW) * 100.0,
            h: (bh / origH) * 100.0,
          ));
        }
        return out;
      }
    } catch (_) {}
    return [];
  }

  Future<List<RawOcrLine>> _extractOcrSpaceCloud({
    required img.Image decoded,
    required HubSettings settings,
    required String targetLang,
  }) async {
    const maxDim = 1280;
    img.Image workImg = decoded;
    if (math.max(decoded.width, decoded.height) > maxDim) {
      final ratio = maxDim / math.max(decoded.width, decoded.height);
      workImg = img.copyResize(
        decoded,
        width: math.max(1, (decoded.width * ratio).round()),
        height: math.max(1, (decoded.height * ratio).round()),
      );
    }
    final workW = workImg.width.toDouble();
    final workH = workImg.height.toDouble();
    final jpgBytes = img.encodeJpg(workImg, quality: 82);
    final b64Img = 'data:image/jpeg;base64,${base64Encode(jpgBytes)}';
    final ocrKey = settings.ocrApiKey.trim().isNotEmpty ? settings.ocrApiKey.trim() : 'helloworld';

    final attempts = targetLang != 'ru'
        ? [('1', 'rus'), ('2', '')]
        : [('2', ''), ('1', 'rus')];

    for (final (engineNum, ocrLang) in attempts) {
      try {
        final body = <String, String>{
          'apikey': ocrKey,
          'base64Image': b64Img,
          'isOverlayRequired': 'true',
          'OCREngine': engineNum,
          'scale': 'true',
          'detectOrientation': 'true',
        };
        if (ocrLang.isNotEmpty) {
          body['language'] = ocrLang;
        }
        final resp = await http
            .post(Uri.parse('https://api.ocr.space/parse/image'), body: body)
            .timeout(const Duration(seconds: 14));
        if (resp.statusCode == 200) {
          final data = jsonDecode(resp.body) as Map<String, dynamic>;
          final parsedList = (data['ParsedResults'] as List?) ?? [];
          if (parsedList.isNotEmpty) {
            final overlay = (parsedList.first['TextOverlay'] as Map?) ?? {};
            final lines = (overlay['Lines'] as List?) ?? [];
            final out = <RawOcrLine>[];
            for (final ln in lines) {
              if (ln is! Map) continue;
              final lineTxt = (ln['LineText'] ?? '').toString().trim();
              final words = (ln['Words'] as List?) ?? [];
              if (lineTxt.isEmpty || words.isEmpty) continue;
              final lefts = words.map((w) => ((w['Left'] as num?) ?? 0).toDouble()).toList();
              final tops = words.map((w) => ((w['Top'] as num?) ?? 0).toDouble()).toList();
              final rights = words
                  .map((w) =>
                      ((w['Left'] as num?) ?? 0).toDouble() +
                      ((w['Width'] as num?) ?? 0).toDouble())
                  .toList();
              final bottoms = words
                  .map((w) =>
                      ((w['Top'] as num?) ?? 0).toDouble() +
                      ((w['Height'] as num?) ?? 0).toDouble())
                  .toList();
              final x0 = lefts.reduce(math.min);
              final y0 = tops.reduce(math.min);
              final x1 = rights.reduce(math.max);
              final y1 = bottoms.reduce(math.max);
              out.add(RawOcrLine(
                text: lineTxt,
                x: (x0 / workW) * 100.0,
                y: (y0 / workH) * 100.0,
                w: ((x1 - x0) / workW) * 100.0,
                h: ((y1 - y0) / workH) * 100.0,
              ));
            }
            if (out.isNotEmpty) return out;
          }
        }
      } catch (_) {}
    }
    return [];
  }

  /// Translates a single text string using the user's configured provider:
  /// - Android offline ML Kit On-Device Translation (`mlkit`)
  /// - Desktop offline NLLB-200 (`local_nllb`)
  /// - OpenAI-compatible API (`openai`)
  /// - LibreTranslate (`libre`)
  /// - MyMemory (`mymemory`)
  /// - Google Translate (`google`)
  Future<String> translateTextSingle({
    required String text,
    required String targetLang,
    required HubSettings settings,
  }) async {
    final cleanTxt = text.trim();
    if (cleanTxt.isEmpty) return '';
    final prov = settings.translateProvider.toLowerCase();

    // 1. Android ML Kit On-Device Offline Translation
    if ((Platform.isAndroid || Platform.isIOS) && prov == 'mlkit') {
      try {
        final srcLang = RegExp(r'[а-яА-ЯёЁ]').hasMatch(cleanTxt)
            ? TranslateLanguage.russian
            : TranslateLanguage.english;
        final tgtLang = targetLang == 'en'
            ? TranslateLanguage.english
            : TranslateLanguage.russian;
        if (srcLang == tgtLang) return cleanTxt;
        final translator = OnDeviceTranslator(
          sourceLanguage: srcLang,
          targetLanguage: tgtLang,
        );
        final res = await translator.translateText(cleanTxt);
        await translator.close();
        if (res.trim().isNotEmpty) return res.trim();
      } catch (_) {}
    }

    // 2. Local NLLB-200 INT8 on Desktop if installed
    if (prov == 'local_nllb' && isLocalNllbInstalled && (Platform.isWindows || Platform.isLinux)) {
      try {
        final res = await Process.run('python3', [
          '-c',
          '''
import sys, ctranslate2, sentencepiece as spm
m_dir, txt, tgt = sys.argv[1], sys.argv[2], sys.argv[3]
sp = spm.SentencePieceProcessor()
sp.Load(m_dir + "/sentencepiece.bpe.model")
tr = ctranslate2.Translator(m_dir, device="cpu", compute_type="int8")
src = "rus_Cyrl" if any("а" <= c.lower() <= "я" for c in txt) else "eng_Latn"
tgt_code = "eng_Latn" if tgt == "en" else "rus_Cyrl"
tokens = [src] + sp.EncodeAsPieces(txt) + ["</s>"]
out = tr.translate_batch([tokens], target_prefix=[[tgt_code]], beam_size=2)[0].hypotheses[0]
if out and out[0] == tgt_code: out = out[1:]
print(sp.DecodePieces(out).strip())
''',
          transModelDir.path,
          cleanTxt,
          targetLang,
        ]).timeout(const Duration(seconds: 10));
        if (res.exitCode == 0 && res.stdout.toString().trim().isNotEmpty) {
          return res.stdout.toString().trim();
        }
      } catch (_) {}
    }

    // 3. OpenAI-compatible API
    if (prov == 'openai' && settings.translateApiKey.trim().isNotEmpty) {
      final endpoint = settings.translateApiUrl.trim().isNotEmpty
          ? settings.translateApiUrl.trim()
          : 'https://api.openai.com/v1/chat/completions';
      try {
        final resp = await http
            .post(
              Uri.parse(endpoint),
              headers: {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ${settings.translateApiKey.trim()}',
              },
              body: jsonEncode({
                'model': 'gpt-4o-mini',
                'messages': [
                  {
                    'role': 'system',
                    'content':
                        "Translate the user's text into '$targetLang'. Output ONLY the translated text without quotes or explanations.",
                  },
                  {'role': 'user', 'content': cleanTxt},
                ],
                'temperature': 0.1,
              }),
            )
            .timeout(const Duration(seconds: 10));
        if (resp.statusCode == 200) {
          final data = jsonDecode(resp.body);
          final content = data['choices']?[0]?['message']?['content']?.toString().trim();
          if (content != null && content.isNotEmpty) return content;
        }
      } catch (_) {}
    }

    // 4. LibreTranslate API
    if (prov == 'libre') {
      final endpoint = settings.translateApiUrl.trim().isNotEmpty
          ? settings.translateApiUrl.trim()
          : 'https://libretranslate.de/translate';
      try {
        final resp = await http
            .post(
              Uri.parse(endpoint),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({
                'q': cleanTxt,
                'source': 'auto',
                'target': targetLang,
                'format': 'text',
                if (settings.translateApiKey.trim().isNotEmpty)
                  'api_key': settings.translateApiKey.trim(),
              }),
            )
            .timeout(const Duration(seconds: 8));
        if (resp.statusCode == 200) {
          final data = jsonDecode(resp.body);
          final tr = (data['translatedText'] ?? data['data'])?.toString().trim();
          if (tr != null && tr.isNotEmpty) return tr;
        }
      } catch (_) {}
    }

    // 5. MyMemory API
    if (prov == 'mymemory') {
      try {
        final qEnc = Uri.encodeComponent(cleanTxt);
        final url =
            'https://api.mymemory.translated.net/get?q=$qEnc&langpair=autodetect|$targetLang';
        final resp = await http.get(Uri.parse(url)).timeout(const Duration(seconds: 7));
        if (resp.statusCode == 200) {
          final data = jsonDecode(resp.body);
          final tr = data['responseData']?['translatedText']?.toString().trim();
          if (tr != null && tr.isNotEmpty) return tr;
        }
      } catch (_) {}
    }

    // 6. Google Translate API (default & fallback)
    final qEnc = Uri.encodeComponent(cleanTxt);
    for (final gUrl in [
      'https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=$targetLang&dt=t&q=$qEnc',
      'https://clients5.google.com/translate_a/single?client=dict-chrome-ex&sl=auto&tl=$targetLang&dt=t&q=$qEnc',
    ]) {
      try {
        final resp = await http.get(Uri.parse(gUrl)).timeout(const Duration(seconds: 7));
        if (resp.statusCode == 200) {
          final data = jsonDecode(resp.body);
          if (data is List && data.isNotEmpty && data[0] is List) {
            final buffer = StringBuffer();
            for (final seg in data[0] as List) {
              if (seg is List && seg.isNotEmpty && seg[0] != null) {
                buffer.write(seg[0].toString());
              }
            }
            if (buffer.isNotEmpty) return buffer.toString().trim();
          }
        }
      } catch (_) {}
    }

    return cleanTxt;
  }
}
