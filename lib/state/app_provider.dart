import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:image_picker/image_picker.dart';
import 'package:path/path.dart' as p;
import 'package:permission_handler/permission_handler.dart';
import 'package:url_launcher/url_launcher.dart';

import '../data/tag_dictionary.dart';
import '../models/models.dart';
import '../services/ai_tagger_service.dart';
import '../services/database_service.dart';
import '../services/localization_service.dart';
import '../services/ocr_translation_service.dart';
import '../services/scanner_service.dart';

class AutocompleteSuggestion {
  final String rawTag;
  final String namespace;
  final String shortName;
  final String ruName;
  final int count;

  const AutocompleteSuggestion({
    required this.rawTag,
    required this.namespace,
    required this.shortName,
    required this.ruName,
    required this.count,
  });
}

class AppProvider extends ChangeNotifier {
  HubSettings settings = const HubSettings();

  String searchQuery = '';
  String activeFolderCat = 'general';
  String activeSubfolder = 'all';
  String activeMediaFilter = 'all'; // 'images', 'all', 'videos'
  String activeAspect = 'all'; // 'all', 'portrait', 'landscape', 'square'
  String activeSort = 'newest';
  bool favoritesOnly = false;
  bool duplicatesOnly = false;
  int randomSeed = 42;

  bool bulkMode = false;
  final Set<int> selectedImageIds = {};

  List<MediaItem> visibleImages = [];
  List<AppFolderCategory> folderCategories = [];
  List<DiskSubfolderCount> diskSubfolders = [];
  List<TagCountItem> sidebarTags = [];
  List<AutocompleteSuggestion> autocompleteCatalog = [];
  List<ArtistNoteItem> artistNotes = [];
  Map<String, String> cachedTagRu = {};

  int totalIndexedCount = 0;
  int favoritesCount = 0;
  int duplicatesCount = 0;
  int worseDuplicatesCount = 0;
  String currentScanFolderPath = '';

  ScanProgressState scanProgress = const ScanProgressState();
  ModelDownloadProgress aiDownloadProgress = const ModelDownloadProgress();
  ModelDownloadProgress nllbDownloadProgress = const ModelDownloadProgress();

  String get lang => settings.language.isNotEmpty ? settings.language : 'ru';

  String tr(String key, [Map<String, String>? params]) =>
      AppStrings.tr(lang, key, params);

  Future<void> initialize() async {
    await DatabaseService.instance.init();
    settings = await DatabaseService.instance.loadSettings();
    activeSort = settings.defaultSort;
    cachedTagRu = await DatabaseService.instance.loadCachedTagTranslations();
    final savedFolders = await DatabaseService.instance.getSavedFolderPaths();
    if (savedFolders.isNotEmpty) {
      currentScanFolderPath = savedFolders.first;
    }
    artistNotes = await DatabaseService.instance.getArtistNotes();
    await refreshState();
  }

  /// Formats a tag for display in UI: translates to Russian ONLY when interface language is Russian
  /// AND `settings.tagsInRussian` is enabled.
  String formatTagDisplay(String rawTag) {
    if (rawTag.isEmpty) return '';
    if (rawTag == 'detail:video' || rawTag == 'video') {
      return tr('videoTag');
    }
    final shortName = rawTag.contains(':') ? rawTag.split(':').sublist(1).join(':') : rawTag;
    if (lang != 'ru' || !settings.tagsInRussian) {
      return shortName.replaceAll('_', ' ');
    }
    return translateSingleTagRu(rawTag, dbCache: cachedTagRu);
  }

  /// Parses Booru search query into positive (AND) and negative (! or -) tokens.
  (List<String> posTokens, List<String> negTokens) parseSearchQuery(String qRaw) {
    final qClean = qRaw.trim().toLowerCase();
    if (qClean.isEmpty) return (const [], const []);
    final rawParts = qClean.contains(',')
        ? qClean.split(',').map((s) => s.trim()).where((s) => s.isNotEmpty).toList()
        : qClean.split(RegExp(r'\s+')).map((s) => s.trim()).where((s) => s.isNotEmpty).toList();

    final pos = <String>[];
    final neg = <String>[];
    for (final part in rawParts) {
      if ((part.startsWith('!') || part.startsWith('-')) && part.length > 1) {
        neg.add(part.substring(1).trim());
      } else {
        pos.add(part);
      }
    }
    return (pos, neg);
  }

  bool _tokenMatchesItem(
    String tok,
    Set<String> itags,
    Set<String> shortTags,
    String haystack,
  ) {
    if (tok.isEmpty) return true;
    final tokNorm = tok.trim().toLowerCase();
    final tokUnder = tokNorm.replaceAll(' ', '_');

    for (final candidate in [tokNorm, tokUnder]) {
      if (itags.contains(candidate) ||
          shortTags.contains(candidate) ||
          itags.contains('detail:$candidate') ||
          itags.contains('angle:$candidate') ||
          itags.contains('pose:$candidate') ||
          itags.contains('nsfw:$candidate') ||
          itags.contains('char:$candidate')) {
        return true;
      }
      if (kExpandedRuSearchSynonyms.containsKey(candidate)) {
        final synList = kExpandedRuSearchSynonyms[candidate]!;
        for (final s in synList) {
          final sLow = s.toLowerCase();
          final sShort = sLow.contains(':') ? sLow.split(':')[1] : sLow;
          if (itags.contains(sLow) || shortTags.contains(sShort)) {
            return true;
          }
        }
      }
    }
    return haystack.contains(tokNorm) || haystack.contains(tokUnder);
  }

  int _qualityScore(Map<String, Object?> r) {
    final w = ((r['width'] as num?) ?? 0).toInt();
    final h = ((r['height'] as num?) ?? 0).toInt();
    final pixels = w * h;
    final pathStr = ((r['path'] as String?) ?? '').toLowerCase();
    final fmtRank = pathStr.endsWith('.png')
        ? 3
        : ((pathStr.endsWith('.webp') || pathStr.endsWith('.bmp')) ? 2 : 1);
    final sz = ((r['file_size'] as num?) ?? 0).toInt();
    return pixels * 10 + fmtRank * 1000 + (sz ~/ 1024);
  }

  Future<void> refreshState() async {
    final db = DatabaseService.instance.db;
    final disableVideos = settings.disableVideos;
    final nsfwOn = settings.nsfwEnabled;

    final enabledNs = <String, bool>{
      'angle': settings.catAngleEnabled,
      'pose': settings.catPoseEnabled,
      'detail': settings.catDetailEnabled,
      'nsfw': nsfwOn,
      'char': settings.catCharEnabled,
    };

    final tagRows = await db.rawQuery("SELECT image_id, tag FROM image_tags WHERE tag NOT LIKE 'light:%'");
    final tagsMap = <int, List<String>>{};

    for (final r in tagRows) {
      final iid = (r['image_id'] as num).toInt();
      String tg = (r['tag'] as String?) ?? '';
      if (tg.isEmpty) continue;
      String nsPart = tg.contains(':') ? tg.split(':').first : 'detail';
      if (isWdTagNsfw(tg)) {
        nsPart = 'nsfw';
        if (!tg.startsWith('nsfw:')) {
          tg = 'nsfw:${tg.split(':').last}';
        }
      }
      if (tg != 'detail:video' && !(enabledNs[nsPart] ?? true)) {
        continue;
      }
      tagsMap.putIfAbsent(iid, () => []).add(tg);
    }

    final allRows = await db.query('images', orderBy: 'id DESC');
    final allTagCounts = <String, int>{};
    final subCounts = <String, int>{};
    final rowsById = <int, Map<String, Object?>>{};
    final md5Groups = <String, List<int>>{};
    final dhashGroups = <String, List<int>>{};

    int favCnt = 0;
    final visibleRows = <Map<String, Object?>>[];

    for (final r in allRows) {
      final art = ((r['art_type'] as String?) ?? 'image').toLowerCase();
      final isVid = art == 'video';
      if (disableVideos && isVid) continue;

      visibleRows.add(r);
      final iid = (r['id'] as num).toInt();
      rowsById[iid] = r;

      final sf = (r['subfolder_name'] as String?) ?? '';
      if (sf.isNotEmpty) {
        subCounts[sf] = (subCounts[sf] ?? 0) + 1;
      }
      if (((r['is_favorite'] as num?) ?? 0).toInt() != 0) {
        favCnt++;
      }

      final itemTags = tagsMap.putIfAbsent(iid, () => []);
      if (isVid && !itemTags.contains('detail:video')) {
        itemTags.insert(0, 'detail:video');
      }
      for (final t in itemTags) {
        allTagCounts[t] = (allTagCounts[t] ?? 0) + 1;
      }

      final fMd5 = (r['file_md5'] as String?) ?? '';
      final dh = (r['dhash'] as String?) ?? '';
      if (fMd5.isNotEmpty) md5Groups.putIfAbsent(fMd5, () => []).add(iid);
      if (dh.isNotEmpty) dhashGroups.putIfAbsent(dh, () => []).add(iid);
    }

    final dupIdToGroup = <int, String>{};
    final worseDupIds = <int>{};

    void processDupClusters(Map<String, List<int>> groups, String prefix) {
      groups.forEach((hVal, ids) {
        if (ids.length > 1) {
          final sortedIds = List<int>.from(ids)
            ..sort((a, b) => _qualityScore(rowsById[b]!).compareTo(_qualityScore(rowsById[a]!)));
          final bestId = sortedIds.first;
          final shortHash = hVal.length > 6 ? hVal.substring(0, 6) : hVal;
          dupIdToGroup.putIfAbsent(bestId, () => '👑 $prefix ($shortHash)');
          for (final wId in sortedIds.skip(1)) {
            dupIdToGroup[wId] = '🗑 $prefix ($shortHash)';
            worseDupIds.add(wId);
          }
        }
      });
    }

    processDupClusters(md5Groups, 'MD5');
    processDupClusters(dhashGroups, 'dHash');

    totalIndexedCount = visibleRows.length;
    favoritesCount = favCnt;
    duplicatesCount = dupIdToGroup.length;
    worseDuplicatesCount = worseDupIds.length;

    final allMediaItems = visibleRows.map((r) {
      final iid = (r['id'] as num).toInt();
      return MediaItem.fromRow(
        r,
        tagsMap[iid] ?? const [],
        dupGroup: dupIdToGroup[iid],
        isWorseDup: worseDupIds.contains(iid),
      );
    }).toList();

    folderCategories = await DatabaseService.instance.getCategoriesWithCounts(allMediaItems);

    final sortedSubs = subCounts.entries.toList()
      ..sort((a, b) => b.value.compareTo(a.value));
    diskSubfolders = sortedSubs
        .map((e) => DiskSubfolderCount(name: e.key, count: e.value))
        .toList();

    final sortedTags = allTagCounts.entries.toList()
      ..sort((a, b) {
        final cmp = b.value.compareTo(a.value);
        return cmp != 0 ? cmp : a.key.compareTo(b.key);
      });

    final tagItems = sortedTags
        .map((e) => TagCountItem(tag: e.key, count: e.value))
        .toList();

    if (nsfwOn) {
      final existingSet = tagItems.map((e) => e.tag).toSet();
      for (final preset in kPresetNsfwTags) {
        if (!existingSet.contains(preset)) {
          tagItems.add(TagCountItem(tag: preset, count: 0));
        }
      }
    }
    sidebarTags = tagItems;

    _rebuildAutocompleteCatalog(tagItems);

    // Filter items according to active search & filters
    final (posTokens, negTokens) = parseSearchQuery(searchQuery);
    final filtered = <MediaItem>[];

    for (final item in allMediaItems) {
      if (activeMediaFilter == 'images' && item.isVideo) continue;
      if (activeMediaFilter == 'videos' && !item.isVideo) continue;
      if (favoritesOnly && !item.isFavorite) continue;
      if (duplicatesOnly && item.dupGroup == null) continue;
      if (activeAspect != 'all' && item.aspectCat != activeAspect) continue;
      if (activeFolderCat != 'all' &&
          activeFolderCat != 'general' &&
          item.folderCategory != activeFolderCat) {
        continue;
      }
      if (activeSubfolder != 'all' && item.subfolderName != activeSubfolder) {
        continue;
      }

      if (posTokens.isNotEmpty || negTokens.isNotEmpty) {
        final itags = item.tags.map((t) => t.toLowerCase()).toSet();
        final shortTags =
            itags.map((t) => t.contains(':') ? t.split(':')[1] : t).toSet();
        final ruTagsStr = item.tags
            .map((t) => translateSingleTagRu(t, dbCache: cachedTagRu).toLowerCase())
            .join(' ');
        final haystack =
            '${item.filename} ${item.subfolderName} ${item.notes} ${itags.join(' ')} ${shortTags.join(' ')} $ruTagsStr'
                .toLowerCase();

        if (negTokens.any((nt) => _tokenMatchesItem(nt, itags, shortTags, haystack))) {
          continue;
        }
        if (!posTokens.every((pt) => _tokenMatchesItem(pt, itags, shortTags, haystack))) {
          continue;
        }
      }

      filtered.add(item);
    }

    if (duplicatesOnly) {
      filtered.sort((a, b) {
        final ga = a.dhash.isNotEmpty ? a.dhash : a.fileMd5;
        final gb = b.dhash.isNotEmpty ? b.dhash : b.fileMd5;
        final cmp = ga.compareTo(gb);
        if (cmp != 0) return cmp;
        if (a.isWorseDup != b.isWorseDup) return a.isWorseDup ? 1 : -1;
        return a.id.compareTo(b.id);
      });
    } else {
      switch (activeSort) {
        case 'oldest':
          filtered.sort((a, b) => a.id.compareTo(b.id));
          break;
        case 'name_asc':
          filtered.sort((a, b) => a.filename.toLowerCase().compareTo(b.filename.toLowerCase()));
          break;
        case 'name_desc':
          filtered.sort((a, b) => b.filename.toLowerCase().compareTo(a.filename.toLowerCase()));
          break;
        case 'res_desc':
          filtered.sort((a, b) => (b.width * b.height).compareTo(a.width * a.height));
          break;
        case 'res_asc':
          filtered.sort((a, b) => (a.width * a.height).compareTo(b.width * b.height));
          break;
        case 'size_desc':
          filtered.sort((a, b) => b.fileSize.compareTo(a.fileSize));
          break;
        case 'random':
          final rng = math.Random(randomSeed);
          filtered.shuffle(rng);
          break;
        case 'newest':
        default:
          filtered.sort((a, b) => b.id.compareTo(a.id));
          break;
      }
    }

    visibleImages = filtered;
    notifyListeners();
  }

  void _rebuildAutocompleteCatalog(List<TagCountItem> currentTags) {
    final mapByShort = <String, AutocompleteSuggestion>{};

    void addEntry(String rawTag, int cnt) {
      final ns = rawTag.contains(':') ? rawTag.split(':').first : 'detail';
      final shortName = rawTag.contains(':') ? rawTag.split(':').sublist(1).join(':') : rawTag;
      if (!settings.nsfwEnabled && (ns == 'nsfw' || isWdTagNsfw(shortName))) return;
      final key = shortName.toLowerCase();
      final existing = mapByShort[key];
      if (existing == null || cnt > existing.count) {
        mapByShort[key] = AutocompleteSuggestion(
          rawTag: rawTag,
          namespace: ns,
          shortName: shortName,
          ruName: translateSingleTagRu(rawTag, dbCache: cachedTagRu),
          count: cnt,
        );
      }
    }

    for (final t in currentTags) {
      addEntry(t.tag, t.count);
    }
    for (final bc in kBrawlStarsChars) {
      addEntry('char:$bc', 0);
    }
    for (final k in kTagRuDict.keys) {
      if (kPublicAngleTags.containsKey(k)) {
        addEntry(kPublicAngleTags[k]!, 0);
      } else if (kPublicPoseTags.containsKey(k)) {
        addEntry(kPublicPoseTags[k]!, 0);
      } else if (kPublicNsfwTags.containsKey(k)) {
        addEntry(kPublicNsfwTags[k]!, 0);
      } else {
        addEntry('detail:$k', 0);
      }
    }

    final list = mapByShort.values.toList()
      ..sort((a, b) {
        final cmp = b.count.compareTo(a.count);
        return cmp != 0 ? cmp : a.shortName.compareTo(b.shortName);
      });
    autocompleteCatalog = list;
  }

  List<AutocompleteSuggestion> getAutocompleteMatches(String currentInput) {
    final lastComma = currentInput.lastIndexOf(',');
    final lastSpace = currentInput.lastIndexOf(' ');
    final splitIdx = math.max(lastComma, lastSpace);
    String rawToken = splitIdx >= 0
        ? currentInput.substring(splitIdx + 1).trimLeft()
        : currentInput.trimLeft();
    if (rawToken.startsWith('!') || rawToken.startsWith('-')) {
      rawToken = rawToken.substring(1).trim();
    }
    if (rawToken.contains(':')) {
      rawToken = rawToken.split(':').last.trim();
    }
    final q = rawToken.toLowerCase();
    if (q.isEmpty) return const [];

    final starts = <AutocompleteSuggestion>[];
    final contains = <AutocompleteSuggestion>[];

    for (final item in autocompleteCatalog) {
      final ruLow = item.ruName.toLowerCase();
      final enLow = item.shortName.toLowerCase();
      final enSpaces = enLow.replaceAll('_', ' ');
      if (ruLow.startsWith(q) || enLow.startsWith(q) || enSpaces.startsWith(q)) {
        starts.add(item);
      } else if (ruLow.contains(q) || enLow.contains(q) || enSpaces.contains(q)) {
        contains.add(item);
      }
    }
    return [...starts, ...contains].take(12).toList();
  }

  // ===================================================================
  // SEARCH & FILTER ACTIONS
  // ===================================================================

  void setSearchQuery(String q) {
    searchQuery = q;
    refreshState();
  }

  /// Left-click/tap includes tag in search; Right-click/long-press excludes (`!tag`).
  void toggleTagInSearch(String rawOrShortTag, {bool exclude = false}) {
    final shortTag = rawOrShortTag.contains(':')
        ? rawOrShortTag.split(':').sublist(1).join(':')
        : rawOrShortTag;
    final norm = shortTag.toLowerCase().trim();
    final neg = '!$norm';

    final tokens = searchQuery.trim().isEmpty
        ? <String>[]
        : (searchQuery.contains(',')
            ? searchQuery.split(',').map((s) => s.trim()).where((s) => s.isNotEmpty).toList()
            : searchQuery.split(RegExp(r'\s+')).map((s) => s.trim()).where((s) => s.isNotEmpty).toList());

    final hasPos = tokens.any((x) => x.toLowerCase() == norm);
    final hasNeg = tokens.any((x) => x.toLowerCase() == neg);

    tokens.removeWhere((x) => x.toLowerCase() == norm || x.toLowerCase() == neg);
    if (exclude) {
      if (!hasNeg) tokens.add(neg);
    } else {
      if (!hasPos) tokens.add(norm);
    }

    searchQuery = tokens.join(', ');
    refreshState();
  }

  void setMediaFilter(String filter) {
    activeMediaFilter = filter;
    refreshState();
  }

  void setFolderCategory(String catKey) {
    activeFolderCat = catKey;
    refreshState();
  }

  void setDiskSubfolder(String subName) {
    activeSubfolder = subName;
    refreshState();
  }

  void setAspectFilter(String aspect) {
    activeAspect = aspect;
    refreshState();
  }

  void setSortOrder(String sort) {
    activeSort = sort;
    if (sort == 'random') {
      randomSeed = math.Random().nextInt(1000000) + 1;
    }
    refreshState();
  }

  void shuffleRandomOrder() {
    activeSort = 'random';
    randomSeed = math.Random().nextInt(1000000) + 1;
    refreshState();
  }

  void toggleFavoritesOnly() {
    favoritesOnly = !favoritesOnly;
    refreshState();
  }

  void setDuplicatesOnly(bool val) {
    duplicatesOnly = val;
    refreshState();
  }

  bool get hasActiveFilters =>
      searchQuery.trim().isNotEmpty ||
      activeAspect != 'all' ||
      activeSubfolder != 'all' ||
      activeFolderCat != 'general' ||
      activeMediaFilter != 'all' ||
      favoritesOnly ||
      duplicatesOnly ||
      activeSort != 'newest';

  void resetAllFilters() {
    searchQuery = '';
    activeFolderCat = 'general';
    activeSubfolder = 'all';
    activeMediaFilter = 'all';
    activeAspect = 'all';
    activeSort = 'newest';
    favoritesOnly = false;
    duplicatesOnly = false;
    refreshState();
  }

  // ===================================================================
  // SCANNING & ANDROID GALLERY INDIVIDUAL MEDIA IMPORT
  // ===================================================================

  Future<bool> requestAndroidStoragePermissions() async {
    if (!Platform.isAndroid) return true;
    try {
      await [
        Permission.photos,
        Permission.videos,
        Permission.storage,
        Permission.manageExternalStorage,
      ].request();
      return true;
    } catch (_) {
      return true;
    }
  }

  Future<String?> pickFolderForScan() async {
    if (Platform.isAndroid) {
      await requestAndroidStoragePermissions();
    }
    final selected = await FilePicker.platform.getDirectoryPath();
    if (selected != null && selected.isNotEmpty) {
      currentScanFolderPath = selected;
      notifyListeners();
    }
    return selected;
  }

  Future<void> startFolderScan({bool forceRescan = false}) async {
    if (scanProgress.running || currentScanFolderPath.trim().isEmpty) return;
    if (Platform.isAndroid) {
      await requestAndroidStoragePermissions();
    }
    await ScannerService.instance.scanFolder(
      folderPath: currentScanFolderPath.trim(),
      category: activeFolderCat,
      forceRescan: forceRescan,
      settings: settings,
      onProgress: (prog) {
        scanProgress = prog;
        notifyListeners();
      },
    );
    await refreshState();
  }

  /// Android-specific feature: pick individual photos/videos directly from the standard Android
  /// Photo Picker / Gallery without scanning an entire directory, and automatically run AI tagging on them!
  Future<void> pickIndividualMediaFromAndroidGallery() async {
    if (scanProgress.running) return;
    await requestAndroidStoragePermissions();

    final picker = ImagePicker();
    List<File> pickedFiles = [];

    try {
      final xfiles = await picker.pickMultipleMedia();
      if (xfiles.isNotEmpty) {
        pickedFiles = xfiles.map((xf) => File(xf.path)).toList();
      }
    } catch (_) {
      // Fallback to FilePicker multi-select
      final res = await FilePicker.platform.pickFiles(
        allowMultiple: true,
        type: FileType.media,
      );
      if (res != null) {
        pickedFiles = res.paths.whereType<String>().map((pStr) => File(pStr)).toList();
      }
    }

    if (pickedFiles.isEmpty) return;

    final parentFolder = pickedFiles.first.parent.path;
    await ScannerService.instance.processFileList(
      files: pickedFiles,
      rootFolderPath: parentFolder,
      category: activeFolderCat,
      forceRescan: true,
      settings: settings,
      onProgress: (prog) {
        scanProgress = prog;
        notifyListeners();
      },
    );
    await refreshState();
  }

  void cancelScan() {
    ScannerService.instance.requestCancel();
  }

  // ===================================================================
  // ITEM ACTIONS, BULK MODE & DELETION MODE
  // ===================================================================

  Future<void> toggleItemFavorite(MediaItem item) async {
    await DatabaseService.instance.updateMediaFlags(item.id, isFavorite: !item.isFavorite);
    await refreshState();
  }

  Future<void> toggleItemFlip(MediaItem item) async {
    await DatabaseService.instance.updateMediaFlags(item.id, flipH: !item.flipH);
    await refreshState();
  }

  Future<void> toggleItemGrayscale(MediaItem item) async {
    await DatabaseService.instance.updateMediaFlags(item.id, grayscale: !item.grayscale);
    await refreshState();
  }

  Future<void> updateItemFolderAndNote(int imageId, {String? folderCat, String? notes}) async {
    await DatabaseService.instance.updateMediaFlags(
      imageId,
      folderCategory: folderCat,
      notes: notes,
    );
    await refreshState();
  }

  Future<void> addCustomTagToItem(int imageId, String rawTag) async {
    await DatabaseService.instance.addTagToImage(imageId, rawTag);
    await refreshState();
  }

  Future<void> removeTagFromItem(int imageId, String tag) async {
    await DatabaseService.instance.removeTagFromImage(imageId, tag);
    await refreshState();
  }

  void toggleBulkMode() {
    bulkMode = !bulkMode;
    if (!bulkMode) {
      selectedImageIds.clear();
    }
    notifyListeners();
  }

  void toggleSelectImage(int id) {
    if (selectedImageIds.contains(id)) {
      selectedImageIds.remove(id);
    } else {
      selectedImageIds.add(id);
    }
    notifyListeners();
  }

  void selectAllVisible() {
    for (final item in visibleImages) {
      selectedImageIds.add(item.id);
    }
    notifyListeners();
  }

  void clearSelection() {
    selectedImageIds.clear();
    notifyListeners();
  }

  Future<void> bulkFavorite(bool isFav) async {
    await DatabaseService.instance.bulkSetFavorite(selectedImageIds.toList(), isFav);
    await refreshState();
  }

  Future<void> bulkMoveToFolder(String catKey) async {
    await DatabaseService.instance.bulkMoveToCategory(selectedImageIds.toList(), catKey);
    await refreshState();
  }

  Future<void> bulkAddTag(String rawTag) async {
    await DatabaseService.instance.bulkAddTag(selectedImageIds.toList(), rawTag);
    await refreshState();
  }

  /// Deletes items respecting `settings.deleteMode` (UI-only with `ignored_paths` vs Full System disk deletion).
  Future<int> deleteItems(List<int> ids) async {
    final deleted = await DatabaseService.instance.deleteMediaItems(ids, settings.deleteMode);
    selectedImageIds.removeAll(ids);
    await refreshState();
    return deleted;
  }

  // ===================================================================
  // SETTINGS, AI MODELS, BACKUP & EXTERNAL PLAYER
  // ===================================================================

  Future<void> saveUpdatedSettings(HubSettings newSettings) async {
    // Sync target translation language if interface language changed
    String syncedTarget = newSettings.translateTargetLang;
    if (newSettings.language != settings.language) {
      if (newSettings.language == 'en' && syncedTarget == 'ru') {
        syncedTarget = 'en';
      } else if (newSettings.language == 'ru' && syncedTarget == 'en') {
        syncedTarget = 'ru';
      }
    }
    settings = await DatabaseService.instance.saveSettings(
      newSettings.copyWith(translateTargetLang: syncedTarget),
    );
    await refreshState();
  }

  Future<void> downloadAiModel(String modelKey) async {
    await AiTaggerService.instance.downloadModel(modelKey, (prog) {
      aiDownloadProgress = prog;
      notifyListeners();
    });
    notifyListeners();
  }

  Future<void> deleteAiModel(String modelKey) async {
    await AiTaggerService.instance.deleteModel(modelKey);
    notifyListeners();
  }

  Future<void> downloadLocalNllb() async {
    await OcrTranslationService.instance.downloadLocalNllbModel((prog) {
      nllbDownloadProgress = prog;
      notifyListeners();
    });
    notifyListeners();
  }

  Future<void> deleteLocalNllb() async {
    await OcrTranslationService.instance.deleteLocalNllbModel();
    notifyListeners();
  }

  /// Selects 3 random references (Pose, Camera Angle, Lighting/Details) for Artist Idea Roulette.
  List<MediaItem> getIdeaRouletteSet() {
    final pool = visibleImages.where((x) => !x.isVideo).toList();
    if (pool.isEmpty) return const [];
    final rng = math.Random();
    final c1 = pool[rng.nextInt(pool.length)];
    final pool2 = pool.where((x) => x.id != c1.id).toList();
    final c2 = pool2.isNotEmpty ? pool2[rng.nextInt(pool2.length)] : c1;
    final pool3 = pool.where((x) => x.id != c1.id && x.id != c2.id).toList();
    final c3 = pool3.isNotEmpty ? pool3[rng.nextInt(pool3.length)] : c2;
    return [c1, c2, c3];
  }

  Future<void> openInExternalPlayer(MediaItem item) async {
    final customPlayer = settings.externalPlayerPath.trim();
    if (customPlayer.isNotEmpty && await File(customPlayer).exists()) {
      await Process.start(customPlayer, [item.path], mode: ProcessStartMode.detached);
      return;
    }
    if (Platform.isWindows) {
      await Process.run('cmd', ['/c', 'start', '', item.path]);
    } else if (Platform.isLinux) {
      await Process.start('xdg-open', [item.path], mode: ProcessStartMode.detached);
    } else {
      final uri = Uri.file(item.path);
      await launchUrl(uri);
    }
  }

  Future<void> revealInFileManager(MediaItem item) async {
    if (Platform.isWindows) {
      await Process.run('explorer.exe', ['/select,${item.path}']);
    } else if (Platform.isLinux) {
      await Process.start('xdg-open', [p.dirname(item.path)], mode: ProcessStartMode.detached);
    }
  }

  Future<void> createNewAppFolder(String label) async {
    await DatabaseService.instance.createCategory(label);
    await refreshState();
  }

  Future<void> renameAppFolder(String key, String newLabel) async {
    await DatabaseService.instance.renameCategory(key, newLabel);
    await refreshState();
  }

  Future<void> deleteAppFolder(String key) async {
    await DatabaseService.instance.deleteCategory(key);
    if (activeFolderCat == key) {
      activeFolderCat = 'general';
    }
    await refreshState();
  }
}
