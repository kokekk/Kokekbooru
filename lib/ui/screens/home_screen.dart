import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../models/models.dart';
import '../../state/app_provider.dart';
import '../dialogs/artist_dialogs.dart';
import '../dialogs/inspector_dialog.dart';
import '../dialogs/settings_dialog.dart';
import '../widgets/media_card_widget.dart';
import '../widgets/sidebar_widget.dart';
import '../widgets/topbar_widget.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  final GlobalKey<ScaffoldState> _scaffoldKey = GlobalKey<ScaffoldState>();
  int _mobileNavIndex = 0; // 0: Gallery, 1: Folders, 2: Tags, 3: Settings
  final TextEditingController _bulkTagCtrl = TextEditingController();
  String _selectedBulkFolder = 'general';

  @override
  void dispose() {
    _bulkTagCtrl.dispose();
    super.dispose();
  }

  Future<void> _confirmBulkDelete(AppProvider prov) async {
    if (prov.selectedImageIds.isEmpty) return;
    final count = prov.selectedImageIds.length;
    final isUiOnly = prov.settings.deleteMode == FileDeleteMode.uiOnly;

    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(
          prov.tr(
            isUiOnly ? 'confirmBulkDeleteUiTitle' : 'confirmBulkDeleteDiskTitle',
            {'count': '$count'},
          ),
        ),
        content: Text(
          prov.tr(
            isUiOnly ? 'confirmBulkDeleteUiBody' : 'confirmBulkDeleteDiskBody',
            {'count': '$count'},
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: Text(prov.tr('btnCancel')),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: isUiOnly ? null : const Color(0xFFEF4444),
            ),
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(
              prov.tr(isUiOnly ? 'btnConfirmDeleteUi' : 'btnConfirmDeleteDisk'),
            ),
          ),
        ],
      ),
    );

    if (confirmed == true) {
      await prov.deleteItems(prov.selectedImageIds.toList());
    }
  }

  @override
  Widget build(BuildContext context) {
    final prov = context.watch<AppProvider>();
    final screenWidth = MediaQuery.of(context).size.width;
    final isDesktop = screenWidth > 820;

    return Scaffold(
      key: _scaffoldKey,
      endDrawer: const ArtistNotesDrawer(),
      body: SafeArea(
        child: Column(
          children: [
            // Scan progress bar with ETA, FPS, file counter, and Cancel button
            if (prov.scanProgress.running)
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
                color: Theme.of(context).canvasColor,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [
                        Expanded(
                          child: Text(
                            '${prov.scanProgress.stage} (${prov.scanProgress.percent.toStringAsFixed(1)}%) — ${prov.scanProgress.currentFile}',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.bold),
                          ),
                        ),
                        Text(
                          'ETA: ~${prov.scanProgress.etaSeconds}s • ${prov.scanProgress.speedFps} fps • ${prov.scanProgress.current}/${prov.scanProgress.total}',
                          style: const TextStyle(fontSize: 12, color: Color(0xFF38BDF8)),
                        ),
                        const SizedBox(width: 10),
                        FilledButton.tonal(
                          style: FilledButton.styleFrom(
                            backgroundColor: const Color(0xFF991B1B),
                            foregroundColor: Colors.white,
                          ),
                          onPressed: prov.cancelScan,
                          child: Text(prov.tr('cancelScan')),
                        ),
                      ],
                    ),
                    const SizedBox(height: 6),
                    LinearProgressIndicator(value: prov.scanProgress.percent / 100.0),
                  ],
                ),
              ),

            // Topbar (shown on Desktop or on Mobile Gallery tab)
            if (isDesktop || _mobileNavIndex == 0)
              TopbarWidget(
                isDesktop: isDesktop,
                onOpenNotesDrawer: () => _scaffoldKey.currentState?.openEndDrawer(),
              ),

            // Bulk Action Bar when bulkMode is active
            if (prov.bulkMode)
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                decoration: BoxDecoration(
                  color: Theme.of(context).canvasColor,
                  border: Border(
                    bottom: BorderSide(color: Theme.of(context).colorScheme.primary, width: 2),
                  ),
                ),
                child: Wrap(
                  spacing: 8,
                  runSpacing: 6,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    Chip(
                      label: Text('${prov.tr('bulkSelected')}: ${prov.selectedImageIds.length}'),
                    ),
                    OutlinedButton(
                      onPressed: prov.selectAllVisible,
                      child: Text('${prov.tr('bulkAll')} (${prov.visibleImages.length})'),
                    ),
                    OutlinedButton(
                      onPressed: prov.clearSelection,
                      child: Text(prov.tr('bulkClear')),
                    ),
                    OutlinedButton(
                      onPressed: () => prov.bulkFavorite(true),
                      child: Text(prov.tr('bulkFavAdd')),
                    ),
                    OutlinedButton(
                      onPressed: () => prov.bulkFavorite(false),
                      child: Text(prov.tr('bulkFavRem')),
                    ),
                    DropdownButton<String>(
                      value: prov.folderCategories.any((c) => c.key == _selectedBulkFolder)
                          ? _selectedBulkFolder
                          : 'general',
                      isDense: true,
                      items: prov.folderCategories
                          .map((c) => DropdownMenuItem(
                                value: c.key,
                                child: Text(c.localizedLabel(prov.lang)),
                              ))
                          .toList(),
                      onChanged: (v) {
                        if (v != null) setState(() => _selectedBulkFolder = v);
                      },
                    ),
                    FilledButton.tonal(
                      onPressed: () => prov.bulkMoveToFolder(_selectedBulkFolder),
                      child: Text(prov.tr('bulkMove')),
                    ),
                    SizedBox(
                      width: 140,
                      child: TextField(
                        controller: _bulkTagCtrl,
                        isDense: true,
                        decoration: InputDecoration(
                          hintText: prov.tr('bulkTagPh'),
                          border: OutlineInputBorder(borderRadius: BorderRadius.circular(6)),
                        ),
                      ),
                    ),
                    FilledButton.tonal(
                      onPressed: () async {
                        if (_bulkTagCtrl.text.trim().isNotEmpty) {
                          await prov.bulkAddTag(_bulkTagCtrl.text.trim());
                          _bulkTagCtrl.clear();
                        }
                      },
                      child: Text(prov.tr('bulkTagBtn')),
                    ),
                    FilledButton(
                      style: FilledButton.styleFrom(backgroundColor: const Color(0xFF991B1B)),
                      onPressed: () => _confirmBulkDelete(prov),
                      child: Text(prov.tr('bulkDeleteBtn')),
                    ),
                  ],
                ),
              ),

            // Main workspace area
            Expanded(
              child: isDesktop
                  ? Row(
                      children: [
                        const SidebarWidget(),
                        Expanded(child: _buildGalleryGrid(context, prov, isDesktop: true)),
                      ],
                    )
                  : _buildMobileBody(context, prov),
            ),
          ],
        ),
      ),

      // Android / Mobile BottomNavigationBar (Gallery, Folders, Tags, Settings)
      bottomNavigationBar: isDesktop
          ? null
          : BottomNavigationBar(
              currentIndex: _mobileNavIndex,
              type: BottomNavigationBarType.fixed,
              selectedItemColor: Theme.of(context).colorScheme.primary,
              onTap: (idx) => setState(() => _mobileNavIndex = idx),
              items: [
                BottomNavigationBarItem(
                  icon: const Icon(Icons.grid_view_rounded),
                  label: prov.tr('navGallery'),
                ),
                BottomNavigationBarItem(
                  icon: const Icon(Icons.folder_outlined),
                  label: prov.tr('navFolders'),
                ),
                BottomNavigationBarItem(
                  icon: const Icon(Icons.sell_outlined),
                  label: prov.tr('navTags'),
                ),
                BottomNavigationBarItem(
                  icon: const Icon(Icons.settings_outlined),
                  label: prov.tr('navSettings'),
                ),
              ],
            ),
    );
  }

  Widget _buildMobileBody(BuildContext context, AppProvider prov) {
    switch (_mobileNavIndex) {
      case 1:
        return const SidebarWidget(onlySection: 'folders');
      case 2:
        return const SidebarWidget(onlySection: 'tags');
      case 3:
        return const SettingsDialog(embedded: true);
      case 0:
      default:
        return _buildGalleryGrid(context, prov, isDesktop: false);
    }
  }

  Widget _buildGalleryGrid(
    BuildContext context,
    AppProvider prov, {
    required bool isDesktop,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
          child: Text(
            prov.duplicatesOnly
                ? '${prov.tr('dupBanner')}: ${prov.visibleImages.length}'
                : '${prov.tr('shownOf')}: ${prov.visibleImages.length} ${prov.tr('of')} ${prov.totalIndexedCount}',
            style: const TextStyle(fontSize: 12, color: Colors.grey),
          ),
        ),
        Expanded(
          child: prov.visibleImages.isEmpty
              ? Center(
                  child: Padding(
                    padding: const EdgeInsets.all(28),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        const Text('🐔', style: TextStyle(fontSize: 48)),
                        const SizedBox(height: 10),
                        Text(
                          prov.tr('emptyTitle'),
                          style: const TextStyle(fontSize: 17, fontWeight: FontWeight.bold),
                        ),
                        const SizedBox(height: 6),
                        Text(
                          prov.tr('emptySub'),
                          textAlign: TextAlign.center,
                          style: const TextStyle(color: Colors.grey),
                        ),
                      ],
                    ),
                  ),
                )
              : GridView.builder(
                  padding: const EdgeInsets.all(12),
                  gridDelegate: isDesktop
                      ? const SliverGridDelegateWithMaxCrossAxisExtent(
                          maxCrossAxisExtent: 240,
                          mainAxisSpacing: 12,
                          crossAxisSpacing: 12,
                          childAspectRatio: 0.78,
                        )
                      : SliverGridDelegateWithFixedCrossAxisCount(
                          crossAxisCount: MediaQuery.of(context).size.width > 520 ? 3 : 2,
                          mainAxisSpacing: 8,
                          crossAxisSpacing: 8,
                          childAspectRatio: 0.76,
                        ),
                  itemCount: prov.visibleImages.length,
                  itemBuilder: (ctx, idx) {
                    final item = prov.visibleImages[idx];
                    return MediaCardWidget(
                      item: item,
                      onOpenInspector: () => showDialog(
                        context: context,
                        builder: (_) => InspectorDialog(initialIndex: idx),
                      ),
                    );
                  },
                ),
        ),
      ],
    );
  }
}
