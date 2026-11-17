import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../models/models.dart';
import '../../state/app_provider.dart';

class SidebarWidget extends StatelessWidget {
  final String? onlySection; // null = full desktop sidebar; 'folders' or 'tags' for mobile tabs

  const SidebarWidget({super.key, this.onlySection});

  Color _nsBorderColor(String ns) {
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

  Future<void> _promptCreateCategory(BuildContext context, AppProvider prov) async {
    final ctrl = TextEditingController();
    final name = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(prov.tr('secFolders')),
        content: TextField(
          controller: ctrl,
          autofocus: true,
          decoration: InputDecoration(
            hintText: prov.lang == 'en' ? 'New folder name' : 'Название новой папки',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: Text(prov.tr('btnCancel')),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, ctrl.text.trim()),
            child: const Text('OK'),
          ),
        ],
      ),
    );
    if (name != null && name.isNotEmpty) {
      await prov.createNewAppFolder(name);
    }
  }

  Future<void> _promptRenameCategory(
    BuildContext context,
    AppProvider prov,
    AppFolderCategory cat,
  ) async {
    final ctrl = TextEditingController(text: cat.label);
    final newName = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(prov.lang == 'en' ? 'Rename folder' : 'Переименовать папку'),
        content: TextField(controller: ctrl, autofocus: true),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: Text(prov.tr('btnCancel')),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, ctrl.text.trim()),
            child: const Text('OK'),
          ),
        ],
      ),
    );
    if (newName != null && newName.isNotEmpty) {
      await prov.renameAppFolder(cat.key, newName);
    }
  }

  @override
  Widget build(BuildContext context) {
    final prov = context.watch<AppProvider>();
    final showFolders = onlySection == null || onlySection == 'folders';
    final showTags = onlySection == null || onlySection == 'tags';

    final angleTags = prov.sidebarTags.where((t) => t.namespace == 'angle').toList();
    final poseTags = prov.sidebarTags.where((t) => t.namespace == 'pose').toList();
    final detailTags = prov.sidebarTags.where((t) => t.namespace == 'detail').toList();
    final nsfwTags = prov.sidebarTags.where((t) => t.namespace == 'nsfw').toList();
    final charTags = prov.sidebarTags.where((t) => t.namespace == 'char').toList();

    return Container(
      width: onlySection == null ? 300 : double.infinity,
      decoration: BoxDecoration(
        color: Theme.of(context).canvasColor,
        border: onlySection == null
            ? Border(right: BorderSide(color: Theme.of(context).dividerColor))
            : null,
      ),
      child: ListView(
        padding: const EdgeInsets.all(10),
        children: [
          if (showFolders) ...[
            // 1. App Folders (Virtual collections inside DB)
            ExpansionTile(
              initiallyExpanded: true,
              title: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Flexible(
                    child: Text(
                      prov.tr('secFolders'),
                      style: const TextStyle(fontSize: 12, fontWeight: FontWeight.bold),
                    ),
                  ),
                  TextButton.icon(
                    onPressed: () => _promptCreateCategory(context, prov),
                    icon: const Icon(Icons.add, size: 14),
                    label: Text(prov.tr('newCat'), style: const TextStyle(fontSize: 11)),
                  ),
                ],
              ),
              children: prov.folderCategories.map((cat) {
                final isSelected = prov.activeFolderCat == cat.key ||
                    (cat.key == 'general' && prov.activeFolderCat == 'all');
                return ListTile(
                  dense: true,
                  selected: isSelected,
                  selectedTileColor: Theme.of(context).colorScheme.primary.withOpacity(0.18),
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                  title: Text(
                    cat.localizedLabel(prov.lang),
                    style: const TextStyle(fontSize: 13),
                    overflow: TextOverflow.ellipsis,
                  ),
                  trailing: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      if (cat.key != 'general') ...[
                        IconButton(
                          icon: const Icon(Icons.edit_outlined, size: 15),
                          onPressed: () => _promptRenameCategory(context, prov, cat),
                        ),
                        IconButton(
                          icon: const Icon(Icons.delete_outline, size: 15),
                          onPressed: () => prov.deleteAppFolder(cat.key),
                        ),
                      ],
                      Text('${cat.count}', style: const TextStyle(fontSize: 11)),
                    ],
                  ),
                  onTap: () => prov.setFolderCategory(cat.key),
                );
              }).toList(),
            ),

            // 2. Disk Subfolders
            if (prov.diskSubfolders.isNotEmpty)
              ExpansionTile(
                initiallyExpanded: true,
                title: Text(
                  prov.tr('secSubfolders'),
                  style: const TextStyle(fontSize: 12, fontWeight: FontWeight.bold),
                ),
                children: [
                  ListTile(
                    dense: true,
                    selected: prov.activeSubfolder == 'all',
                    title: Text(prov.tr('allDiskFolders'), style: const TextStyle(fontSize: 12.5)),
                    trailing: Text('${prov.totalIndexedCount}', style: const TextStyle(fontSize: 11)),
                    onTap: () => prov.setDiskSubfolder('all'),
                  ),
                  ...prov.diskSubfolders.map(
                    (sf) => ListTile(
                      dense: true,
                      selected: prov.activeSubfolder == sf.name,
                      title: Text('📁 ${sf.name}', style: const TextStyle(fontSize: 12.5)),
                      trailing: Text('${sf.count}', style: const TextStyle(fontSize: 11)),
                      onTap: () => prov.setDiskSubfolder(sf.name),
                    ),
                  ),
                ],
              ),
          ],

          if (showTags) ...[
            if (prov.settings.catAngleEnabled)
              _buildTagSection(context, prov, prov.tr('secAngle'), 'angle', angleTags),
            if (prov.settings.catPoseEnabled)
              _buildTagSection(context, prov, prov.tr('secPose'), 'pose', poseTags),
            if (prov.settings.catDetailEnabled)
              _buildTagSection(context, prov, prov.tr('secDetail'), 'detail', detailTags),
            if (prov.settings.nsfwEnabled)
              _buildTagSection(context, prov, prov.tr('secNsfw'), 'nsfw', nsfwTags),
            if (prov.settings.catCharEnabled && charTags.isNotEmpty)
              _buildTagSection(context, prov, prov.tr('secChar'), 'char', charTags),
          ],
        ],
      ),
    );
  }

  Widget _buildTagSection(
    BuildContext context,
    AppProvider prov,
    String title,
    String ns,
    List<TagCountItem> tags,
  ) {
    final (posTokens, negTokens) = prov.parseSearchQuery(prov.searchQuery);
    final posSet = posTokens.toSet();
    final negSet = negTokens.toSet();

    return ExpansionTile(
      initiallyExpanded: ns == 'detail' || ns == 'angle',
      title: Text(
        '$title (${tags.length})',
        style: const TextStyle(fontSize: 12, fontWeight: FontWeight.bold),
      ),
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 6),
          child: Wrap(
            spacing: 5,
            runSpacing: 5,
            children: tags.map((tItem) {
              final shortLow = tItem.shortName.toLowerCase();
              final isInc = posSet.contains(shortLow) || posSet.contains(tItem.tag.toLowerCase());
              final isExc = negSet.contains(shortLow) || negSet.contains(tItem.tag.toLowerCase());
              final disp = prov.formatTagDisplay(tItem.tag);

              return GestureDetector(
                // Left-click / tap -> include tag in search
                onTap: () => prov.toggleTagInSearch(tItem.shortName, exclude: false),
                // Right-click (Desktop) or Long-press (Android) -> exclude tag (!tag)
                onSecondaryTap: () => prov.toggleTagInSearch(tItem.shortName, exclude: true),
                onLongPress: () => prov.toggleTagInSearch(tItem.shortName, exclude: true),
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                  decoration: BoxDecoration(
                    color: isInc
                        ? Theme.of(context).colorScheme.primary
                        : (isExc ? const Color(0xFF7F1D1D) : Theme.of(context).cardColor),
                    borderRadius: BorderRadius.circular(6),
                    border: Border(
                      left: BorderSide(color: _nsBorderColor(ns), width: 3),
                    ),
                  ),
                  child: Text(
                    '$disp (${tItem.count})',
                    style: TextStyle(
                      fontSize: 11.5,
                      decoration: isExc ? TextDecoration.lineThrough : null,
                      color: isInc || isExc ? Colors.white : null,
                    ),
                  ),
                ),
              );
            }).toList(),
          ),
        ),
      ],
    );
  }
}
