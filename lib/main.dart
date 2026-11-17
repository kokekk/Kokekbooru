import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:provider/provider.dart';
import 'package:window_manager/window_manager.dart';

import 'state/app_provider.dart';
import 'ui/screens/home_screen.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  if (Platform.isWindows || Platform.isLinux || Platform.isMacOS) {
    try {
      await windowManager.ensureInitialized();
      const windowOptions = WindowOptions(
        size: Size(1360, 860),
        minimumSize: Size(680, 520),
        center: true,
        title: 'Kokekbooru (beta 0.2)',
      );
      windowManager.waitUntilReadyToShow(windowOptions, () async {
        await windowManager.show();
        await windowManager.focus();
      });
    } catch (_) {}
  }

  final appProvider = AppProvider();
  await appProvider.initialize();

  runApp(
    ChangeNotifierProvider<AppProvider>.value(
      value: appProvider,
      child: const KokekbooruApp(),
    ),
  );
}

class KokekbooruApp extends StatelessWidget {
  const KokekbooruApp({super.key});

  ThemeMode _resolveThemeMode(String pref) {
    switch (pref) {
      case 'dark':
        return ThemeMode.dark;
      case 'light':
        return ThemeMode.light;
      default:
        return ThemeMode.system;
    }
  }

  @override
  Widget build(BuildContext context) {
    final prov = context.watch<AppProvider>();
    final themeMode = _resolveThemeMode(prov.settings.theme);

    final darkTheme = ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      scaffoldBackgroundColor: const Color(0xFF101217),
      cardColor: const Color(0xFF212531),
      canvasColor: const Color(0xFF181B23),
      dividerColor: const Color(0xFF2E3446),
      colorScheme: const ColorScheme.dark(
        primary: Color(0xFF7C5CFF),
        secondary: Color(0xFFEC4899),
        surface: Color(0xFF181B23),
        error: Color(0xFFEF4444),
      ),
    );

    final lightTheme = ThemeData(
      useMaterial3: true,
      brightness: Brightness.light,
      scaffoldBackgroundColor: const Color(0xFFF1F5F9),
      cardColor: const Color(0xFFF8FAFC),
      canvasColor: const Color(0xFFFFFFFF),
      dividerColor: const Color(0xFFCBD5E1),
      colorScheme: const ColorScheme.light(
        primary: Color(0xFF6366F1),
        secondary: Color(0xFFEC4899),
        surface: Color(0xFFFFFFFF),
        error: Color(0xFFEF4444),
      ),
    );

    return MaterialApp(
      title: 'Kokekbooru (beta 0.2)',
      debugShowCheckedModeBanner: false,
      themeMode: themeMode,
      theme: lightTheme,
      darkTheme: darkTheme,
      locale: Locale(prov.lang),
      supportedLocales: const [
        Locale('ru'),
        Locale('en'),
      ],
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      home: const HomeScreen(),
    );
  }
}
