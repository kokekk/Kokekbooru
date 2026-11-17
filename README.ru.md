<div align="center">

# 🎨 Kokekbooru (`beta 0.2`)

**Нативный приватный локальный буру-каталогизатор и менеджер референсов для художников и коллекционеров**  
Написан на **Flutter (Dart)** для **Windows, Linux и Android**

[**🇬🇧 English**](README.md) | [**🇷🇺 Русский**](README.ru.md)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20Android-7c5cff)](https://github.com/kokekk/kokekbooru)

</div>

## ✨ О проекте

**Kokekbooru** (`https://github.com/kokekk/kokekbooru`, автор: `@kokekk`) — быстрый приватный локальный буру-каталогизатор и менеджер референсов. Все оригинальные файлы остаются в ваших папках на диске или смартфоне (не копируются внутрь закрытого контейнера), а база данных (`SQLite`), превью и настройки хранятся локально (`~/.kokekbooru/` на ПК и `getApplicationSupportDirectory()` на Android).

## 🚀 Ключевые возможности (`beta 0.2`)

- **Адаптивный интерфейс (Desktop vs Android):**
  - **Десктоп (`width > 820px`):** левая боковая панель (`Sidebar`) + верхняя панель (`Topbar`) + сетка карточек (`maxCrossAxisExtent: 240`), сплит-просмотрщик с зумом (`InteractiveViewer`) и двухколоночное окно настроек.
  - **Android (`width <= 820px`):** нижняя навигация (`Галерея`, `Папки`, `Теги`, `Настройки`), полноэкранный просмотр со свайпами (`PageView`) и выезжающей шторкой (`DraggableScrollableSheet`), а также отдельная кнопка **`🖼 Из галереи`** для импорта и авто-тегирования отдельных фото/видео из стандартной галереи Android без сканирования всей папки.
- **Локальное ИИ-тегирование ONNX (`SmilingWolf WaifuDiffusion v3`):**
  - Поддержка двух моделей со скачиванием прямо из настроек: **WD-EVA02-Large v3** (~1.26 ГБ, максимальная точность для ПК) и **WD-ViT v3** (~360 МБ, быстрая модель для Android и слабых ПК).
  - Автоподбор безопасного батча по доступной VRAM/ОЗУ (`16/6` для `15+ ГБ`, `8/3` для `8–14 ГБ`, `4/2` для `4–7 ГБ`, `2/1` для `<4 ГБ`, CPU или Android) и анализ видео по 3 кадрам (`начало`, `середина`, `конец`).
  - Полный словарь русских тегов (`TAG_RU_DICT`) и категории `📐 Ракурс`, `🧘 Поза и действие`, `🏷️ Общие теги`, `👤 Персонажи` и `🔞 18+ (NSFW)`.
- **Два режима удаления файлов (`Основные` настройки):**
  1. **Удалять только из интерфейса приложения:** файл скрывается из галереи и заносится в таблицу `ignored_paths` (чтобы не появляться снова при следующем сканировании), но физический файл на устройстве остаётся нетронутым.
  2. **Удалять полностью из системы:** физическое удаление файла с диска/смартфона вместе с записью в базе.
- **Встроенный OCR и перевод поверх картинок (`文A` + `Bubble Overlay`):**
  - Распознавание текста через оффлайн **Google ML Kit** (на Android), **Windows.Media.Ocr** (`UTF-8` + `ru-RU`) и **RapidOCR** (на ПК), а также облачный фоллбэк **OCR.space** (`language=rus`).
  - Объединение строк одного облачка речи (`speech bubble`), автоподбор цвета фона/текста и размера шрифта (`AutoSizeText`) с переводом через **ML Kit**, локальную нейросеть **NLLB-200 INT8** (~600 МБ) или онлайн API (**Google Translate**, **MyMemory**, **LibreTranslate**, **OpenAI**).
- **Режим художника и кроссплатформенный JSON-бэкап:**
  - Форматный фильтр (`Вертикальные`, `Горизонтальные`, `Квадратные`), быстрое отражение (`↔️ Flip`) и перевод в Ч/Б (`◐ Grayscale`), `🎲 Рулетка идей` и встроенный блокнот заметок.
  - Экспорт и импорт всей базы в единый `.json` файл, на 100% совместимый между ПК, Android и прототипом `kokekbooru.py`.

## 📦 Установка и запуск

### Готовые сборки (GitHub Releases)
Скачайте подходящий файл со страницы [Releases](https://github.com/kokekk/kokekbooru/releases):
- **Установщик Windows:** `Kokekbooru-Setup.exe`
- **Портативная версия Windows:** `Kokekbooru-Windows-Portable.zip`
- **Linux (x86_64):** `Kokekbooru-Linux-x86_64.tar.gz`
- **Android APK:** `Kokekbooru-Android.apk`

### Сборка из исходного кода
```bash
git clone https://github.com/kokekk/kokekbooru.git
cd kokekbooru
flutter pub get

# Запуск в режиме разработки (Windows / Linux)
flutter run -d windows   # или -d linux

# Сборка релизных версий
flutter build windows --release
flutter build linux --release
flutter build apk --release
```

## 📄 Лицензия
Распространяется под лицензией **GNU General Public License v3.0 (GPL-3.0)**. Автор: `@kokekk`.
