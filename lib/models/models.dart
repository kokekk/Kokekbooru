import 'dart:convert';

/// Deletion mode setting in Kokekbooru (Settings -> General):
/// - [uiOnly]: removes item from Kokekbooru DB & gallery and records its path in `ignored_paths`
///   so future folder scans skip it, while leaving the physical file on disk untouched.
/// - [fullSystem]: deletes the physical file from disk/device storage along with its DB record.
enum FileDeleteMode {
  uiOnly,
  fullSystem;

  String toJson() => name;

  static FileDeleteMode fromJson(String? val) {
    if (val == 'fullSystem' || val == 'disk') return FileDeleteMode.fullSystem;
    return FileDeleteMode.uiOnly;
  }
}

class HubSettings {
  final String externalPlayerPath;
  final String defaultSort;
  final bool aiEnabled;
  final bool gpuEnabled;
  final String aiModel; // 'eva02' or 'vit'
  final String theme; // 'system', 'dark', 'light'
  final bool disableVideos;
  final bool artistMode;
  final bool nsfwEnabled;
  final bool catAngleEnabled;
  final bool catPoseEnabled;
  final bool catDetailEnabled;
  final bool catNsfwEnabled;
  final bool catCharEnabled;
  final bool tagsInRussian;
  final bool imgTranslateEnabled;
  final bool imgTranslateAuto;
  final String translateProvider; // 'google', 'local_nllb', 'mlkit', 'mymemory', 'libre', 'openai'
  final String translateTargetLang; // 'ru', 'en', 'uk', 'de', 'es', 'ja', 'zh-CN'
  final String translateApiUrl;
  final String translateApiKey;
  final String ocrApiKey;
  final String language; // 'ru' or 'en'
  final bool firstRunCompleted;
  final FileDeleteMode deleteMode;

  const HubSettings({
    this.externalPlayerPath = '',
    this.defaultSort = 'newest',
    this.aiEnabled = true,
    this.gpuEnabled = true,
    this.aiModel = 'eva02',
    this.theme = 'system',
    this.disableVideos = false,
    this.artistMode = false,
    this.nsfwEnabled = false,
    this.catAngleEnabled = true,
    this.catPoseEnabled = true,
    this.catDetailEnabled = true,
    this.catNsfwEnabled = true,
    this.catCharEnabled = true,
    this.tagsInRussian = true,
    this.imgTranslateEnabled = true,
    this.imgTranslateAuto = false,
    this.translateProvider = 'google',
    this.translateTargetLang = 'ru',
    this.translateApiUrl = '',
    this.translateApiKey = '',
    this.ocrApiKey = 'helloworld',
    this.language = 'ru',
    this.firstRunCompleted = false,
    this.deleteMode = FileDeleteMode.uiOnly,
  });

  HubSettings copyWith({
    String? externalPlayerPath,
    String? defaultSort,
    bool? aiEnabled,
    bool? gpuEnabled,
    String? aiModel,
    String? theme,
    bool? disableVideos,
    bool? artistMode,
    bool? nsfwEnabled,
    bool? catAngleEnabled,
    bool? catPoseEnabled,
    bool? catDetailEnabled,
    bool? catNsfwEnabled,
    bool? catCharEnabled,
    bool? tagsInRussian,
    bool? imgTranslateEnabled,
    bool? imgTranslateAuto,
    String? translateProvider,
    String? translateTargetLang,
    String? translateApiUrl,
    String? translateApiKey,
    String? ocrApiKey,
    String? language,
    bool? firstRunCompleted,
    FileDeleteMode? deleteMode,
  }) {
    return HubSettings(
      externalPlayerPath: externalPlayerPath ?? this.externalPlayerPath,
      defaultSort: defaultSort ?? this.defaultSort,
      aiEnabled: aiEnabled ?? this.aiEnabled,
      gpuEnabled: gpuEnabled ?? this.gpuEnabled,
      aiModel: aiModel ?? this.aiModel,
      theme: theme ?? this.theme,
      disableVideos: disableVideos ?? this.disableVideos,
      artistMode: artistMode ?? this.artistMode,
      nsfwEnabled: nsfwEnabled ?? this.nsfwEnabled,
      catAngleEnabled: catAngleEnabled ?? this.catAngleEnabled,
      catPoseEnabled: catPoseEnabled ?? this.catPoseEnabled,
      catDetailEnabled: catDetailEnabled ?? this.catDetailEnabled,
      catNsfwEnabled: catNsfwEnabled ?? this.catNsfwEnabled,
      catCharEnabled: catCharEnabled ?? this.catCharEnabled,
      tagsInRussian: tagsInRussian ?? this.tagsInRussian,
      imgTranslateEnabled: imgTranslateEnabled ?? this.imgTranslateEnabled,
      imgTranslateAuto: imgTranslateAuto ?? this.imgTranslateAuto,
      translateProvider: translateProvider ?? this.translateProvider,
      translateTargetLang: translateTargetLang ?? this.translateTargetLang,
      translateApiUrl: translateApiUrl ?? this.translateApiUrl,
      translateApiKey: translateApiKey ?? this.translateApiKey,
      ocrApiKey: ocrApiKey ?? this.ocrApiKey,
      language: language ?? this.language,
      firstRunCompleted: firstRunCompleted ?? this.firstRunCompleted,
      deleteMode: deleteMode ?? this.deleteMode,
    );
  }

  Map<String, dynamic> toMap() {
    return {
      'external_player_path': externalPlayerPath,
      'default_sort': defaultSort,
      'ai_enabled': aiEnabled,
      'gpu_enabled': gpuEnabled,
      'ai_model': aiModel,
      'theme': theme,
      'disable_videos': disableVideos,
      'artist_mode': artistMode,
      'nsfw_enabled': nsfwEnabled,
      'cat_angle_enabled': catAngleEnabled,
      'cat_pose_enabled': catPoseEnabled,
      'cat_detail_enabled': catDetailEnabled,
      'cat_nsfw_enabled': catNsfwEnabled,
      'cat_char_enabled': catCharEnabled,
      'tags_in_russian': tagsInRussian,
      'img_translate_enabled': imgTranslateEnabled,
      'img_translate_auto': imgTranslateAuto,
      'translate_provider': translateProvider,
      'translate_target_lang': translateTargetLang,
      'translate_api_url': translateApiUrl,
      'translate_api_key': translateApiKey,
      'ocr_api_key': ocrApiKey,
      'language': language,
      'first_run_completed': firstRunCompleted,
      'delete_mode': deleteMode.toJson(),
    };
  }

  factory HubSettings.fromMap(Map<String, dynamic> map) {
    final lang = (map['language'] as String?)?.isNotEmpty == true
        ? map['language'] as String
        : 'ru';
    return HubSettings(
      externalPlayerPath: (map['external_player_path'] as String?) ?? '',
      defaultSort: (map['default_sort'] as String?) ?? 'newest',
      aiEnabled: (map['ai_enabled'] as bool?) ?? true,
      gpuEnabled: (map['gpu_enabled'] as bool?) ?? true,
      aiModel: (map['ai_model'] as String?) ?? 'eva02',
      theme: (map['theme'] as String?) ?? 'system',
      disableVideos: (map['disable_videos'] as bool?) ?? false,
      artistMode: (map['artist_mode'] as bool?) ?? false,
      nsfwEnabled: (map['nsfw_enabled'] as bool?) ?? false,
      catAngleEnabled: (map['cat_angle_enabled'] as bool?) ?? true,
      catPoseEnabled: (map['cat_pose_enabled'] as bool?) ?? true,
      catDetailEnabled: (map['cat_detail_enabled'] as bool?) ?? true,
      catNsfwEnabled: (map['cat_nsfw_enabled'] as bool?) ?? true,
      catCharEnabled: (map['cat_char_enabled'] as bool?) ?? true,
      tagsInRussian: (map['tags_in_russian'] as bool?) ?? true,
      imgTranslateEnabled: (map['img_translate_enabled'] as bool?) ?? true,
      imgTranslateAuto: (map['img_translate_auto'] as bool?) ?? false,
      translateProvider: (map['translate_provider'] as String?) ?? 'google',
      translateTargetLang: (map['translate_target_lang'] as String?) ?? lang,
      translateApiUrl: (map['translate_api_url'] as String?) ?? '',
      translateApiKey: (map['translate_api_key'] as String?) ?? '',
      ocrApiKey: (map['ocr_api_key'] as String?) ?? 'helloworld',
      language: lang,
      firstRunCompleted: (map['first_run_completed'] as bool?) ?? false,
      deleteMode: FileDeleteMode.fromJson(map['delete_mode'] as String?),
    );
  }

  @override
  bool operator ==(Object other) {
    if (identical(this, other)) return true;
    if (other is! HubSettings) return false;
    return jsonEncode(toMap()) == jsonEncode(other.toMap());
  }

  @override
  int get hashCode => jsonEncode(toMap()).hashCode;
}

class AppFolderCategory {
  final String key;
  final String label;
  final int sortOrder;
  final int count;

  const AppFolderCategory({
    required this.key,
    required this.label,
    this.sortOrder = 0,
    this.count = 0,
  });

  String localizedLabel(String lang) {
    if (key == 'general') {
      return lang == 'en' ? '🗂️ All Files' : '🗂️ Все файлы';
    }
    return label;
  }

  Map<String, dynamic> toMap() => {
        'key': key,
        'label': label,
        'sort_order': sortOrder,
      };
}

class DiskSubfolderCount {
  final String name;
  final int count;

  const DiskSubfolderCount({required this.name, required this.count});
}

class TagCountItem {
  final String tag;
  final int count;

  const TagCountItem({required this.tag, required this.count});

  String get namespace => tag.contains(':') ? tag.split(':').first : 'detail';
  String get shortName => tag.contains(':') ? tag.split(':').sublist(1).join(':') : tag;
}

class OcrBubbleBlock {
  final String orig;
  final String trans;
  final double x; // percentage 0..100
  final double y; // percentage 0..100
  final double w; // percentage 0..100
  final double h; // percentage 0..100
  final int lines;
  final String bgHex;
  final String fgHex;

  const OcrBubbleBlock({
    required this.orig,
    required this.trans,
    required this.x,
    required this.y,
    required this.w,
    required this.h,
    this.lines = 1,
    this.bgHex = '#FFFFFF',
    this.fgHex = '#111827',
  });

  Map<String, dynamic> toMap() => {
        'orig': orig,
        'trans': trans,
        'x': x,
        'y': y,
        'w': w,
        'h': h,
        'lines': lines,
        'bg': bgHex,
        'fg': fgHex,
      };

  factory OcrBubbleBlock.fromMap(Map<String, dynamic> map) {
    return OcrBubbleBlock(
      orig: (map['orig'] as String?) ?? '',
      trans: (map['trans'] as String?) ?? '',
      x: ((map['x'] as num?) ?? 0).toDouble(),
      y: ((map['y'] as num?) ?? 0).toDouble(),
      w: ((map['w'] as num?) ?? 10).toDouble(),
      h: ((map['h'] as num?) ?? 5).toDouble(),
      lines: ((map['lines'] as num?) ?? 1).toInt(),
      bgHex: (map['bg'] as String?) ?? '#FFFFFF',
      fgHex: (map['fg'] as String?) ?? '#111827',
    );
  }
}

class MediaItem {
  final int id;
  final String path;
  final String filename;
  final String folderPath;
  final String subfolderName;
  final String folderCategory;
  final String fileMd5;
  final String dhash;
  final int fileSize;
  final String artType; // 'image' or 'video'
  final bool isFavorite;
  final int width;
  final int height;
  final String aspectCat; // 'portrait', 'landscape', 'square'
  final String dominantHex;
  final List<String> palette;
  final bool flipH;
  final bool grayscale;
  final String notes;
  final double mtime;
  final double indexedAt;
  final List<String> tags;
  final String? dupGroup;
  final bool isWorseDup;

  const MediaItem({
    required this.id,
    required this.path,
    required this.filename,
    required this.folderPath,
    this.subfolderName = '',
    this.folderCategory = 'general',
    this.fileMd5 = '',
    this.dhash = '',
    this.fileSize = 0,
    this.artType = 'image',
    this.isFavorite = false,
    this.width = 0,
    this.height = 0,
    this.aspectCat = 'square',
    this.dominantHex = '#212531',
    this.palette = const [],
    this.flipH = false,
    this.grayscale = false,
    this.notes = '',
    this.mtime = 0,
    this.indexedAt = 0,
    this.tags = const [],
    this.dupGroup,
    this.isWorseDup = false,
  });

  bool get isVideo => artType == 'video';

  MediaItem copyWith({
    String? folderCategory,
    bool? isFavorite,
    bool? flipH,
    bool? grayscale,
    String? notes,
    List<String>? tags,
    String? dupGroup,
    bool? isWorseDup,
  }) {
    return MediaItem(
      id: id,
      path: path,
      filename: filename,
      folderPath: folderPath,
      subfolderName: subfolderName,
      folderCategory: folderCategory ?? this.folderCategory,
      fileMd5: fileMd5,
      dhash: dhash,
      fileSize: fileSize,
      artType: artType,
      isFavorite: isFavorite ?? this.isFavorite,
      width: width,
      height: height,
      aspectCat: aspectCat,
      dominantHex: dominantHex,
      palette: palette,
      flipH: flipH ?? this.flipH,
      grayscale: grayscale ?? this.grayscale,
      notes: notes ?? this.notes,
      mtime: mtime,
      indexedAt: indexedAt,
      tags: tags ?? this.tags,
      dupGroup: dupGroup ?? this.dupGroup,
      isWorseDup: isWorseDup ?? this.isWorseDup,
    );
  }

  factory MediaItem.fromRow(
    Map<String, Object?> row,
    List<String> rowTags, {
    String? dupGroup,
    bool isWorseDup = false,
  }) {
    final art = ((row['art_type'] as String?) ?? 'image').toLowerCase();
    final isVid = art == 'video';
    final tagsList = List<String>.from(rowTags);
    if (isVid && !tagsList.contains('detail:video') && !tagsList.contains('video')) {
      tagsList.insert(0, 'detail:video');
    }
    List<String> parsedPalette = [];
    final rawPal = row['palette_json'] as String?;
    if (rawPal != null && rawPal.isNotEmpty) {
      try {
        parsedPalette = (jsonDecode(rawPal) as List).map((e) => e.toString()).toList();
      } catch (_) {}
    }
    return MediaItem(
      id: (row['id'] as num).toInt(),
      path: (row['path'] as String?) ?? '',
      filename: (row['filename'] as String?) ?? '',
      folderPath: (row['folder_path'] as String?) ?? '',
      subfolderName: (row['subfolder_name'] as String?) ?? '',
      folderCategory: (row['folder_category'] as String?) ?? 'general',
      fileMd5: (row['file_md5'] as String?) ?? '',
      dhash: (row['dhash'] as String?) ?? '',
      fileSize: ((row['file_size'] as num?) ?? 0).toInt(),
      artType: isVid ? 'video' : 'image',
      isFavorite: ((row['is_favorite'] as num?) ?? 0).toInt() != 0,
      width: ((row['width'] as num?) ?? 0).toInt(),
      height: ((row['height'] as num?) ?? 0).toInt(),
      aspectCat: (row['aspect_cat'] as String?) ?? 'square',
      dominantHex: (row['dominant_hex'] as String?) ?? '#212531',
      palette: parsedPalette,
      flipH: ((row['flip_h'] as num?) ?? 0).toInt() != 0,
      grayscale: ((row['grayscale'] as num?) ?? 0).toInt() != 0,
      notes: (row['notes'] as String?) ?? '',
      mtime: ((row['mtime'] as num?) ?? 0).toDouble(),
      indexedAt: ((row['indexed_at'] as num?) ?? 0).toDouble(),
      tags: tagsList,
      dupGroup: dupGroup,
      isWorseDup: isWorseDup,
    );
  }
}

class ArtistNoteItem {
  final int id;
  final String title;
  final String content;
  final double updatedAt;

  const ArtistNoteItem({
    required this.id,
    required this.title,
    required this.content,
    required this.updatedAt,
  });

  factory ArtistNoteItem.fromRow(Map<String, Object?> row) {
    return ArtistNoteItem(
      id: (row['id'] as num).toInt(),
      title: (row['title'] as String?) ?? '',
      content: (row['content'] as String?) ?? '',
      updatedAt: ((row['updated_at'] as num?) ?? 0).toDouble(),
    );
  }
}

class ScanProgressState {
  final bool running;
  final bool cancelling;
  final String folder;
  final int total;
  final int current;
  final int newAdded;
  final int skippedExisting;
  final String currentFile;
  final String stage;
  final int etaMinutes;
  final int etaSeconds;
  final double speedFps;
  final String message;

  const ScanProgressState({
    this.running = false,
    this.cancelling = false,
    this.folder = '',
    this.total = 0,
    this.current = 0,
    this.newAdded = 0,
    this.skippedExisting = 0,
    this.currentFile = '',
    this.stage = '',
    this.etaMinutes = 0,
    this.etaSeconds = 0,
    this.speedFps = 0.0,
    this.message = '',
  });

  double get percent => total > 0 ? ((current / total) * 100.0).clamp(0.0, 100.0) : 0.0;
}
