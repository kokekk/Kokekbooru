<div align="center">

# 🎨 Kokekbooru (`beta 0.2`)

**Native Private Local Booru Cataloger & Reference Manager for Artists and Collectors**  
Built with **Flutter (Dart)** for **Windows, Linux, and Android**

[**🇬🇧 English**](README.md) | [**🇷🇺 Русский**](README.ru.md)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20Android-7c5cff)](https://github.com/kokekk/kokekbooru)

</div>

## ✨ Overview

**Kokekbooru** (`https://github.com/kokekk/kokekbooru`, author: `@kokekk`) is a fast, 100% local booru-style media library and reference manager. Your original photos and videos remain untouched in their existing folders on your PC or smartphone — Kokekbooru stores only its SQLite index, thumbnails, and settings locally (`~/.kokekbooru/` on Desktop and `getApplicationSupportDirectory()` on Android).

## 🚀 Key Features (`beta 0.2`)

- **Adaptive Desktop & Mobile UI:**
  - **Desktop (`width > 820px`):** Left Sidebar + Topbar + responsive media grid (`maxCrossAxisExtent: 240`), split-screen Inspector with `InteractiveViewer`, and 2-column Settings window.
  - **Android (`width <= 820px`):** Bottom Navigation Bar (`Gallery`, `Folders`, `Tags`, `Settings`), full-screen swipeable `PageView` inspector with bottom `DraggableScrollableSheet`, and a dedicated **`🖼 From Gallery`** button to import and AI-tag individual images/videos from the standard Android Photo Picker without scanning an entire folder.
- **Local ONNX AI Auto-Tagging (`SmilingWolf WaifuDiffusion v3`):**
  - Supports **WD-EVA02-Large v3** (~1.26 GB, maximum accuracy for PC) and **WD-ViT v3** (~360 MB, fast & lightweight for Android and low-spec PCs), downloadable directly from Settings.
  - Automatic VRAM/RAM batch sizing (`16/6` for `15+ GB`, `8/3` for `8–14 GB`, `4/2` for `4–7 GB`, `2/1` for `<4 GB`, CPU, or Android) and 3-frame video sampling (`beginning`, `middle`, `end`).
  - Categorizes tags into `📐 Angle`, `🧘 Pose & Action`, `🏷️ General Tags`, `👤 Characters`, and `🔞 18+ (NSFW)` with full Russian tag dictionary (`TAG_RU_DICT`) compatibility.
- **Safe File Deletion Modes:**
  - Switchable in `Settings -> General`:
    1. **Remove from app interface only:** hides the file from Kokekbooru and records its path in `ignored_paths` so future scans skip it, while leaving the physical file on disk untouched.
    2. **Delete permanently from system:** physically deletes the file from your device storage and database.
- **Built-in OCR & Speech Bubble Overlay Translation (`文A`):**
  - Recognizes speech bubbles via **Google ML Kit** (Android), **Windows.Media.Ocr** (`UTF-8` + `ru-RU` Cyrillic support) / **RapidOCR** (Desktop), or **OCR.space** fallback.
  - Translates via offline **Google ML Kit**, offline **NLLB-200 INT8** (~600 MB), or online APIs (**Google Translate**, **MyMemory**, **LibreTranslate**, **OpenAI-compatible**) and renders auto-sized speech bubble overlays (`AutoSizeText`) directly over the image.
- **Artist Mode & Cross-Platform Backup:**
  - Aspect ratio filter (`Portrait`, `Landscape`, `Square`), quick `↔️ Flip H` and `◐ Grayscale` checks, `🎲 Idea Roulette`, and built-in Artist Notebook.
  - Full `.json` database backup & restore 100% compatible between Desktop, Android, and `kokekbooru.py`.

## 📦 Installation

### Prebuilt Releases (GitHub Releases)
Download the latest package for your platform from the [Releases](https://github.com/kokekk/kokekbooru/releases) page:
- **Windows Installer:** `Kokekbooru-Setup.exe`
- **Windows Portable:** `Kokekbooru-Windows-Portable.zip`
- **Linux (x86_64):** `Kokekbooru-Linux-x86_64.tar.gz`
- **Android APK:** `Kokekbooru-Android.apk`

### Build from Source
```bash
git clone https://github.com/kokekk/kokekbooru.git
cd kokekbooru
flutter pub get

# Run on Desktop (Windows / Linux)
flutter run -d windows   # or -d linux

# Build Release binaries
flutter build windows --release
flutter build linux --release
flutter build apk --release
```

## 📄 License
Distributed under the **GNU General Public License v3.0 (GPL-3.0)**. Copyright © `@kokekk`.
