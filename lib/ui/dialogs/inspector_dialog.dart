import 'dart:io';
import 'package:auto_size_text/auto_size_text.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../../models/models.dart';
import '../../services/ocr_translation_service.dart';
import '../../state/app_provider.dart';

class InspectorDialog extends StatefulWidget {
  final int initialIndex;

  const InspectorDialog({super.key, required this.initialIndex});

  @override
  State<InspectorDialog> createState() => _InspectorDialogState();
}

class _InspectorDialogState extends State<InspectorDialog> {
  late int _currentIndex;
  late PageController _pageController;
  final TransformationController _zoomCtrl = TransformationController();
  final TextEditingController _newTagCtrl = TextEditingController();
  final TextEditingController _noteCtrl = TextEditingController();

  bool _isFullscreen = false;
  bool _ocrLoading = false;
  bool _ocrVisible = false;
  List<OcrBubbleBlock> _ocrBlocks = [];

  static const List<double> _grayscaleMatrix = <double>[
    0.2126, 0.7152, 0.0722, 0, 0,
    0.2126, 0.7152, 0.0722, 0, 0,
    0.2126, 0.7152, 0.0722, 0, 0,
    0,      0,      0,      1, 0,
  ];

  @override
  void initState() {
    super.initState();
    _currentIndex = widget.initialIndex;
    _pageController = PageController(initialPage: _currentIndex);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _syncItemStateAndAutoTranslate();
    });
  }

  @override
  void dispose() {
    _pageController.dispose();
    _zoomCtrl.dispose();
    _newTagCtrl.dispose();
    _noteCtrl.dispose();
    super.dispose();
  }

  MediaItem? _currentItem(AppProvider prov) {
    if (prov.visibleImages.isEmpty) return null;
    final idx = _currentIndex.clamp(0, prov.visibleImages.length - 1);
    return prov.visibleImages[idx];
  }

  Future<void> _syncItemStateAndAutoTranslate() async {
    if (!mounted) return;
    final prov = context.read<AppProvider>();
    final item = _currentItem(prov);
    if (item == null) return;
    _noteCtrl.text = item.notes;
    _zoomCtrl.value = Matrix4.identity();
    setState(() {
      _ocrVisible = false;
      _ocrBlocks = [];
    });

    if (prov.settings.imgTranslateEnabled &&
        prov.settings.imgTranslateAuto &&
        !item.isVideo) {
      await _runOcrTranslation(forceRefresh: false);
    }
  }

  Future<void> _runOcrTranslation({required bool forceRefresh}) async {
    final prov = context.read<AppProvider>();
    final item = _currentItem(prov);
    if (item == null || !prov.settings.imgTranslateEnabled) return;

    if (!forceRefresh && _ocrVisible) {
      setState(() {
        _ocrVisible = false;
      });
      return;
    }

    setState(() {
      _ocrLoading = true;
      _ocrVisible = true;
    });

    try {
      final blocks = await OcrTranslationService.instance.recognizeAndTranslateImage(
        item: item,
        settings: prov.settings,
        forceRefresh: forceRefresh,
      );
      if (mounted) {
        setState(() {
          _ocrBlocks = blocks;
          _ocrLoading = false;
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          _ocrLoading = false;
        });
      }
    }
  }

  Color _parseHexColor(String hex, Color fallback) {
    final clean = hex.replaceAll('#', '').trim();
    if (clean.length == 6) {
      final val = int.tryParse('FF$clean', radix: 16);
      if (val != null) return Color(val);
    }
    return fallback;
  }

  Future<void> _confirmAndDeleteCurrent(AppProvider prov, MediaItem item) async {
    final isUiOnly = prov.settings.deleteMode == FileDeleteMode.uiOnly;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(
          prov.tr(isUiOnly ? 'confirmDeleteUiTitle' : 'confirmDeleteDiskTitle'),
        ),
        content: Text(
          prov.tr(
            isUiOnly ? 'confirmDeleteUiBody' : 'confirmDeleteDiskBody',
            {'name': item.filename},
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
      await prov.deleteItems([item.id]);
      if (!mounted) return;
      if (prov.visibleImages.isEmpty) {
        Navigator.pop(context);
      } else {
        setState(() {
          _currentIndex = _currentIndex.clamp(0, prov.visibleImages.length - 1);
        });
        _syncItemStateAndAutoTranslate();
      }
    }
  }

  void _navigateDelta(AppProvider prov, int delta) {
    if (prov.visibleImages.isEmpty) return;
    final nextIdx =
        (_currentIndex + delta + prov.visibleImages.length) % prov.visibleImages.length;
    setState(() {
      _currentIndex = nextIdx;
    });
    if (_pageController.hasClients) {
      _pageController.jumpToPage(nextIdx);
    }
    _syncItemStateAndAutoTranslate();
  }

  @override
  Widget build(BuildContext context) {
    final prov = context.watch<AppProvider>();
    final item = _currentItem(prov);
    if (item == null) {
      return const SizedBox.shrink();
    }

    final width = MediaQuery.of(context).size.width;
    final isDesktop = width > 820;

    return Focus(
      autofocus: true,
      onKeyEvent: (node, event) {
        if (event is KeyDownEvent) {
          if (event.logicalKey == LogicalKeyboardKey.arrowLeft) {
            _navigateDelta(prov, -1);
            return KeyEventResult.handled;
          } else if (event.logicalKey == LogicalKeyboardKey.arrowRight) {
            _navigateDelta(prov, 1);
            return KeyEventResult.handled;
          }
        }
        return KeyEventResult.ignored;
      },
      child: Dialog(
        insetPadding: isDesktop && !_isFullscreen
            ? const EdgeInsets.all(24)
            : EdgeInsets.zero,
        clipBehavior: Clip.antiAlias,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(isDesktop && !_isFullscreen ? 12 : 0),
        ),
        child: SizedBox(
          width: isDesktop && !_isFullscreen ? 1260 : double.infinity,
          height: isDesktop && !_isFullscreen
              ? MediaQuery.of(context).size.height * 0.88
              : double.infinity,
          child: isDesktop
              ? Row(
                  children: [
                    Expanded(child: _buildCanvasArea(context, prov, item, isDesktop: true)),
                    if (!_isFullscreen)
                      SizedBox(
                        width: 360,
                        child: _buildInspectorPanel(context, prov, item),
                      ),
                  ],
                )
              : Stack(
                  children: [
                    Positioned.fill(
                      child: PageView.builder(
                        controller: _pageController,
                        itemCount: prov.visibleImages.length,
                        onPageChanged: (idx) {
                          setState(() => _currentIndex = idx);
                          _syncItemStateAndAutoTranslate();
                        },
                        itemBuilder: (ctx, idx) {
                          final pageItem = prov.visibleImages[idx];
                          return _buildCanvasArea(ctx, prov, pageItem, isDesktop: false);
                        },
                      ),
                    ),
                    // Bottom pull-up DraggableScrollableSheet for mobile tags & OCR controls
                    DraggableScrollableSheet(
                      initialChildSize: 0.24,
                      minChildSize: 0.12,
                      maxChildSize: 0.82,
                      builder: (ctx, scrollCtrl) {
                        return Container(
                          decoration: BoxDecoration(
                            color: Theme.of(context).canvasColor,
                            borderRadius: const BorderRadius.vertical(top: Radius.circular(16)),
                            boxShadow: const [
                              BoxShadow(color: Colors.black54, blurRadius: 12),
                            ],
                          ),
                          child: _buildInspectorPanel(
                            ctx,
                            prov,
                            item,
                            scrollController: scrollCtrl,
                          ),
                        );
                      },
                    ),
                  ],
                ),
        ),
      ),
    );
  }

  Widget _buildCanvasArea(
    BuildContext context,
    AppProvider prov,
    MediaItem item, {
    required bool isDesktop,
  }) {
    return Container(
      color: const Color(0xFF08090D),
      child: Stack(
        fit: StackFit.expand,
        children: [
          // InteractiveViewer with Bubble Overlay matched to image aspect bounds
          LayoutBuilder(
            builder: (ctx, constraints) {
              final imgW = (item.width > 0 ? item.width : 1000).toDouble();
              final imgH = (item.height > 0 ? item.height : 1000).toDouble();
              final scale = (constraints.maxWidth / imgW) < (constraints.maxHeight / imgH)
                  ? (constraints.maxWidth / imgW)
                  : (constraints.maxHeight / imgH);
              final dispW = imgW * scale;
              final dispH = imgH * scale;

              Widget imgChild = Image.file(
                File(item.path),
                width: dispW,
                height: dispH,
                fit: BoxFit.contain,
              );
              if (item.flipH) {
                imgChild = Transform.scale(scaleX: -1, child: imgChild);
              }
              if (item.grayscale) {
                imgChild = ColorFiltered(
                  colorFilter: const ColorFilter.matrix(_grayscaleMatrix),
                  child: imgChild,
                );
              }

              return InteractiveViewer(
                transformationController: _zoomCtrl,
                minScale: 0.5,
                maxScale: 6.0,
                child: Center(
                  child: SizedBox(
                    width: dispW,
                    height: dispH,
                    child: Stack(
                      children: [
                        Positioned.fill(child: imgChild),
                        // Speech Bubble Translation Overlay (文A)
                        if (_ocrVisible && _ocrBlocks.isNotEmpty)
                          ..._ocrBlocks.map((b) {
                            final leftPx = (b.x / 100.0) * dispW;
                            final topPx = (b.y / 100.0) * dispH;
                            final boxW = ((b.w / 100.0) * dispW).clamp(24.0, dispW - leftPx);
                            final boxH = ((b.h / 100.0) * dispH).clamp(16.0, dispH - topPx);

                            return Positioned(
                              left: leftPx,
                              top: topPx,
                              width: boxW,
                              height: boxH,
                              child: Tooltip(
                                message: '${b.orig} → ${b.trans}',
                                child: Container(
                                  padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
                                  decoration: BoxDecoration(
                                    color: _parseHexColor(b.bgHex, Colors.white),
                                    borderRadius: BorderRadius.circular(5),
                                    boxShadow: const [
                                      BoxShadow(color: Colors.black38, blurRadius: 4),
                                    ],
                                  ),
                                  alignment: Alignment.center,
                                  child: AutoSizeText(
                                    b.trans,
                                    textAlign: TextAlign.center,
                                    minFontSize: 6,
                                    maxFontSize: 24,
                                    stepGranularity: 0.5,
                                    style: TextStyle(
                                      fontWeight: FontWeight.w700,
                                      color: _parseHexColor(b.fgHex, Colors.black87),
                                      height: 1.15,
                                    ),
                                  ),
                                ),
                              ),
                            );
                          }),
                      ],
                    ),
                  ),
                ),
              );
            },
          ),

          if (_ocrLoading)
            Positioned(
              top: 16,
              left: 0,
              right: 0,
              child: Center(
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
                  decoration: BoxDecoration(
                    color: Colors.black87,
                    borderRadius: BorderRadius.circular(20),
                  ),
                  child: Text(
                    prov.tr('translatingWait'),
                    style: const TextStyle(color: Colors.white, fontSize: 12.5),
                  ),
                ),
              ),
            ),

          // Left / Right navigation arrows on desktop
          if (isDesktop && prov.visibleImages.length > 1) ...[
            Positioned(
              left: 12,
              top: 0,
              bottom: 0,
              child: Center(
                child: IconButton.filledTonal(
                  onPressed: () => _navigateDelta(prov, -1),
                  icon: const Icon(Icons.chevron_left, size: 28),
                ),
              ),
            ),
            Positioned(
              right: 12,
              top: 0,
              bottom: 0,
              child: Center(
                child: IconButton.filledTonal(
                  onPressed: () => _navigateDelta(prov, 1),
                  icon: const Icon(Icons.chevron_right, size: 28),
                ),
              ),
            ),
          ],

          // Top close button for mobile or fullscreen mode
          if (!isDesktop || _isFullscreen)
            Positioned(
              top: 12,
              right: 12,
              child: IconButton.filledTonal(
                onPressed: () => Navigator.pop(context),
                icon: const Icon(Icons.close),
              ),
            ),

          // Bottom zoom & fullscreen toolbar
          if (isDesktop)
            Positioned(
              bottom: 14,
              left: 0,
              right: 0,
              child: Center(
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                  decoration: BoxDecoration(
                    color: Theme.of(context).canvasColor.withOpacity(0.9),
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: Theme.of(context).dividerColor),
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      TextButton(
                        onPressed: () => _zoomCtrl.value = Matrix4.identity(),
                        child: const Text('1:1'),
                      ),
                      TextButton(
                        onPressed: () => setState(() => _isFullscreen = !_isFullscreen),
                        child: Text(
                          prov.tr(_isFullscreen ? 'exitFullscreenBtn' : 'fullscreenBtn'),
                        ),
                      ),
                      if (prov.settings.artistMode) ...[
                        TextButton(
                          onPressed: () => prov.toggleItemFlip(item),
                          child: Text(prov.tr('inspFlip')),
                        ),
                        TextButton(
                          onPressed: () => prov.toggleItemGrayscale(item),
                          child: Text(prov.tr('inspGray')),
                        ),
                      ],
                    ],
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildInspectorPanel(
    BuildContext context,
    AppProvider prov,
    MediaItem item, {
    ScrollController? scrollController,
  }) {
    return ListView(
      controller: scrollController,
      padding: const EdgeInsets.all(14),
      children: [
        // Header row
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Expanded(
              child: Text(
                '${item.filename} (${item.width}×${item.height})',
                style: const TextStyle(fontSize: 13.5, fontWeight: FontWeight.bold),
              ),
            ),
            IconButton(
              onPressed: () => Navigator.pop(context),
              icon: const Icon(Icons.close, size: 20),
            ),
          ],
        ),
        const SizedBox(height: 8),

        // Action buttons: Favorite, 文A OCR Translate, 🔄 Re-translate, Explorer, External Player, Delete
        Wrap(
          spacing: 6,
          runSpacing: 6,
          children: [
            FilledButton.tonal(
              onPressed: () => prov.toggleItemFavorite(item),
              child: Text(item.isFavorite ? '❤️' : '🤍'),
            ),
            if (prov.settings.imgTranslateEnabled && !item.isVideo) ...[
              Tooltip(
                message: prov.tr('translateImgTooltip'),
                child: FilledButton.tonal(
                  style: _ocrVisible
                      ? FilledButton.styleFrom(
                          backgroundColor: Theme.of(context).colorScheme.primary,
                          foregroundColor: Colors.white,
                        )
                      : null,
                  onPressed: () => _runOcrTranslation(forceRefresh: false),
                  child: const Text(
                    '文A',
                    style: TextStyle(fontWeight: FontWeight.w800),
                  ),
                ),
              ),
              if (_ocrVisible)
                Tooltip(
                  message: prov.tr('retranslateImgTooltip'),
                  child: OutlinedButton(
                    onPressed: () => _runOcrTranslation(forceRefresh: true),
                    child: const Text('🔄'),
                  ),
                ),
            ],
            if (Platform.isWindows || Platform.isLinux)
              OutlinedButton(
                onPressed: () => prov.revealInFileManager(item),
                child: Text(prov.tr('openExplorer')),
              ),
            if (item.isVideo)
              FilledButton(
                onPressed: () => prov.openInExternalPlayer(item),
                child: Text(prov.tr('openExtPlayer')),
              ),
            if (prov.settings.artistMode && !item.isVideo) ...[
              OutlinedButton(
                onPressed: () => prov.toggleItemFlip(item),
                child: Text(prov.tr('inspFlip')),
              ),
              OutlinedButton(
                onPressed: () => prov.toggleItemGrayscale(item),
                child: Text(prov.tr('inspGray')),
              ),
            ],
            IconButton(
              tooltip: prov.tr('bulkDeleteBtn'),
              color: const Color(0xFFEF4444),
              onPressed: () => _confirmAndDeleteCurrent(prov, item),
              icon: const Icon(Icons.delete_outline),
            ),
          ],
        ),

        if (_ocrVisible && _ocrBlocks.isEmpty && !_ocrLoading)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(
              prov.tr('noTextFound'),
              style: const TextStyle(fontSize: 12, color: Colors.orangeAccent),
            ),
          ),

        const Divider(height: 22),

        // App Folder selector
        Text(
          prov.tr('inspAppFolder'),
          style: const TextStyle(fontSize: 12, fontWeight: FontWeight.bold),
        ),
        const SizedBox(height: 6),
        DropdownButtonFormField<String>(
          value: prov.folderCategories.any((c) => c.key == item.folderCategory)
              ? item.folderCategory
              : 'general',
          isDense: true,
          decoration: InputDecoration(
            border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
          ),
          items: prov.folderCategories
              .map(
                (c) => DropdownMenuItem(
                  value: c.key,
                  child: Text(c.localizedLabel(prov.lang)),
                ),
              )
              .toList(),
          onChanged: (val) {
            if (val != null) {
              prov.updateItemFolderAndNote(item.id, folderCat: val);
            }
          },
        ),

        const SizedBox(height: 14),

        // Personal reference note (Artist Mode or always accessible)
        Text(
          prov.tr('inspNote'),
          style: const TextStyle(fontSize: 12, fontWeight: FontWeight.bold),
        ),
        const SizedBox(height: 6),
        TextField(
          controller: _noteCtrl,
          maxLines: 2,
          style: const TextStyle(fontSize: 12.5),
          decoration: InputDecoration(
            hintText: prov.tr('inspNotePh'),
            border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
          ),
          onChanged: (v) => prov.updateItemFolderAndNote(item.id, notes: v),
        ),

        const SizedBox(height: 14),

        // Tags list + Add custom tag input
        Text(
          '${prov.tr('inspTags')} (${item.tags.length})',
          style: const TextStyle(fontSize: 12, fontWeight: FontWeight.bold),
        ),
        const SizedBox(height: 8),
        Wrap(
          spacing: 5,
          runSpacing: 5,
          children: item.tags.map((tg) {
            return Chip(
              label: Text(
                prov.formatTagDisplay(tg),
                style: const TextStyle(fontSize: 11.5),
              ),
              visualDensity: VisualDensity.compact,
              onDeleted: () => prov.removeTagFromItem(item.id, tg),
            );
          }).toList(),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: TextField(
                controller: _newTagCtrl,
                style: const TextStyle(fontSize: 12.5),
                decoration: InputDecoration(
                  hintText: prov.tr('addTagPh'),
                  isDense: true,
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                ),
                onSubmitted: (val) async {
                  if (val.trim().isNotEmpty) {
                    await prov.addCustomTagToItem(item.id, val.trim());
                    _newTagCtrl.clear();
                  }
                },
              ),
            ),
            const SizedBox(width: 6),
            FilledButton.tonal(
              onPressed: () async {
                final val = _newTagCtrl.text.trim();
                if (val.isNotEmpty) {
                  await prov.addCustomTagToItem(item.id, val);
                  _newTagCtrl.clear();
                }
              },
              child: Text(prov.tr('addTagBtn')),
            ),
          ],
        ),
      ],
    );
  }
}
