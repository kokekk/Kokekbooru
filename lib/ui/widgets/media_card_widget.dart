import 'dart:io';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../models/models.dart';
import '../../services/scanner_service.dart';
import '../../state/app_provider.dart';

class MediaCardWidget extends StatefulWidget {
  final MediaItem item;
  final VoidCallback onOpenInspector;

  const MediaCardWidget({
    super.key,
    required this.item,
    required this.onOpenInspector,
  });

  @override
  State<MediaCardWidget> createState() => _MediaCardWidgetState();
}

class _MediaCardWidgetState extends State<MediaCardWidget> {
  bool _hovered = false;

  static const List<double> _grayscaleMatrix = <double>[
    0.2126, 0.7152, 0.0722, 0, 0,
    0.2126, 0.7152, 0.0722, 0, 0,
    0.2126, 0.7152, 0.0722, 0, 0,
    0,      0,      0,      1, 0,
  ];

  @override
  Widget build(BuildContext context) {
    final prov = context.watch<AppProvider>();
    final item = widget.item;
    final isSelected = prov.selectedImageIds.contains(item.id);
    final thumbFile = ScannerService.instance.getThumbnailFileForPath(item.path);
    final displayFile = thumbFile.existsSync() ? thumbFile : File(item.path);

    Widget imageWidget = Image.file(
      displayFile,
      fit: BoxFit.contain,
      errorBuilder: (_, __, ___) => const Center(
        child: Icon(Icons.broken_image_outlined, size: 36, color: Colors.grey),
      ),
    );

    if (item.flipH) {
      imageWidget = Transform.scale(scaleX: -1, child: imageWidget);
    }
    if (item.grayscale) {
      imageWidget = ColorFiltered(
        colorFilter: const ColorFilter.matrix(_grayscaleMatrix),
        child: imageWidget,
      );
    }

    return MouseRegion(
      onEnter: (_) => setState(() => _hovered = true),
      onExit: (_) => setState(() => _hovered = false),
      child: GestureDetector(
        onTap: () {
          if (prov.bulkMode) {
            prov.toggleSelectImage(item.id);
          } else {
            widget.onOpenInspector();
          }
        },
        onLongPress: () {
          if (!prov.bulkMode) {
            prov.toggleBulkMode();
          }
          prov.toggleSelectImage(item.id);
        },
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 140),
          decoration: BoxDecoration(
            color: Theme.of(context).cardColor,
            borderRadius: BorderRadius.circular(10),
            border: Border.all(
              color: isSelected
                  ? const Color(0xFF38BDF8)
                  : (_hovered
                      ? Theme.of(context).colorScheme.primary
                      : (item.isFavorite
                          ? const Color(0xFFEF4444).withOpacity(0.7)
                          : Theme.of(context).dividerColor)),
              width: isSelected ? 2.2 : 1.0,
            ),
          ),
          clipBehavior: Clip.antiAlias,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              // Thumbnail area
              Expanded(
                child: Stack(
                  fit: StackFit.expand,
                  children: [
                    Container(
                      color: const Color(0xFF08090D),
                      child: imageWidget,
                    ),

                    // Video badge or Duplicate group badge
                    Positioned(
                      top: 6,
                      left: 6,
                      child: Wrap(
                        spacing: 4,
                        children: [
                          if (item.isVideo)
                            Container(
                              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                              decoration: BoxDecoration(
                                color: const Color(0xFF7C5CFF).withOpacity(0.92),
                                borderRadius: BorderRadius.circular(4),
                              ),
                              child: Text(
                                '🎬 ${prov.tr('videoTag')}',
                                style: const TextStyle(
                                  fontSize: 10,
                                  fontWeight: FontWeight.bold,
                                  color: Colors.white,
                                ),
                              ),
                            ),
                          if (item.dupGroup != null)
                            Container(
                              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                              decoration: BoxDecoration(
                                color: item.isWorseDup
                                    ? const Color(0xFFEF4444)
                                    : const Color(0xFF10B981),
                                borderRadius: BorderRadius.circular(4),
                              ),
                              child: Text(
                                item.dupGroup!,
                                style: const TextStyle(
                                  fontSize: 10,
                                  fontWeight: FontWeight.bold,
                                  color: Colors.white,
                                ),
                              ),
                            ),
                        ],
                      ),
                    ),

                    // Favorite heart or Bulk selection checkbox
                    Positioned(
                      top: 6,
                      right: 6,
                      child: prov.bulkMode
                          ? Container(
                              width: 24,
                              height: 24,
                              decoration: BoxDecoration(
                                color: isSelected
                                    ? const Color(0xFF0284C7)
                                    : Colors.black54,
                                borderRadius: BorderRadius.circular(6),
                                border: Border.all(color: Colors.white70),
                              ),
                              child: isSelected
                                  ? const Icon(Icons.check, size: 16, color: Colors.white)
                                  : null,
                            )
                          : InkWell(
                              onTap: () => prov.toggleItemFavorite(item),
                              child: Container(
                                width: 28,
                                height: 28,
                                decoration: BoxDecoration(
                                  color: Colors.black54,
                                  shape: BoxShape.circle,
                                  border: Border.all(
                                    color: item.isFavorite
                                        ? const Color(0xFFEF4444)
                                        : Colors.white24,
                                  ),
                                ),
                                alignment: Alignment.center,
                                child: Text(
                                  item.isFavorite ? '❤️' : '🤍',
                                  style: const TextStyle(fontSize: 13),
                                ),
                              ),
                            ),
                    ),

                    // Quick Artist Mode buttons on card hover (Flip H / Grayscale)
                    if (prov.settings.artistMode && !item.isVideo && _hovered)
                      Positioned(
                        bottom: 4,
                        right: 4,
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            IconButton.filledTonal(
                              visualDensity: VisualDensity.compact,
                              tooltip: prov.tr('inspFlip'),
                              onPressed: () => prov.toggleItemFlip(item),
                              icon: const Icon(Icons.flip, size: 15),
                            ),
                            const SizedBox(width: 4),
                            IconButton.filledTonal(
                              visualDensity: VisualDensity.compact,
                              tooltip: prov.tr('inspGray'),
                              onPressed: () => prov.toggleItemGrayscale(item),
                              icon: const Icon(Icons.contrast, size: 15),
                            ),
                          ],
                        ),
                      ),
                  ],
                ),
              ),

              // Card metadata & clickable tag chips
              Padding(
                padding: const EdgeInsets.all(7),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '${item.width}×${item.height} • ${item.filename}',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontSize: 11, color: Colors.grey),
                    ),
                    const SizedBox(height: 4),
                    SizedBox(
                      height: 38,
                      child: SingleChildScrollView(
                        child: Wrap(
                          spacing: 4,
                          runSpacing: 3,
                          children: item.tags.take(6).map((tg) {
                            final isNsfw = tg.startsWith('nsfw:');
                            return InkWell(
                              onTap: () => prov.toggleTagInSearch(tg, exclude: false),
                              onLongPress: () => prov.toggleTagInSearch(tg, exclude: true),
                              child: Container(
                                padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1.5),
                                decoration: BoxDecoration(
                                  color: Theme.of(context).scaffoldBackgroundColor,
                                  borderRadius: BorderRadius.circular(4),
                                  border: Border.all(
                                    color: isNsfw
                                        ? const Color(0xFFEF4444).withOpacity(0.55)
                                        : Theme.of(context).dividerColor,
                                  ),
                                ),
                                child: Text(
                                  prov.formatTagDisplay(tg),
                                  style: TextStyle(
                                    fontSize: 10,
                                    color: isNsfw ? const Color(0xFFEF4444) : null,
                                  ),
                                ),
                              ),
                            );
                          }).toList(),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
