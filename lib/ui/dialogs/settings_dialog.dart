import 'dart:convert';
import 'dart:io';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../models/models.dart';
import '../../services/ai_tagger_service.dart';
import '../../services/database_service.dart';
import '../../services/ocr_translation_service.dart';
import '../../state/app_provider.dart';

class SettingsDialog extends StatefulWidget {
  final bool embedded;

  const SettingsDialog({super.key, this.embedded = false});

  @override
  State<SettingsDialog> createState() => _SettingsDialogState();
}

class _SettingsDialogState extends State<SettingsDialog> {
  late HubSettings _initialSnapshot;
  late HubSettings _draft;
  int _activeTabIndex = 0;

  late TextEditingController _playerCtrl;
  late TextEditingController _apiUrlCtrl;
  late TextEditingController _apiKeyCtrl;

  Map<String, dynamic> _storageStats = {};

  @override
  void initState() {
    super.initState();
    final prov = context.read<AppProvider>();
    _initialSnapshot = prov.settings;
    _draft = prov.settings;
    _playerCtrl = TextEditingController(text: _draft.externalPlayerPath);
    _apiUrlCtrl = TextEditingController(text: _draft.translateApiUrl);
    _apiKeyCtrl = TextEditingController(text: _draft.translateApiKey);
    _loadStorageStats();
  }

  @override
  void dispose() {
    _playerCtrl.dispose();
    _apiUrlCtrl.dispose();
    _apiKeyCtrl.dispose();
    super.dispose();
  }

  Future<void> _loadStorageStats() async {
    final st = await DatabaseService.instance.getStorageStats();
    if (mounted) {
      setState(() => _storageStats = st);
    }
  }

  /// True ONLY when `_draft` actually differs from `_initialSnapshot`.
  /// If user toggles a setting and reverts it back, `isDirty` immediately becomes false!
  bool get isDirty => _draft != _initialSnapshot;

  void _updateDraft(HubSettings updated) {
    setState(() {
      _draft = updated;
    });
  }

  Future<void> _saveChanges(AppProvider prov) async {
    await prov.saveUpdatedSettings(_draft);
    setState(() {
      _initialSnapshot = prov.settings;
      _draft = prov.settings;
    });
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(prov.tr('settingsSavedToast'))),
      );
    }
  }

  Future<void> _exportBackupJson(AppProvider prov) async {
    final payload = await DatabaseService.instance.exportBackupPayload(prov.settings);
    final jsonStr = const JsonEncoder.withIndent('  ').convert(payload);
    final dateStr = DateTime.now().toIso8601String().substring(0, 10);
    final defaultName = 'kokekbooru_backup_$dateStr.json';

    final outPath = await FilePicker.platform.saveFile(
      dialogTitle: prov.tr('exportBackup'),
      fileName: defaultName,
      type: FileType.custom,
      allowedExtensions: ['json'],
    );
    if (outPath != null && outPath.isNotEmpty) {
      await File(outPath).writeAsString(jsonStr);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(prov.tr('backupExportedToast', {'path': outPath}))),
        );
      }
    }
  }

  Future<void> _importBackupJson(AppProvider prov) async {
    final picked = await FilePicker.platform.pickFiles(
      type: FileType.custom,
      allowedExtensions: ['json'],
    );
    if (picked == null || picked.files.single.path == null) return;
    final file = File(picked.files.single.path!);
    final raw = await file.readAsString();
    final map = jsonDecode(raw) as Map<String, dynamic>;
    final res = await DatabaseService.instance.importBackupPayload(map);
    await prov.initialize();
    setState(() {
      _initialSnapshot = prov.settings;
      _draft = prov.settings;
    });
    await _loadStorageStats();
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            prov.tr('backupImportedToast', {
              'items': '${res['restored_items'] ?? 0}',
              'favs': '${res['restored_favorites'] ?? 0}',
            }),
          ),
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final prov = context.watch<AppProvider>();
    final lang = _draft.language;
    final isDesktop = MediaQuery.of(context).size.width > 820;

    final tabs = [
      (prov.tr('tabGeneral'), Icons.tune, false),
      (prov.tr('tabAi'), Icons.psychology_outlined, false),
      (prov.tr('tabTrans'), Icons.translate, false),
      (prov.tr('tabNsfw'), Icons.EighteenUpRating, true),
      (prov.tr('tabBackup'), Icons.save_alt, false),
      (prov.tr('tabService'), Icons.build_outlined, false),
    ];

    final contentBody = Column(
      children: [
        // Top header bar
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
          decoration: BoxDecoration(
            border: Border(bottom: BorderSide(color: Theme.of(context).dividerColor)),
          ),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                prov.tr('setTitle'),
                style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
              ),
              Row(
                children: [
                  FilledButton(
                    onPressed: () => _saveChanges(prov),
                    child: Text(prov.tr('save')),
                  ),
                  if (!widget.embedded) ...[
                    const SizedBox(width: 8),
                    OutlinedButton(
                      onPressed: () => Navigator.pop(context),
                      child: Text(prov.tr('close')),
                    ),
                  ],
                ],
              ),
            ],
          ),
        ),

        // Main adaptive layout: 2-column on Desktop vs Top TabBar on Android/Mobile
        Expanded(
          child: isDesktop
              ? Row(
                  children: [
                    // Left navigation column
                    Container(
                      width: 200,
                      decoration: BoxDecoration(
                        border: Border(
                          right: BorderSide(color: Theme.of(context).dividerColor),
                        ),
                      ),
                      child: ListView.builder(
                        padding: const EdgeInsets.all(10),
                        itemCount: tabs.length,
                        itemBuilder: (ctx, idx) {
                          final (label, _, isNsfwTab) = tabs[idx];
                          final active = _activeTabIndex == idx;
                          return Padding(
                            padding: const EdgeInsets.only(bottom: 6),
                            child: ListTile(
                              dense: true,
                              selected: active,
                              selectedTileColor: isNsfwTab
                                  ? const Color(0xFFDC2626)
                                  : Theme.of(context).colorScheme.primary,
                              shape: RoundedRectangleBorder(
                                borderRadius: BorderRadius.circular(8),
                              ),
                              title: Text(
                                label,
                                style: TextStyle(
                                  fontWeight: FontWeight.w600,
                                  color: active
                                      ? Colors.white
                                      : (isNsfwTab ? const Color(0xFFEF4444) : null),
                                ),
                              ),
                              onTap: () => setState(() => _activeTabIndex = idx),
                            ),
                          );
                        },
                      ),
                    ),
                    // Right active pane
                    Expanded(
                      child: Padding(
                        padding: const EdgeInsets.all(18),
                        child: _buildActivePane(context, prov, lang),
                      ),
                    ),
                  ],
                )
              : Column(
                  children: [
                    SingleChildScrollView(
                      scrollDirection: Axis.horizontal,
                      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
                      child: Row(
                        children: List.generate(tabs.length, (idx) {
                          final (label, _, _) = tabs[idx];
                          final active = _activeTabIndex == idx;
                          return Padding(
                            padding: const EdgeInsets.only(right: 6),
                            child: ChoiceChip(
                              label: Text(label),
                              selected: active,
                              onSelected: (_) => setState(() => _activeTabIndex = idx),
                            ),
                          );
                        }),
                      ),
                    ),
                    Expanded(
                      child: Padding(
                        padding: const EdgeInsets.all(14),
                        child: _buildActivePane(context, prov, lang),
                      ),
                    ),
                  ],
                ),
        ),

        // Unsaved changes warning banner (appears ONLY when _draft != _initialSnapshot)
        if (isDirty)
          Container(
            width: double.infinity,
            padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 16),
            color: const Color(0xFFF59E0B).withOpacity(0.18),
            child: Text(
              prov.tr('dirtyWarn'),
              textAlign: TextAlign.center,
              style: const TextStyle(
                color: Color(0xFFF59E0B),
                fontWeight: FontWeight.bold,
                fontSize: 12.5,
              ),
            ),
          ),
      ],
    );

    if (widget.embedded) {
      return contentBody;
    }

    return Dialog(
      insetPadding: const EdgeInsets.all(20),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: SizedBox(
        width: 880,
        height: 640,
        child: contentBody,
      ),
    );
  }

  Widget _buildActivePane(BuildContext context, AppProvider prov, String lang) {
    switch (_activeTabIndex) {
      case 0:
        return _buildGeneralTab(context, prov, lang);
      case 1:
        return _buildAiAndTagsTab(context, prov);
      case 2:
        return _buildTranslatorTab(context, prov);
      case 3:
        return _buildNsfwTab(context, prov);
      case 4:
        return _buildBackupTab(context, prov);
      case 5:
      default:
        return _buildServiceTab(context, prov);
    }
  }

  // TAB 1: GENERAL (Language, Theme, Tags in Russian when RU, Artist Mode, File Deletion Mode, External Player)
  Widget _buildGeneralTab(BuildContext context, AppProvider prov, String lang) {
    return ListView(
      children: [
        Row(
          children: [
            Expanded(
              child: DropdownButtonFormField<String>(
                value: _draft.language,
                decoration: InputDecoration(
                  labelText: prov.tr('setLang'),
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                ),
                items: const [
                  DropdownMenuItem(value: 'ru', child: Text('🇷🇺 Русский')),
                  DropdownMenuItem(value: 'en', child: Text('🇬🇧 English')),
                ],
                onChanged: (val) {
                  if (val != null) {
                    String newTarget = _draft.translateTargetLang;
                    if (val == 'en' && newTarget == 'ru') newTarget = 'en';
                    if (val == 'ru' && newTarget == 'en') newTarget = 'ru';
                    _updateDraft(_draft.copyWith(
                      language: val,
                      translateTargetLang: newTarget,
                    ));
                  }
                },
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: DropdownButtonFormField<String>(
                value: _draft.theme,
                decoration: InputDecoration(
                  labelText: prov.tr('setTheme'),
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                ),
                items: [
                  DropdownMenuItem(value: 'system', child: Text(prov.tr('themeSys'))),
                  DropdownMenuItem(value: 'dark', child: Text(prov.tr('themeDark'))),
                  DropdownMenuItem(value: 'light', child: Text(prov.tr('themeLight'))),
                ],
                onChanged: (val) {
                  if (val != null) _updateDraft(_draft.copyWith(theme: val));
                },
              ),
            ),
          ],
        ),
        const SizedBox(height: 14),

        // "Tags in Russian" checkbox shown ONLY when interface language is Russian
        if (_draft.language == 'ru')
          CheckboxListTile(
            value: _draft.tagsInRussian,
            title: Text(prov.tr('setTagsRu'), style: const TextStyle(fontSize: 13)),
            onChanged: (v) => _updateDraft(_draft.copyWith(tagsInRussian: v ?? true)),
          ),

        CheckboxListTile(
          value: _draft.artistMode,
          title: Text(prov.tr('setArtist'), style: const TextStyle(fontSize: 13)),
          onChanged: (v) => _updateDraft(_draft.copyWith(artistMode: v ?? false)),
        ),

        CheckboxListTile(
          value: _draft.disableVideos,
          title: Text(prov.tr('setNoVid'), style: const TextStyle(fontSize: 13)),
          onChanged: (v) => _updateDraft(_draft.copyWith(disableVideos: v ?? false)),
        ),

        const Divider(height: 24),

        // Explicit File Deletion Mode switcher (UI-only with ignored_paths vs Full system deletion)
        Text(
          prov.tr('setDeleteModeTitle'),
          style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13.5),
        ),
        const SizedBox(height: 6),
        RadioListTile<FileDeleteMode>(
          value: FileDeleteMode.uiOnly,
          groupValue: _draft.deleteMode,
          title: Text(prov.tr('deleteModeUiOnly'), style: const TextStyle(fontSize: 12.5)),
          onChanged: (v) {
            if (v != null) _updateDraft(_draft.copyWith(deleteMode: v));
          },
        ),
        RadioListTile<FileDeleteMode>(
          value: FileDeleteMode.fullSystem,
          groupValue: _draft.deleteMode,
          title: Text(
            prov.tr('deleteModeFullSystem'),
            style: const TextStyle(fontSize: 12.5, color: Color(0xFFEF4444)),
          ),
          onChanged: (v) {
            if (v != null) _updateDraft(_draft.copyWith(deleteMode: v));
          },
        ),

        if (Platform.isWindows || Platform.isLinux) ...[
          const Divider(height: 24),
          Text(
            prov.tr('setPlayer'),
            style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13),
          ),
          const SizedBox(height: 6),
          TextField(
            controller: _playerCtrl,
            decoration: InputDecoration(
              hintText: 'C:\\Program Files\\DAUM\\PotPlayer\\PotPlayerMini64.exe',
              helperText: prov.tr('setPlayerSub'),
              border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
            ),
            onChanged: (v) => _updateDraft(_draft.copyWith(externalPlayerPath: v.trim())),
          ),
        ],
      ],
    );
  }

  // TAB 2: AI & TAGS (WD-EVA02-Large v3 & WD-ViT v3 download/delete, GPU acceleration, Category visibility)
  Widget _buildAiAndTagsTab(BuildContext context, AppProvider prov) {
    final modelInstalled = AiTaggerService.instance.isModelInstalled(_draft.aiModel);

    return ListView(
      children: [
        Text(
          prov.tr('descTabAi'),
          style: const TextStyle(fontSize: 12.5, color: Colors.grey),
        ),
        const SizedBox(height: 14),

        DropdownButtonFormField<String>(
          value: _draft.aiModel,
          isExpanded: true,
          decoration: InputDecoration(
            labelText: prov.tr('setAiModel'),
            border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
          ),
          items: [
            DropdownMenuItem(value: 'eva02', child: Text(prov.tr('aiSmart'))),
            DropdownMenuItem(value: 'vit', child: Text(prov.tr('aiFast'))),
          ],
          onChanged: (val) {
            if (val != null) _updateDraft(_draft.copyWith(aiModel: val));
          },
        ),
        const SizedBox(height: 10),

        // Model download/installed status row: hides Download button when already installed!
        Row(
          children: [
            Expanded(
              child: Text(
                modelInstalled ? prov.tr('modelInstalled') : prov.tr('modelMissing'),
                style: TextStyle(
                  fontSize: 12.5,
                  fontWeight: FontWeight.w600,
                  color: modelInstalled ? const Color(0xFF10B981) : const Color(0xFFF59E0B),
                ),
              ),
            ),
            if (!modelInstalled)
              FilledButton.icon(
                onPressed: prov.aiDownloadProgress.downloading
                    ? null
                    : () => prov.downloadAiModel(_draft.aiModel),
                icon: const Icon(Icons.download, size: 16),
                label: Text(prov.tr('dlModel')),
              )
            else
              OutlinedButton.icon(
                style: OutlinedButton.styleFrom(foregroundColor: const Color(0xFFEF4444)),
                onPressed: () async {
                  await prov.deleteAiModel(_draft.aiModel);
                  setState(() {});
                },
                icon: const Icon(Icons.delete_outline, size: 16),
                label: Text(prov.tr('delModel')),
              ),
          ],
        ),

        if (prov.aiDownloadProgress.downloading) ...[
          const SizedBox(height: 10),
          LinearProgressIndicator(value: prov.aiDownloadProgress.percent / 100.0),
          const SizedBox(height: 4),
          Text(
            '${prov.aiDownloadProgress.stepText} (${prov.aiDownloadProgress.downloadedMb} / ${prov.aiDownloadProgress.totalMb} MB)',
            style: const TextStyle(fontSize: 11.5),
          ),
        ],

        const Divider(height: 24),

        CheckboxListTile(
          value: _draft.aiEnabled,
          title: Text(prov.tr('setAiEn'), style: const TextStyle(fontSize: 13)),
          onChanged: (v) => _updateDraft(_draft.copyWith(aiEnabled: v ?? true)),
        ),

        CheckboxListTile(
          value: _draft.gpuEnabled,
          title: Text(prov.tr('setGpuEn'), style: const TextStyle(fontSize: 13)),
          subtitle: Text(prov.tr('setGpuSub'), style: const TextStyle(fontSize: 11.5)),
          onChanged: (v) => _updateDraft(_draft.copyWith(gpuEnabled: v ?? true)),
        ),

        const Divider(height: 24),
        Text(
          prov.tr('catFilterHeader'),
          style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13),
        ),
        Wrap(
          spacing: 12,
          children: [
            FilterChip(
              label: Text(prov.tr('catAngle')),
              selected: _draft.catAngleEnabled,
              onSelected: (v) => _updateDraft(_draft.copyWith(catAngleEnabled: v)),
            ),
            FilterChip(
              label: Text(prov.tr('catPose')),
              selected: _draft.catPoseEnabled,
              onSelected: (v) => _updateDraft(_draft.copyWith(catPoseEnabled: v)),
            ),
            FilterChip(
              label: Text(prov.tr('catDetail')),
              selected: _draft.catDetailEnabled,
              onSelected: (v) => _updateDraft(_draft.copyWith(catDetailEnabled: v)),
            ),
            FilterChip(
              label: Text(prov.tr('catChar')),
              selected: _draft.catCharEnabled,
              onSelected: (v) => _updateDraft(_draft.copyWith(catCharEnabled: v)),
            ),
          ],
        ),
      ],
    );
  }

  // TAB 3: TRANSLATOR (OCR & Bubble Overlay settings, auto-translate disabled when main toggle is off)
  Widget _buildTranslatorTab(BuildContext context, AppProvider prov) {
    final nllbInstalled = OcrTranslationService.instance.isLocalNllbInstalled;

    return ListView(
      children: [
        Text(
          prov.tr('descTabTrans'),
          style: const TextStyle(fontSize: 12.5, color: Colors.grey),
        ),
        const SizedBox(height: 12),

        CheckboxListTile(
          value: _draft.imgTranslateEnabled,
          title: Text(prov.tr('setImgTransEn'), style: const TextStyle(fontSize: 13)),
          onChanged: (v) => _updateDraft(_draft.copyWith(imgTranslateEnabled: v ?? true)),
        ),

        // Dependent checkbox: disabled (greyed out) when main translation feature is off!
        CheckboxListTile(
          value: _draft.imgTranslateAuto,
          enabled: _draft.imgTranslateEnabled,
          title: Text(prov.tr('setImgTransAuto'), style: const TextStyle(fontSize: 13)),
          onChanged: _draft.imgTranslateEnabled
              ? (v) => _updateDraft(_draft.copyWith(imgTranslateAuto: v ?? false))
              : null,
        ),

        const SizedBox(height: 12),
        Row(
          children: [
            Expanded(
              child: DropdownButtonFormField<String>(
                value: _draft.translateProvider,
                isExpanded: true,
                decoration: InputDecoration(
                  labelText: prov.tr('setTransProv'),
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                ),
                items: [
                  DropdownMenuItem(value: 'google', child: Text(prov.tr('provGoogle'))),
                  DropdownMenuItem(value: 'mlkit', child: Text(prov.tr('provMlKit'))),
                  DropdownMenuItem(value: 'local_nllb', child: Text(prov.tr('provLocalNllb'))),
                  DropdownMenuItem(value: 'mymemory', child: Text(prov.tr('provMyMemory'))),
                  DropdownMenuItem(value: 'libre', child: Text(prov.tr('provLibre'))),
                  DropdownMenuItem(value: 'openai', child: Text(prov.tr('provOpenAi'))),
                ],
                onChanged: (v) {
                  if (v != null) _updateDraft(_draft.copyWith(translateProvider: v));
                },
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: DropdownButtonFormField<String>(
                value: _draft.translateTargetLang,
                decoration: InputDecoration(
                  labelText: prov.tr('setTransTarget'),
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                ),
                items: const [
                  DropdownMenuItem(value: 'ru', child: Text('🇷🇺 Русский (ru)')),
                  DropdownMenuItem(value: 'en', child: Text('🇬🇧 English (en)')),
                  DropdownMenuItem(value: 'uk', child: Text('🇺🇦 Українська (uk)')),
                  DropdownMenuItem(value: 'de', child: Text('🇩🇪 Deutsch (de)')),
                  DropdownMenuItem(value: 'es', child: Text('🇪🇸 Español (es)')),
                  DropdownMenuItem(value: 'ja', child: Text('🇯🇵 日本語 (ja)')),
                  DropdownMenuItem(value: 'zh-CN', child: Text('🇨🇳 中文 (zh-CN)')),
                ],
                onChanged: (v) {
                  if (v != null) _updateDraft(_draft.copyWith(translateTargetLang: v));
                },
              ),
            ),
          ],
        ),

        const SizedBox(height: 12),
        Row(
          children: [
            Expanded(
              child: TextField(
                controller: _apiUrlCtrl,
                decoration: InputDecoration(
                  labelText: 'API URL (LibreTranslate / OpenAI)',
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                ),
                onChanged: (v) => _updateDraft(_draft.copyWith(translateApiUrl: v.trim())),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: TextField(
                controller: _apiKeyCtrl,
                obscureText: true,
                decoration: InputDecoration(
                  labelText: 'API Key',
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                ),
                onChanged: (v) => _updateDraft(_draft.copyWith(translateApiKey: v.trim())),
              ),
            ),
          ],
        ),

        const Divider(height: 24),
        // Local NLLB-200 download/delete controls (Download button hidden when installed)
        Row(
          children: [
            Expanded(
              child: Text(
                nllbInstalled
                    ? prov.tr('localTransInstalled')
                    : prov.tr('localTransMissing'),
                style: TextStyle(
                  fontSize: 12.5,
                  color: nllbInstalled ? const Color(0xFF10B981) : Colors.grey,
                ),
              ),
            ),
            if (!nllbInstalled)
              FilledButton.tonal(
                onPressed: prov.nllbDownloadProgress.downloading
                    ? null
                    : () => prov.downloadLocalNllb(),
                child: Text(prov.tr('dlLocalTrans')),
              )
            else
              OutlinedButton(
                style: OutlinedButton.styleFrom(foregroundColor: const Color(0xFFEF4444)),
                onPressed: () async {
                  await prov.deleteLocalNllb();
                  setState(() {});
                },
                child: Text(prov.tr('delLocalTrans')),
              ),
          ],
        ),
        if (prov.nllbDownloadProgress.downloading) ...[
          const SizedBox(height: 8),
          LinearProgressIndicator(value: prov.nllbDownloadProgress.percent / 100.0),
          Text(prov.nllbDownloadProgress.stepText, style: const TextStyle(fontSize: 11.5)),
        ],
      ],
    );
  }

  // TAB 4: 18+ MODE (Dedicated tab)
  Widget _buildNsfwTab(BuildContext context, AppProvider prov) {
    return ListView(
      children: [
        Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            color: const Color(0xFFEF4444).withOpacity(0.1),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: const Color(0xFFEF4444).withOpacity(0.5)),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                prov.tr('descTabNsfw'),
                style: const TextStyle(fontSize: 12.5),
              ),
              const SizedBox(height: 12),
              CheckboxListTile(
                value: _draft.nsfwEnabled,
                title: Text(
                  prov.tr('setNsfwEn'),
                  style: const TextStyle(
                    fontWeight: FontWeight.bold,
                    color: Color(0xFFEF4444),
                  ),
                ),
                onChanged: (v) => _updateDraft(_draft.copyWith(nsfwEnabled: v ?? false)),
              ),
              const SizedBox(height: 8),
              Text(
                prov.tr('nsfwRescanTip'),
                style: const TextStyle(fontSize: 12, color: Colors.grey),
              ),
            ],
          ),
        ),
      ],
    );
  }

  // TAB 5: BACKUP (Second-to-last tab: Cross-platform .json Export & Import)
  Widget _buildBackupTab(BuildContext context, AppProvider prov) {
    return ListView(
      children: [
        Text(
          prov.tr('descTabBackup'),
          style: const TextStyle(fontSize: 12.5, color: Colors.grey),
        ),
        const SizedBox(height: 16),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  prov.tr('setBackupHeader'),
                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13.5),
                ),
                const SizedBox(height: 14),
                Wrap(
                  spacing: 12,
                  runSpacing: 10,
                  children: [
                    FilledButton.icon(
                      onPressed: () => _exportBackupJson(prov),
                      icon: const Icon(Icons.upload_file),
                      label: Text(prov.tr('exportBackup')),
                    ),
                    OutlinedButton.icon(
                      onPressed: () => _importBackupJson(prov),
                      icon: const Icon(Icons.download_for_offline_outlined),
                      label: Text(prov.tr('importBackup')),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }

  // TAB 6: SERVICE (Last tab: Duplicates finder & auto-cleaner, library & cache stats, Full Reset without Debug buttons)
  Widget _buildServiceTab(BuildContext context, AppProvider prov) {
    return ListView(
      children: [
        Text(
          prov.tr('descTabService'),
          style: const TextStyle(fontSize: 12.5, color: Colors.grey),
        ),
        const SizedBox(height: 14),

        // Duplicate groups & auto-clean
        Card(
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  '${prov.tr('setDups')} ${prov.duplicatesCount} (${prov.tr('setWorse')}: ${prov.worseDuplicatesCount})',
                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13),
                ),
                const SizedBox(height: 10),
                Wrap(
                  spacing: 10,
                  runSpacing: 8,
                  children: [
                    OutlinedButton(
                      onPressed: () {
                        prov.setDuplicatesOnly(true);
                        if (!widget.embedded) Navigator.pop(context);
                      },
                      child: Text(prov.tr('showDups')),
                    ),
                    FilledButton.tonal(
                      style: FilledButton.styleFrom(
                        foregroundColor: const Color(0xFFEF4444),
                      ),
                      onPressed: () async {
                        final res = await DatabaseService.instance
                            .autoCleanDuplicates(prov.settings.deleteMode);
                        await prov.refreshState();
                        await _loadStorageStats();
                        if (mounted) {
                          ScaffoldMessenger.of(context).showSnackBar(
                            SnackBar(
                              content: Text(
                                '🧹 -${res['deleted']} (${res['freed_mb']} ${prov.tr('statsMb')})',
                              ),
                            ),
                          );
                        }
                      },
                      child: Text(prov.tr('cleanDups')),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),

        const SizedBox(height: 12),

        // Library & Cache statistics
        Card(
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  prov.tr('statsHeader'),
                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13),
                ),
                const SizedBox(height: 8),
                Text('${prov.tr('statsDir')} ${_storageStats['app_dir'] ?? '—'}'),
                const SizedBox(height: 4),
                Text(
                  '${prov.tr('statsItems')} ${_storageStats['total_items'] ?? prov.totalIndexedCount} (${prov.tr('statsFolders')} ${_storageStats['total_folders'] ?? 0})',
                ),
                const SizedBox(height: 4),
                Text('${prov.tr('statsIgnored')} ${_storageStats['ignored_count'] ?? 0}'),
                const SizedBox(height: 4),
                Text(
                  '${prov.tr('statsCache')} ${_storageStats['cache_mb'] ?? 0} ${prov.tr('statsMb')}',
                ),
              ],
            ),
          ),
        ),

        const SizedBox(height: 16),

        // Full Reset button (without any debug buttons)
        FilledButton.icon(
          style: FilledButton.styleFrom(
            backgroundColor: const Color(0xFF991B1B),
            foregroundColor: Colors.white,
            padding: const EdgeInsets.symmetric(vertical: 12),
          ),
          onPressed: () async {
            final ok = await showDialog<bool>(
              context: context,
              builder: (ctx) => AlertDialog(
                title: Text(prov.tr('confirmResetTitle')),
                content: Text(prov.tr('confirmResetBody')),
                actions: [
                  TextButton(
                    onPressed: () => Navigator.pop(ctx, false),
                    child: Text(prov.tr('btnCancel')),
                  ),
                  FilledButton(
                    style: FilledButton.styleFrom(backgroundColor: const Color(0xFFEF4444)),
                    onPressed: () => Navigator.pop(ctx, true),
                    child: const Text('OK'),
                  ),
                ],
              ),
            );
            if (ok == true) {
              await DatabaseService.instance.fullResetDatabaseAndSettings();
              await prov.initialize();
              await _loadStorageStats();
              if (!widget.embedded && mounted) Navigator.pop(context);
            }
          },
          icon: const Icon(Icons.delete_forever),
          label: Text(prov.tr('fullReset')),
        ),
      ],
    );
  }
}
