import 'dart:io';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../state/app_provider.dart';
import '../dialogs/artist_dialogs.dart';
import '../dialogs/settings_dialog.dart';

class TopbarWidget extends StatefulWidget {
  final bool isDesktop;
  final VoidCallback onOpenNotesDrawer;

  const TopbarWidget({
    super.key,
    required this.isDesktop,
    required this.onOpenNotesDrawer,
  });

  @override
  State<TopbarWidget> createState() => _TopbarWidgetState();
}

class _TopbarWidgetState extends State<TopbarWidget> {
  late TextEditingController _searchCtrl;
  late TextEditingController _folderCtrl;
  final FocusNode _searchFocus = FocusNode();

  @override
  void initState() {
    super.initState();
    final prov = context.read<AppProvider>();
    _searchCtrl = TextEditingController(text: prov.searchQuery);
    _folderCtrl = TextEditingController(text: prov.currentScanFolderPath);
  }

  @override
  void dispose() {
    _searchCtrl.dispose();
    _folderCtrl.dispose();
    _searchFocus.dispose();
    super.dispose();
  }

  Color _namespaceColor(String ns) {
    switch (ns) {
      case 'angle':
        return const Color(0xFF3B82F6);
      case 'pose':
        return const Color(0xFFEC4899);
      case 'nsfw':
        return const Color(0xFFEF4444);
      case 'char':
        return const Color(0xFFF59E0B);
      default:
        return const Color(0xFF10B981);
    }
  }

  void _applyAutocompleteSuggestion(AppProvider prov, AutocompleteSuggestion s) {
    final current = _searchCtrl.text;
    final lastComma = current.lastIndexOf(',');
    final lastSpace = current.lastIndexOf(' ');
    final splitIdx = lastComma > lastSpace ? lastComma : lastSpace;
    final prefix = splitIdx >= 0 ? '${current.substring(0, splitIdx + 1)} ' : '';
    final tail = splitIdx >= 0 ? current.substring(splitIdx + 1).trimLeft() : current.trimLeft();
    final negPrefix = (tail.startsWith('!') || tail.startsWith('-')) ? tail[0] : '';

    final updated = '$prefix$negPrefix${s.shortName}, ';
    _searchCtrl.text = updated;
    _searchCtrl.selection = TextSelection.fromPosition(TextPosition(offset: updated.length));
    prov.setSearchQuery(updated);
    setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final prov = context.watch<AppProvider>();
    if (_searchCtrl.text != prov.searchQuery && !_searchFocus.hasFocus) {
      _searchCtrl.text = prov.searchQuery;
    }
    if (_folderCtrl.text != prov.currentScanFolderPath) {
      _folderCtrl.text = prov.currentScanFolderPath;
    }

    final suggestions = _searchFocus.hasFocus
        ? prov.getAutocompleteMatches(_searchCtrl.text)
        : const <AutocompleteSuggestion>[];

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
          decoration: BoxDecoration(
            color: Theme.of(context).canvasColor,
            border: Border(
              bottom: BorderSide(color: Theme.of(context).dividerColor),
            ),
          ),
          child: Wrap(
            spacing: 6,
            runSpacing: 6,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              // Smart Booru Search bar with '?' FAQ button inside
              SizedBox(
                width: widget.isDesktop ? 280 : MediaQuery.of(context).size.width - 24,
                child: TextField(
                  controller: _searchCtrl,
                  focusNode: _searchFocus,
                  style: const TextStyle(fontSize: 13),
                  decoration: InputDecoration(
                    hintText: prov.tr('searchPh'),
                    isDense: true,
                    contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
                    border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                    suffixIcon: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        if (_searchCtrl.text.isNotEmpty)
                          IconButton(
                            icon: const Icon(Icons.clear, size: 16),
                            padding: EdgeInsets.zero,
                            constraints: const BoxConstraints(minWidth: 26, minHeight: 26),
                            onPressed: () {
                              _searchCtrl.clear();
                              prov.setSearchQuery('');
                              setState(() {});
                            },
                          ),
                        IconButton(
                          icon: const Icon(Icons.help_outline, size: 17),
                          tooltip: prov.tr('faqModalTitle'),
                          padding: EdgeInsets.zero,
                          constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
                          onPressed: () => showDialog(
                            context: context,
                            builder: (_) => const SearchFaqDialog(),
                          ),
                        ),
                      ],
                    ),
                  ),
                  onChanged: (val) {
                    prov.setSearchQuery(val);
                    setState(() {});
                  },
                ),
              ),

              // Folder path & Scan controls
              if (widget.isDesktop)
                SizedBox(
                  width: 190,
                  child: TextField(
                    controller: _folderCtrl,
                    style: const TextStyle(fontSize: 12.5),
                    decoration: InputDecoration(
                      hintText: prov.tr('folderPh'),
                      isDense: true,
                      contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
                      border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                    ),
                    onChanged: (v) => prov.currentScanFolderPath = v,
                  ),
                ),

              Tooltip(
                message: prov.tr('pickFolderTooltip'),
                child: OutlinedButton(
                  onPressed: () async {
                    final picked = await prov.pickFolderForScan();
                    if (picked != null) {
                      _folderCtrl.text = picked;
                    }
                  },
                  child: const Text('📁'),
                ),
              ),

              Tooltip(
                message: prov.tr('scanNewTooltip'),
                child: FilledButton(
                  onPressed: prov.scanProgress.running
                      ? null
                      : () => prov.startFolderScan(forceRescan: false),
                  child: Text(prov.tr('scanNew')),
                ),
              ),

              Tooltip(
                message: prov.tr('rescanTooltip'),
                child: OutlinedButton(
                  onPressed: prov.scanProgress.running
                      ? null
                      : () => prov.startFolderScan(forceRescan: true),
                  child: Text(widget.isDesktop ? prov.tr('rescan') : prov.tr('rescanShort')),
                ),
              ),

              // Dedicated Android Gallery multi-pick button ("🖼 Из галереи")
              if (!widget.isDesktop || Platform.isAndroid)
                Tooltip(
                  message: prov.tr('pickGalleryTooltip'),
                  child: FilledButton.tonalIcon(
                    onPressed: prov.scanProgress.running
                        ? null
                        : () => prov.pickIndividualMediaFromAndroidGallery(),
                    icon: const Icon(Icons.photo_library_outlined, size: 16),
                    label: Text(prov.tr('pickGalleryAndroid')),
                  ),
                ),

              // Media type segmented filter (Photos / All / Videos)
              if (!prov.settings.disableVideos)
                SegmentedButton<String>(
                  segments: [
                    ButtonSegment(value: 'images', label: Text(prov.tr('segImages'))),
                    ButtonSegment(value: 'all', label: Text(prov.tr('segAll'))),
                    ButtonSegment(value: 'videos', label: Text(prov.tr('segVideos'))),
                  ],
                  selected: {prov.activeMediaFilter},
                  showSelectedIcon: false,
                  style: const ButtonStyle(
                    visualDensity: VisualDensity.compact,
                    tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                  ),
                  onSelectionChanged: (s) => prov.setMediaFilter(s.first),
                ),

              // Sort dropdown + Random Shuffle button
              DropdownButton<String>(
                value: prov.activeSort,
                isDense: true,
                underline: const SizedBox.shrink(),
                items: [
                  DropdownMenuItem(value: 'newest', child: Text(prov.tr('sortNewest'))),
                  DropdownMenuItem(value: 'oldest', child: Text(prov.tr('sortOldest'))),
                  DropdownMenuItem(value: 'name_asc', child: Text(prov.tr('sortNameAsc'))),
                  DropdownMenuItem(value: 'name_desc', child: Text(prov.tr('sortNameDesc'))),
                  DropdownMenuItem(value: 'res_desc', child: Text(prov.tr('sortResDesc'))),
                  DropdownMenuItem(value: 'res_asc', child: Text(prov.tr('sortResAsc'))),
                  DropdownMenuItem(value: 'size_desc', child: Text(prov.tr('sortSizeDesc'))),
                  DropdownMenuItem(value: 'random', child: Text(prov.tr('sortRandom'))),
                ],
                onChanged: (v) {
                  if (v != null) prov.setSortOrder(v);
                },
              ),

              IconButton(
                tooltip: prov.tr('shuffleTooltip'),
                onPressed: prov.shuffleRandomOrder,
                icon: const Text('🔀'),
              ),

              // Artist Mode tools: Aspect Ratio filter, Idea Roulette, Artist Notebook
              if (prov.settings.artistMode) ...[
                DropdownButton<String>(
                  value: prov.activeAspect,
                  isDense: true,
                  underline: const SizedBox.shrink(),
                  items: [
                    DropdownMenuItem(value: 'all', child: Text(prov.tr('aspAll'))),
                    DropdownMenuItem(value: 'portrait', child: Text(prov.tr('aspPort'))),
                    DropdownMenuItem(value: 'landscape', child: Text(prov.tr('aspLand'))),
                    DropdownMenuItem(value: 'square', child: Text(prov.tr('aspSq'))),
                  ],
                  onChanged: (v) {
                    if (v != null) prov.setAspectFilter(v);
                  },
                ),
                OutlinedButton(
                  onPressed: () => showDialog(
                    context: context,
                    builder: (_) => const IdeaRouletteDialog(),
                  ),
                  child: Text(prov.tr('roulette')),
                ),
                OutlinedButton(
                  onPressed: widget.onOpenNotesDrawer,
                  child: Text(prov.tr('notesBtn')),
                ),
              ],

              // Favorites filter button
              FilterChip(
                selected: prov.favoritesOnly,
                label: Text('❤️ ${prov.tr('fav')} (${prov.favoritesCount})'),
                onSelected: (_) => prov.toggleFavoritesOnly(),
              ),

              // Bulk selection toggle button
              FilterChip(
                selected: prov.bulkMode,
                label: Text('☑️ ${prov.tr('bulk')}'),
                onSelected: (_) => prov.toggleBulkMode(),
              ),

              if (prov.hasActiveFilters)
                TextButton(
                  onPressed: () {
                    _searchCtrl.clear();
                    prov.resetAllFilters();
                  },
                  child: Text(prov.tr('reset')),
                ),

              if (widget.isDesktop)
                OutlinedButton(
                  onPressed: () => showDialog(
                    context: context,
                    builder: (_) => const SettingsDialog(),
                  ),
                  child: Text(prov.tr('settings')),
                ),
            ],
          ),
        ),

        // Booru Autocomplete Dropdown
        if (suggestions.isNotEmpty)
          Container(
            width: double.infinity,
            constraints: const BoxConstraints(maxHeight: 250),
            decoration: BoxDecoration(
              color: Theme.of(context).canvasColor,
              border: Border(
                bottom: BorderSide(color: Theme.of(context).colorScheme.primary, width: 1.5),
              ),
            ),
            child: ListView.builder(
              shrinkWrap: true,
              itemCount: suggestions.length,
              itemBuilder: (context, idx) {
                final s = suggestions[idx];
                final showRu = prov.lang == 'ru' && prov.settings.tagsInRussian;
                final primaryLabel = showRu ? s.ruName : s.shortName;
                final secondaryLabel =
                    (showRu && s.ruName.toLowerCase() != s.shortName.toLowerCase())
                        ? ' (${s.shortName})'
                        : '';
                return ListTile(
                  dense: true,
                  visualDensity: VisualDensity.compact,
                  leading: CircleAvatar(
                    radius: 5,
                    backgroundColor: _namespaceColor(s.namespace),
                  ),
                  title: Text.rich(
                    TextSpan(
                      children: [
                        TextSpan(
                          text: primaryLabel,
                          style: TextStyle(
                            fontWeight: FontWeight.w600,
                            color: _namespaceColor(s.namespace),
                          ),
                        ),
                        if (secondaryLabel.isNotEmpty)
                          TextSpan(
                            text: secondaryLabel,
                            style: const TextStyle(fontSize: 11, color: Colors.grey),
                          ),
                      ],
                    ),
                  ),
                  trailing: Text(
                    '(${s.count})',
                    style: const TextStyle(fontSize: 11, color: Colors.grey),
                  ),
                  onTap: () => _applyAutocompleteSuggestion(prov, s),
                );
              },
            ),
          ),
      ],
    );
  }
}
