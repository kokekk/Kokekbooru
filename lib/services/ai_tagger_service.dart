import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';
import 'package:http/http.dart' as http;
import 'package:image/image.dart' as img;
import 'package:onnxruntime/onnxruntime.dart';
import 'package:path/path.dart' as p;
import 'package:video_thumbnail/video_thumbnail.dart';

import '../data/tag_dictionary.dart';
import '../models/models.dart';
import 'database_service.dart';

class ModelDownloadProgress {
  final bool downloading;
  final double percent;
  final String stepText;
  final double downloadedMb;
  final double totalMb;
  final String error;

  const ModelDownloadProgress({
    this.downloading = false,
    this.percent = 0.0,
    this.stepText = '',
    this.downloadedMb = 0.0,
    this.totalMb = 0.0,
    this.error = '',
  });
}

class AiTaggerService {
  static final AiTaggerService instance = AiTaggerService._();
  AiTaggerService._();

  static const String eva02OnnxUrl =
      'https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/resolve/main/model.onnx';
  static const String eva02CsvUrl =
      'https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/resolve/main/selected_tags.csv';

  static const String vitOnnxUrl =
      'https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/resolve/main/model.onnx';
  static const String vitCsvUrl =
      'https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/resolve/main/selected_tags.csv';

  static const int maxGeneralTagsPerItem = 32;
  static const int videoSampleFrames = 3;

  OrtSession? _session;
  String _loadedModelKey = '';
  String activeProvider = 'CPU';
  List<(String, int)> _ortTags = [];

  File get eva02OnnxFile =>
      File(p.join(DatabaseService.instance.modelsDir.path, 'wd_eva02_large_tagger_v3.onnx'));
  File get eva02CsvFile =>
      File(p.join(DatabaseService.instance.modelsDir.path, 'eva02_selected_tags.csv'));

  File get vitOnnxFile =>
      File(p.join(DatabaseService.instance.modelsDir.path, 'wd_vit_tagger_v3.onnx'));
  File get vitCsvFile =>
      File(p.join(DatabaseService.instance.modelsDir.path, 'selected_tags.csv'));

  bool isModelInstalled(String modelKey) {
    if (modelKey == 'vit') {
      return vitOnnxFile.existsSync() &&
          vitOnnxFile.lengthSync() > 10 * 1024 * 1024 &&
          vitCsvFile.existsSync();
    } else {
      return eva02OnnxFile.existsSync() &&
          eva02OnnxFile.lengthSync() > 100 * 1024 * 1024 &&
          eva02CsvFile.existsSync();
    }
  }

  bool get anyModelInstalled => isModelInstalled('eva02') || isModelInstalled('vit');

  /// Detects GPU VRAM in GB on Windows/Linux to pick safe batch sizes:
  /// - 15+ GB -> (16, 6)
  /// - 8-14 GB -> (8, 3)
  /// - 4-7 GB -> (4, 2)
  /// - < 4 GB, CPU, or Android -> (2, 1)
  Future<double?> detectGpuVramGb() async {
    if (Platform.isAndroid || Platform.isIOS) return null;
    try {
      final res = await Process.run(
        'nvidia-smi',
        ['--query-gpu=memory.total', '--format=csv,noheader,nounits'],
      ).timeout(const Duration(seconds: 3));
      if (res.exitCode == 0) {
        final lines = res.stdout.toString().split('\n');
        int maxMiB = 0;
        for (final ln in lines) {
          final v = int.tryParse(ln.trim());
          if (v != null && v > maxMiB) maxMiB = v;
        }
        if (maxMiB > 0) return maxMiB / 1024.0;
      }
    } catch (_) {}

    if (Platform.isWindows) {
      try {
        const psCmd = r"$v = (Get-ItemProperty -Path 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' "
            r"-Name 'HardwareInformation.qwMemorySize' -ErrorAction SilentlyContinue | "
            r"Select-Object -ExpandProperty 'HardwareInformation.qwMemorySize' | Measure-Object -Maximum).Maximum; "
            r"if ($v) { [math]::Round($v / 1GB, 2) }";
        final res = await Process.run(
          'powershell',
          ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', psCmd],
        ).timeout(const Duration(seconds: 4));
        if (res.exitCode == 0) {
          final out = res.stdout.toString().trim().replaceAll(',', '.');
          final val = double.tryParse(out);
          if (val != null && val > 0) return val;
        }
      } catch (_) {}
    }
    return null;
  }

  Future<(int imageBatch, int videoBatch)> getOptimalBatchSizes(HubSettings settings) async {
    if (Platform.isAndroid || !settings.gpuEnabled) {
      return (2, 1);
    }
    final vramGb = await detectGpuVramGb();
    if (vramGb == null) return (4, 2);
    if (vramGb >= 15.0) return (16, 6);
    if (vramGb >= 8.0) return (8, 3);
    if (vramGb >= 4.0) return (4, 2);
    return (2, 1);
  }

  Future<void> downloadModel(
    String modelKey,
    void Function(ModelDownloadProgress) onProgress,
  ) async {
    final isVit = modelKey == 'vit';
    final onnxUrl = isVit ? vitOnnxUrl : eva02OnnxUrl;
    final csvUrl = isVit ? vitCsvUrl : eva02CsvUrl;
    final onnxTarget = isVit ? vitOnnxFile : eva02OnnxFile;
    final csvTarget = isVit ? vitCsvFile : eva02CsvFile;
    final title = isVit ? 'WD-ViT v3' : 'WD-EVA02-Large v3';

    try {
      onProgress(ModelDownloadProgress(
        downloading: true,
        percent: 5.0,
        stepText: 'Скачивание словаря тегов ($title)...',
      ));

      final csvResp = await http.get(Uri.parse(csvUrl));
      if (csvResp.statusCode == 200) {
        await csvTarget.writeAsBytes(csvResp.bodyBytes);
      } else {
        throw Exception('HTTP ${csvResp.statusCode} при скачивании CSV');
      }

      onProgress(ModelDownloadProgress(
        downloading: true,
        percent: 15.0,
        stepText: 'Скачивание нейросети $title...',
      ));

      final req = http.Request('GET', Uri.parse(onnxUrl));
      final streamed = await req.send();
      if (streamed.statusCode != 200) {
        throw Exception('HTTP ${streamed.statusCode} при скачивании ONNX');
      }

      final totalBytes = streamed.contentLength ?? (isVit ? 377000000 : 1350000000);
      int received = 0;
      final tmpFile = File('${onnxTarget.path}.part');
      final sink = tmpFile.openWrite();

      await for (final chunk in streamed.stream) {
        sink.add(chunk);
        received += chunk.length;
        final ratio = (received / totalBytes).clamp(0.0, 1.0);
        onProgress(ModelDownloadProgress(
          downloading: true,
          percent: double.parse((15.0 + ratio * 84.0).toStringAsFixed(1)),
          stepText: 'Скачивание $title...',
          downloadedMb: double.parse((received / (1024 * 1024)).toStringAsFixed(1)),
          totalMb: double.parse((totalBytes / (1024 * 1024)).toStringAsFixed(1)),
        ));
      }
      await sink.flush();
      await sink.close();

      if (await onnxTarget.exists()) {
        await onnxTarget.delete();
      }
      await tmpFile.rename(onnxTarget.path);

      onProgress(const ModelDownloadProgress(
        downloading: false,
        percent: 100.0,
        stepText: 'Модель успешно установлена!',
      ));
    } catch (e) {
      onProgress(ModelDownloadProgress(
        downloading: false,
        percent: 0.0,
        stepText: 'Ошибка загрузки',
        error: e.toString(),
      ));
      rethrow;
    }
  }

  Future<void> deleteModel(String modelKey) async {
    releaseSession();
    final files = modelKey == 'vit'
        ? [vitOnnxFile, vitCsvFile]
        : [eva02OnnxFile, eva02CsvFile];
    for (final f in files) {
      if (await f.exists()) {
        try {
          await f.delete();
        } catch (_) {}
      }
    }
  }

  void releaseSession() {
    try {
      _session?.release();
    } catch (_) {}
    _session = null;
    _loadedModelKey = '';
  }

  Future<bool> ensureLoaded(HubSettings settings) async {
    if (!settings.aiEnabled) {
      releaseSession();
      return false;
    }

    String chosen = settings.aiModel;
    if (!isModelInstalled(chosen)) {
      if (isModelInstalled('eva02')) {
        chosen = 'eva02';
      } else if (isModelInstalled('vit')) {
        chosen = 'vit';
      } else {
        return false;
      }
    }

    if (_session != null && _loadedModelKey == chosen && _ortTags.isNotEmpty) {
      return true;
    }

    releaseSession();

    final onnxFile = chosen == 'vit' ? vitOnnxFile : eva02OnnxFile;
    final csvFile = chosen == 'vit' ? vitCsvFile : eva02CsvFile;

    try {
      // Load CSV tags
      final lines = await csvFile.readAsLines();
      _ortTags = [];
      for (int i = 1; i < lines.length; i++) {
        final row = lines[i].split(',');
        if (row.length >= 3) {
          final tName = row[1].trim();
          final catId = int.tryParse(row[2].trim()) ?? 0;
          _ortTags.add((tName, catId));
        }
      }

      OrtEnv.instance.init();
      final sessionOptions = OrtSessionOptions();
      if (settings.gpuEnabled) {
        activeProvider = Platform.isAndroid ? 'NNAPI / GPU' : 'GPU (DirectML / CUDA)';
      } else {
        activeProvider = 'CPU';
      }

      _session = OrtSession.fromFile(onnxFile, sessionOptions);
      _loadedModelKey = chosen;
      return true;
    } catch (e) {
      activeProvider = 'CPU';
      _session = null;
      return false;
    }
  }

  /// Converts a decoded `img.Image` into a 448x448 BGR Float32List tensor on a white canvas.
  Float32List imageToOrtTensor(img.Image src, {int inpSize = 448}) {
    final w = src.width;
    final h = src.height;
    final ratio = math.min(inpSize / math.max(1, w), inpSize / math.max(1, h));
    final nw = math.max(1, (w * ratio).round());
    final nh = math.max(1, (h * ratio).round());

    final resized = img.copyResize(
      src,
      width: nw,
      height: nh,
      interpolation: img.Interpolation.cubic,
    );

    final tensor = Float32List(1 * inpSize * inpSize * 3);
    // Fill white background (255.0)
    for (int i = 0; i < tensor.length; i++) {
      tensor[i] = 255.0;
    }

    final padLeft = (inpSize - nw) ~/ 2;
    final padTop = (inpSize - nh) ~/ 2;

    for (int y = 0; y < nh; y++) {
      for (int x = 0; x < nw; x++) {
        final px = resized.getPixel(x, y);
        final dstY = padTop + y;
        final dstX = padLeft + x;
        final baseIdx = (dstY * inpSize + dstX) * 3;
        // SmilingWolf WDv3 expects BGR float32 [0..255]
        tensor[baseIdx] = px.b.toDouble();
        tensor[baseIdx + 1] = px.g.toDouble();
        tensor[baseIdx + 2] = px.r.toDouble();
      }
    }
    return tensor;
  }

  Future<List<double>?> runInferenceOnTensor(Float32List tensor448) async {
    if (_session == null || _ortTags.isEmpty) return null;
    try {
      final inputOrt = OrtValueTensor.createTensorWithDataList(
        tensor448,
        [1, 448, 448, 3],
      );
      final runOptions = OrtRunOptions();
      final inputName = _session!.inputNames.isNotEmpty ? _session!.inputNames.first : 'input_1:0';
      final outputs = await _session!.runAsync(runOptions, {inputName: inputOrt});
      inputOrt.release();
      runOptions.release();

      if (outputs == null || outputs.isEmpty || outputs.first == null) {
        return null;
      }
      final rawVal = outputs.first!.value;
      outputs.first!.release();

      if (rawVal is List && rawVal.isNotEmpty) {
        final firstBatch = rawVal.first;
        if (firstBatch is List) {
          return firstBatch.map((e) => (e as num).toDouble()).toList();
        }
      }
    } catch (_) {}
    return null;
  }

  /// Maps raw model probabilities to Kokekbooru namespaced tags (`angle:*`, `pose:*`, `detail:*`, `char:*`, `nsfw:*`).
  Map<String, (String source, double confidence)> mapPredictionsToTags(
    List<double> preds,
    HubSettings settings,
  ) {
    final result = <String, (String, double)>{};
    final isEva = _loadedModelKey == 'eva02';
    final genThresh = isEva ? 0.37 : 0.35;
    final footThresh = isEva ? 0.48 : 0.44;
    const charThresh = 0.32;
    final nsfwOn = settings.nsfwEnabled;

    bool hasFootEvidence = false;
    final charCandidates = <(String, double)>[];
    final generalCandidates = <(String, double)>[];

    final limit = math.min(preds.length, _ortTags.length);
    for (int idx = 0; idx < limit; idx++) {
      final (rawT, catId) = _ortTags[idx];
      final pVal = preds[idx];

      if (catId == 4 && pVal >= charThresh) {
        final cleanChar = rawT.replaceAll(RegExp(r'_\(.*?\)'), '');
        charCandidates.add((cleanChar, pVal));
      } else if (catId == 9 && pVal >= 0.45) {
        if (nsfwOn) {
          if (rawT == 'explicit') {
            result['nsfw:explicit'] = ('wd_ai', double.parse(pVal.toStringAsFixed(3)));
          } else if (rawT == 'questionable') {
            result['nsfw:erotic'] = ('wd_ai', double.parse(pVal.toStringAsFixed(3)));
          }
        }
      } else if (catId == 0 && pVal >= genThresh) {
        if (kEva02FootEvidenceTags.contains(rawT) && pVal >= footThresh) {
          hasFootEvidence = true;
        }
        if (kPublicNsfwTags.containsKey(rawT) || isWdTagNsfw(rawT)) {
          if (nsfwOn && pVal >= 0.38) {
            final mappedNsfw = kPublicNsfwTags[rawT] ?? 'nsfw:$rawT';
            result[mappedNsfw] = ('wd_ai', double.parse(pVal.toStringAsFixed(3)));
          }
        } else if (kPublicAngleTags.containsKey(rawT)) {
          result[kPublicAngleTags[rawT]!] = ('wd_ai', double.parse(pVal.toStringAsFixed(3)));
        } else if (kPublicPoseTags.containsKey(rawT)) {
          result[kPublicPoseTags[rawT]!] = ('wd_ai', double.parse(pVal.toStringAsFixed(3)));
        } else {
          generalCandidates.add((rawT, pVal));
        }
      }
    }

    charCandidates.sort((a, b) => b.$2.compareTo(a.$2));
    for (final (cName, cProb) in charCandidates.take(6)) {
      result['char:$cName'] = ('wd_char', double.parse(cProb.toStringAsFixed(3)));
    }

    generalCandidates.sort((a, b) => b.$2.compareTo(a.$2));
    for (final (gName, gProb) in generalCandidates.take(maxGeneralTagsPerItem)) {
      result['detail:$gName'] = ('wd_ai', double.parse(gProb.toStringAsFixed(3)));
    }

    if (!hasFootEvidence) {
      result.removeWhere((k, _) =>
          k.contains('barefoot') ||
          k.contains('soles') ||
          k.contains('toes') ||
          k.contains('heel') ||
          k.contains('arch') ||
          k.contains('tiptoe'));
    }

    return result;
  }

  /// Extracts 3 sample frames (beginning ~15%, middle ~50%, end ~85%) from a video file
  /// using FFmpeg on Desktop or `video_thumbnail` on Android, and runs combined AI tagging.
  Future<(Map<String, (String, double)> tags, Uint8List? middleThumbBytes)> analyzeVideoFrames(
    File videoFile,
    HubSettings settings,
  ) async {
    final framesBytes = <Uint8List>[];

    if (Platform.isAndroid || Platform.isIOS) {
      for (final ms in [500, 2500, 5000]) {
        try {
          final bytes = await VideoThumbnail.thumbnailData(
            video: videoFile.path,
            imageFormat: ImageFormat.JPEG,
            maxWidth: 448,
            quality: 85,
            timeMs: ms,
          );
          if (bytes != null && bytes.isNotEmpty) {
            framesBytes.add(bytes);
          }
        } catch (_) {}
      }
    } else {
      // Desktop FFmpeg 3-frame extraction
      final tmpDir = await Directory.systemTemp.createTemp('kokek_vid_');
      try {
        for (int i = 0; i < videoSampleFrames; i++) {
          final outJpg = p.join(tmpDir.path, 'frame_$i.jpg');
          final ssSec = i == 0 ? '00:00:01' : (i == 1 ? '00:00:03' : '00:00:06');
          final res = await Process.run('ffmpeg', [
            '-y',
            '-ss',
            ssSec,
            '-i',
            videoFile.path,
            '-frames:v',
            '1',
            '-q:v',
            '3',
            outJpg,
          ]).timeout(const Duration(seconds: 8));
          final f = File(outJpg);
          if (res.exitCode == 0 && await f.exists()) {
            framesBytes.add(await f.readAsBytes());
          }
        }
      } catch (_) {
      } finally {
        try {
          await tmpDir.delete(recursive: true);
        } catch (_) {}
      }
    }

    Uint8List? middleThumb;
    if (framesBytes.isNotEmpty) {
      middleThumb = framesBytes[framesBytes.length ~/ 2];
    }

    final mergedTags = <String, (String, double)>{
      'detail:video': ('system', 1.0),
    };

    final ready = await ensureLoaded(settings);
    if (ready && framesBytes.isNotEmpty) {
      List<double>? maxPreds;
      for (final fBytes in framesBytes) {
        final decoded = img.decodeImage(fBytes);
        if (decoded == null) continue;
        final tensor = imageToOrtTensor(decoded);
        final preds = await runInferenceOnTensor(tensor);
        if (preds != null) {
          if (maxPreds == null) {
            maxPreds = List<double>.from(preds);
          } else {
            for (int i = 0; i < math.min(maxPreds.length, preds.length); i++) {
              if (preds[i] > maxPreds[i]) maxPreds[i] = preds[i];
            }
          }
        }
      }
      if (maxPreds != null) {
        mergedTags.addAll(mapPredictionsToTags(maxPreds, settings));
      }
    }

    return (mergedTags, middleThumb);
  }
}
