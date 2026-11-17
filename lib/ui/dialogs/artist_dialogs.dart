import 'dart:io';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../models/models.dart';
import '../../services/database_service.dart';
import '../../services/scanner_service.dart';
import '../../state/app_provider.dart';
import 'inspector_dialog.dart';

class IdeaRouletteDialog extends StatefulWidget {
  const IdeaRouletteDialog({super.key});

  @override
  State<IdeaRouletteDialog> createState() => _IdeaRouletteDialogState();
}

class _IdeaRouletteDialogState extends State<IdeaRouletteDialog> {
  List<MediaItem> _cards = [];

  @override
  void initState() {
    super.initState();
    _roll();
  }

  void _roll() {
    final prov = context.read<AppProvider>();
    setState(() {
      _cards = prov.getIdeaRouletteSet();
    });
  }

  @override
  Widget build(BuildContext context) {
    final prov = context.watch<AppProvider>();
    final roles = [
      prov.tr('rouletteRole1'),
      prov.tr('rouletteRole2'),
      prov.tr('rouletteRole3'),
    ];

    return Dialog(
      insetPadding: const EdgeInsets.all(20),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: Container(
        width: 920,
        padding: const EdgeInsets.all(18),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  prov.tr('rouletteTitle'),
                  style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
                ),
                Row(
                  children: [
                    FilledButton(
                      onPressed: _roll,
                      child: Text(prov.tr('reroll')),
                    ),
                    const SizedBox(width: 8),
                    IconButton(
                      onPressed: () => Navigator.pop(context),
                      icon: const Icon(Icons.close),
                    ),
                  ],
                ),
              ],
            ),
            const SizedBox(height: 14),
            if (_cards.isEmpty)
              Padding(
                padding: const EdgeInsets.all(32),
                child: Text(prov.tr('emptyTitle')),
              )
            else
              SizedBox(
                height: 320,
                child: Row(
                  children: List.generate(_cards.length, (i) {
                    final item = _cards[i];
                    final thumb = ScannerService.instance.getThumbnailFileForPath(item.path);
                    final file = thumb.existsSync() ? thumb : File(item.path);
                    return Expanded(
                      child: Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 6),
                        child: InkWell(
                          onTap: () {
                            final idx = prov.visibleImages.indexWhere((x) => x.id == item.id);
                            if (idx >= 0) {
                              Navigator.pop(context);
                              showDialog(
                                context: context,
                                builder: (_) => InspectorDialog(initialIndex: idx),
                              );
                            }
                          },
                          child: Card(
                            clipBehavior: Clip.antiAlias,
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                Container(
                                  padding: const EdgeInsets.all(8),
                                  color: Theme.of(context).colorScheme.primary.withOpacity(0.18),
                                  child: Text(
                                    roles[i % roles.length],
                                    style: const TextStyle(
                                      fontWeight: FontWeight.bold,
                                      fontSize: 12,
                                    ),
                                  ),
                                ),
                                Expanded(
                                  child: Image.file(file, fit: BoxFit.contain),
                                ),
                                Padding(
                                  padding: const EdgeInsets.all(8),
                                  child: Text(
                                    item.filename,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(fontSize: 11.5),
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ),
                      ),
                    );
                  }),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class SearchFaqDialog extends StatelessWidget {
  const SearchFaqDialog({super.key});

  @override
  Widget build(BuildContext context) {
    final prov = context.watch<AppProvider>();
    return AlertDialog(
      title: Text(prov.tr('faqModalTitle')),
      content: SizedBox(
        width: 520,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(prov.tr('faqStep1'), style: const TextStyle(fontSize: 13)),
            const SizedBox(height: 10),
            Text(prov.tr('faqStep2'), style: const TextStyle(fontSize: 13)),
            const SizedBox(height: 10),
            Text(prov.tr('faqStep3'), style: const TextStyle(fontSize: 13)),
            const SizedBox(height: 10),
            Text(prov.tr('faqStep4'), style: const TextStyle(fontSize: 13)),
          ],
        ),
      ),
      actions: [
        FilledButton(
          onPressed: () => Navigator.pop(context),
          child: Text(prov.tr('close')),
        ),
      ],
    );
  }
}

class ArtistNotesDrawer extends StatefulWidget {
  const ArtistNotesDrawer({super.key});

  @override
  State<ArtistNotesDrawer> createState() => _ArtistNotesDrawerState();
}

class _ArtistNotesDrawerState extends State<ArtistNotesDrawer> {
  int? _selectedId;
  final TextEditingController _titleCtrl = TextEditingController();
  final TextEditingController _bodyCtrl = TextEditingController();

  @override
  void initState() {
    super.initState();
    final prov = context.read<AppProvider>();
    if (prov.artistNotes.isNotEmpty) {
      _selectNote(prov.artistNotes.first);
    }
  }

  void _selectNote(ArtistNoteItem note) {
    setState(() {
      _selectedId = note.id;
      _titleCtrl.text = note.title;
      _bodyCtrl.text = note.content;
    });
  }

  Future<void> _saveCurrent(AppProvider prov) async {
    final saved = await DatabaseService.instance.saveArtistNote(
      id: _selectedId,
      title: _titleCtrl.text,
      content: _bodyCtrl.text,
    );
    prov.artistNotes = await DatabaseService.instance.getArtistNotes();
    setState(() => _selectedId = saved.id);
    prov.notifyListeners();
  }

  @override
  Widget build(BuildContext context) {
    final prov = context.watch<AppProvider>();
    return Drawer(
      width: 350,
      child: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Text(
                    prov.tr('notesDrawerTitle'),
                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14),
                  ),
                  FilledButton.tonal(
                    onPressed: () async {
                      final created = await DatabaseService.instance.saveArtistNote(
                        title: prov.lang == 'en' ? 'New Note' : 'Новая заметка',
                        content: '',
                      );
                      prov.artistNotes = await DatabaseService.instance.getArtistNotes();
                      _selectNote(created);
                    },
                    child: Text(prov.tr('newNoteBtn')),
                  ),
                ],
              ),
              const SizedBox(height: 10),
              SizedBox(
                height: 140,
                child: ListView.builder(
                  itemCount: prov.artistNotes.length,
                  itemBuilder: (ctx, idx) {
                    final n = prov.artistNotes[idx];
                    return ListTile(
                      dense: true,
                      selected: n.id == _selectedId,
                      title: Text(n.title, maxLines: 1, overflow: TextOverflow.ellipsis),
                      trailing: IconButton(
                        icon: const Icon(Icons.delete_outline, size: 16),
                        onPressed: () async {
                          await DatabaseService.instance.deleteArtistNote(n.id);
                          prov.artistNotes = await DatabaseService.instance.getArtistNotes();
                          setState(() {
                            if (prov.artistNotes.isNotEmpty) {
                              _selectNote(prov.artistNotes.first);
                            } else {
                              _selectedId = null;
                              _titleCtrl.clear();
                              _bodyCtrl.clear();
                            }
                          });
                        },
                      ),
                      onTap: () => _selectNote(n),
                    );
                  },
                ),
              ),
              const Divider(),
              TextField(
                controller: _titleCtrl,
                decoration: InputDecoration(
                  hintText: prov.tr('noteTitlePh'),
                  isDense: true,
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                ),
                onChanged: (_) => _saveCurrent(prov),
              ),
              const SizedBox(height: 8),
              Expanded(
                child: TextField(
                  controller: _bodyCtrl,
                  maxLines: null,
                  expands: true,
                  textAlignVertical: TextAlignVertical.top,
                  decoration: InputDecoration(
                    hintText: prov.tr('noteBodyPh'),
                    border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                  ),
                  onChanged: (_) => _saveCurrent(prov),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
