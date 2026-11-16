#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kokekbooru (public | beta 0.1)
Local FastAPI + SQLite + ONNX Runtime media library with GPU/CPU auto-tagging,
OCR overlay translation, and local NLLB-200 neural translation support.
"""

from concurrent.futures import ThreadPoolExecutor
import base64
import csv
import ctypes
import hashlib
import importlib
import io
import json
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# =====================================================================
# 0. ПОЛНОЕ СКРЫТИЕ КОНСОЛИ PYTHON (WINDOWS) И БЕЗОПАСНЫЕ ПОТОКИ ЛОГОВ
# =====================================================================

APP_DIR = Path.home() / ".kokekbooru"
APP_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE_PATH = APP_DIR / "kokekbooru.log"

IS_FROZEN = getattr(sys, "frozen", False)
WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform.startswith("win") else 0


def hide_python_console_completely():
    try:
        log_f = open(LOG_FILE_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = log_f
        sys.stderr = log_f
    except Exception:
        pass

    if sys.platform.startswith("win"):
        try:
            kernel32 = ctypes.windll.kernel32
            user32 = ctypes.windll.user32
            hwnd = kernel32.GetConsoleWindow()
            if hwnd:
                user32.ShowWindow(hwnd, 0)
            kernel32.FreeConsole()
        except Exception:
            pass


hide_python_console_completely()

import numpy as np
from PIL import Image, ImageDraw, ImageOps

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from pydantic import BaseModel
    import uvicorn
except ImportError:
    if IS_FROZEN:
        raise
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "fastapi", "uvicorn", "pydantic", "pillow", "numpy"],
        creationflags=WIN_NO_WINDOW,
    )
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from pydantic import BaseModel
    import uvicorn


# =====================================================================
# 1. ПУТИ ХРАНЕНИЯ И КОНФИГУРАЦИЯ
# =====================================================================

HUB_VERSION = "beta 0.1"
EDITION = "public"

SHARED_MODELS_DIR = Path.home() / ".kokekbooru" / "ai_models"
SHARED_MODELS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = APP_DIR / "kokekbooru.db"
SETTINGS_PATH = APP_DIR / "kokekbooru_settings.json"
THUMBS_DIR = APP_DIR / "thumbnails"
FLIPPED_DIR = APP_DIR / "flipped_cache"
MODELS_DIR = SHARED_MODELS_DIR if EDITION == "public" else (APP_DIR / "ai_models")
AI_DONE_FLAG = MODELS_DIR / "ai_setup_done.json"
AI_BANNER_DISMISSED_FLAG = APP_DIR / "ai_banner_dismissed.json"

for d in (APP_DIR, THUMBS_DIR, FLIPPED_DIR, MODELS_DIR):
    d.mkdir(parents=True, exist_ok=True)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv"}
ALL_MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

GPU_BATCH_SIZE = 16
VIDEO_BATCH_SIZE = 6

def detect_gpu_vram_gb() -> Optional[float]:
    """Определяет объём видеопамяти (VRAM) в ГБ для подбора безопасного размера батча."""
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
            creationflags=WIN_NO_WINDOW,
        )
        if res.returncode == 0 and res.stdout.strip():
            vals = []
            for ln in res.stdout.decode("utf-8", errors="ignore").splitlines():
                ln = ln.strip()
                if ln.isdigit():
                    vals.append(int(ln))
            if vals:
                return round(max(vals) / 1024.0, 2)
    except Exception:
        pass

    if sys.platform.startswith("win"):
        try:
            ps_cmd = (
                "$v = (Get-ItemProperty -Path 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' "
                "-Name 'HardwareInformation.qwMemorySize' -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty 'HardwareInformation.qwMemorySize' | Measure-Object -Maximum).Maximum; "
                "if ($v) { [math]::Round($v / 1GB, 2) }"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=4,
                creationflags=WIN_NO_WINDOW,
            )
            if res.returncode == 0 and res.stdout.strip():
                val = float(res.stdout.decode("utf-8", errors="ignore").strip().replace(",", "."))
                if val > 0:
                    return val
        except Exception:
            pass
    return None


def get_optimal_batch_sizes(active_provider: str) -> Tuple[int, int]:
    """Подбирает (image_batch, video_batch) под VRAM видеокарты, чтобы не вылетать на 4-12 ГБ GPU."""
    if active_provider == "CPU":
        return 4, 2
    vram_gb = detect_gpu_vram_gb()
    if vram_gb is None:
        return 4, 2
    if vram_gb >= 15.0:
        return 16, 6
    if vram_gb >= 8.0:
        return 8, 3
    if vram_gb >= 4.0:
        return 4, 2
    return 2, 1

VIDEO_SAMPLE_FRAMES = 3
MAX_GENERAL_TAGS_PER_ITEM = 32
CPU_PREFETCH_WORKERS = min(8, (os.cpu_count() or 4))

EVA02_ONNX_URL = "https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/resolve/main/model.onnx"
EVA02_CSV_URL = "https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/resolve/main/selected_tags.csv"
EVA02_ONNX_PATH = MODELS_DIR / "wd_eva02_large_tagger_v3.onnx"
EVA02_CSV_PATH = MODELS_DIR / "eva02_selected_tags.csv"

VIT_ONNX_URL = "https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/resolve/main/model.onnx"
VIT_CSV_URL = "https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/resolve/main/selected_tags.csv"
VIT_ONNX_PATH = MODELS_DIR / "wd_vit_tagger_v3.onnx"
VIT_CSV_PATH = MODELS_DIR / "selected_tags.csv"

TRANS_MODEL_DIR = MODELS_DIR / "nllb_200_ct2_int8"
NLLB_REPO_BASE = "https://huggingface.co/mijuanlo/nllb-200-distilled-600M-ct2-int8/resolve/main"
NLLB_FILES = [
    ("model.bin", f"{NLLB_REPO_BASE}/model.bin"),
    ("sentencepiece.bpe.model", f"{NLLB_REPO_BASE}/sentencepiece.bpe.model"),
    ("shared_vocabulary.txt", f"{NLLB_REPO_BASE}/shared_vocabulary.txt"),
    ("config.json", f"{NLLB_REPO_BASE}/config.json"),
]
NLLB_LANG_MAP = {
    "ru": "rus_Cyrl",
    "en": "eng_Latn",
    "uk": "ukr_Cyrl",
    "de": "deu_Latn",
    "es": "spa_Latn",
    "fr": "fra_Latn",
    "ja": "jpn_Jpan",
    "zh-CN": "zho_Hans",
    "ko": "kor_Hang",
}

# Базовая корневая категория по умолчанию
DEFAULT_FOLDER_CATEGORIES = [
    ("general", "🗂️ Все файлы", 0),
]

LEGACY_PRESET_CATEGORY_KEYS = {
    "lighting_refs", "heels", "soles", "toes", "full_poses", "micro_world",
    "anatomy", "environment", "characters", "props_fx",
}

BRAWL_STARS_CHARS: Set[str] = {
    "shelly", "nita", "colt", "bull", "brock", "el_primo", "barley", "poco", "rosa",
    "jessie", "dynamike", "tick", "8-bit", "rico", "darryl", "penny", "carl", "jacky",
    "gus", "bo", "emz", "stu", "piper", "pam", "frank", "bibi", "bea", "nani",
    "edgar", "griff", "grom", "bonnie", "gale", "colette", "belle", "ash", "lola",
    "sam", "mandy", "maisie", "hank", "pearl", "larry", "lawrie", "angelo", "berry",
    "shade", "mortis", "tara", "gene", "max", "mr_p", "sprout", "byron", "squeak",
    "lou", "ruffs", "buzz", "fang", "eve", "janet", "otis", "buster", "gray",
    "rt", "willow", "doug", "chuck", "charlie", "mico", "melodie", "lily", "clancy",
    "moe", "juju", "spike", "crow", "leon", "sandy", "amber", "meg", "surge",
    "chester", "cordelius", "kit", "draco", "kenji", "meeple", "ollie",
}

# Базовый словарь локализации тегов (EN -> RU)
TAG_RU_DICT: Dict[str, str] = {
    # Специальные и базовые
    "video": "Видео",
    "1girl": "1 девушка",
    "2girls": "2 девушки",
    "3girls": "3 девушки",
    "4girls": "4 девушки",
    "5girls": "5 девушек",
    "6+girls": "6+ девушек",
    "multiple_girls": "Несколько девушек",
    "1boy": "1 парень",
    "2boys": "2 парня",
    "3boys": "3 парня",
    "multiple_boys": "Несколько парней",
    "1other": "1 другой персонаж",
    "solo": "Соло",
    "solo_focus": "Фокус на одном",
    "couple": "Пара",
    "group": "Группа",
    "crowd": "Толпа",

    # Ракурсы и планы (angle:*)
    "under_sole": "Вид из-под подошвы",
    "pov_ground": "Вид с земли",
    "back_heel": "Вид сзади на пятки",
    "side_arch": "Вид сбоку на свод",
    "top_down": "Вид сверху вниз",
    "from_below": "Вид снизу",
    "from_above": "Вид сверху",
    "from_behind": "Вид сзади",
    "from_side": "Вид сбоку",
    "pov": "От первого лица",
    "pov_hands": "Руки от первого лица",
    "pov_feet": "Стопы от первого лица",
    "pov_crotch": "Вид снизу (паховая зона)",
    "closeup": "Крупный план",
    "close-up": "Крупный план",
    "full_body": "В полный рост",
    "lower_body": "Нижняя часть тела",
    "upper_body": "По пояс",
    "cowboy_shot": "По бёдра",
    "portrait": "Портрет",
    "wide_shot": "Общий план",
    "foreshortening": "Перспектива",
    "dutch_angle": "Наклон камеры",
    "fisheye": "Рыбий глаз",
    "panoramic": "Панорама",
    "profile": "В профиль",
    "back_view": "Вид со спины",
    "back": "Спина",
    "straight_on": "Прямой ракурс",
    "straight-on": "Прямой ракурс",
    "worm's-eye_view": "Вид с уровня земли",
    "bird's-eye_view": "Вид с высоты птичьего полёта",
    "intense_angle": "Динамичный ракурс",
    "dynamic_angle": "Динамичный ракурс",
    "vanishing_point": "Точка схода",

    # Позы стопы и тела (pose:*)
    "tiptoe": "На цыпочках",
    "tiptoes": "На цыпочках",
    "spread_toes": "Растопыренные пальцы",
    "curled_toes": "Поджатые пальцы",
    "clenched_toes": "Сжатые пальцы стоп",
    "stepping": "Шаг / наступание",
    "crushing": "Раздавливание / нажим",
    "trampling": "Топтание",
    "foot_worship": "Поклонение стопам",
    "sitting": "Сидит",
    "standing": "Стоит",
    "squatting": "На корточках",
    "kneeling": "На коленях",
    "lying": "Лежит",
    "on_back": "Лежит на спине",
    "on_stomach": "Лежит на животе",
    "on_side": "Лежит на боку",
    "all_fours": "На четвереньках",
    "legs_up": "Ноги вверх",
    "crossed_legs": "Нога на ногу",
    "spread_legs": "Разведённые ноги",
    "straddling": "Верхом",
    "leaning_forward": "Наклон вперёд",
    "leaning_back": "Наклон назад",
    "arched_back": "Прогиб в спине",
    "arms_up": "Руки вверх",
    "arms_behind_back": "Руки за спиной",
    "arms_behind_head": "Руки за головой",
    "crossed_arms": "Скрещенные руки",
    "walking": "Идёт",
    "running": "Бежит",
    "jumping": "Прыжок",
    "floating": "Парит в воздухе",
    "flying": "Полёт",
    "stretching": "Потягивается",
    "hugging": "Объятия",
    "fighting_stance": "Боевая стойка",
    "reaching": "Тянется рукой",
    "looking_at_viewer": "Смотрит в камеру",
    "looking_back": "Оглядывается назад",
    "looking_up": "Смотрит вверх",
    "looking_down": "Смотрит вниз",
    "looking_away": "Отводит взгляд",
    "looking_to_the_side": "Смотрит в сторону",
    "head_tilt": "Наклон головы",
    "peace_sign": "Жест мира (V)",
    "v": "Жест V",
    "pointing": "Указывает пальцем",
    "waving": "Машет рукой",
    "carrying": "Несёт в руках",
    "holding": "Держит предмет",
    "hand_on_hip": "Рука на бедре",
    "hands_on_hips": "Руки на бёдрах",
    "arm_up": "Поднятая рука",
    "leg_up": "Поднятая нога",
    "knees_up": "Подтянутые колени",
    "hugging_own_legs": "Обнимает колени",
    "fetal_position": "Поза эмбриона",
    "seiza": "Сэйдза (сидя на пятках)",
    "wariza": "Варидза (сидя ноги врозь)",
    "indian_style": "Сидя по-турецки",
    "split": "Шпагат",
    "standing_split": "Вертикальный шпагат",
    "bent_over": "Наклонившись",
    "paw_pose": "Поза лапок",
    "salute": "Отдаёт честь",
    "shushing": "Палец у губ (тс-с)",
    "thumbs_up": "Палец вверх",
    "outstretched_arm": "Вытянутая рука",
    "outstretched_hand": "Протянутая рука",
    "reaching_towards_viewer": "Тянется к зрителю",
    "incoming_foot": "Приближающаяся стопа",
    "foot_up": "Поднятая стопа",
    "feet_up": "Поднятые стопы",
    "soles_up": "Подошвы вверх",
    "plantar_flexion": "Вытянутый носок стопы",
    "dorsiflexion": "Тянет носок на себя",

    # Стопы, анатомия и GTS детали (detail:*)
    "barefoot": "Босиком",
    "soles": "Подошвы",
    "sole_focus": "Акцент на подошвах",
    "toes": "Пальцы стоп",
    "toe_gap": "Просвет между пальцами",
    "foot_focus": "Акцент на стопах",
    "feet": "Стопы",
    "foot": "Стопа",
    "high_arch": "Высокий свод стопы",
    "arch_(foot)": "Свод стопы",
    "heel": "Пятка",
    "heels": "Пятки",
    "ankle": "Лодыжка",
    "ankles": "Лодыжки",
    "toenails": "Ногти на ногах",
    "toenail_polish": "Лак на ногтях ног",
    "painted_nails": "Педикюр / лак",
    "painted_toenails": "Накрашенные ногти стоп",
    "pedicure": "Педикюр",
    "nail_polish": "Лак для ногтей",
    "fingernails": "Ногти на руках",
    "long_fingernails": "Длинные ногти",
    "wrinkled_sole": "Складки на подошве",
    "wrinkled_skin": "Складки кожи",
    "sole_lines": "Линии на подошве",
    "veins_tendons": "Вены и сухожилия",
    "veins": "Вены",
    "tendons": "Сухожилия",
    "wet_sweaty": "Влажная кожа / пот",
    "sweat": "Пот",
    "sweatdrop": "Капля пота",
    "flying_sweatdrops": "Брызги пота",
    "wet": "Мокрая кожа",
    "wet_skin": "Мокрая кожа",
    "wet_hair": "Мокрые волосы",
    "wet_clothes": "Мокрая одежда",
    "dirty_sole": "Грязные подошвы",
    "dirty_feet": "Грязные стопы",
    "dirty": "Грязь / пыль",
    "mud": "Грязь",
    "dust": "Пыль",
    "micro_people": "Микро-люди / масштаб",
    "micro_city": "Микро-город",
    "giantess": "Великанша",
    "giant": "Гигант",
    "minigirl": "Мини-девушка",
    "miniboy": "Мини-парень",
    "miniguy": "Микро-человек",
    "size_difference": "Разница в размерах",
    "shrinking": "Уменьшение",
    "growth": "Увеличение",
    "unaware": "Не замечает микро",
    "cruel": "Властная / жестокая",
    "domination": "Доминирование",
    "femdom": "Фемдом",
    "smug": "Ухмылка / надменность",
    "steaming_body": "Пар от тела",
    "steam": "Пар",
    "smell": "Запах / аромат",
    "odor_lines": "Линии запаха",
    "saliva": "Слюна",
    "licking": "Облизывание",
    "licking_lips": "Облизывает губы",
    "licking_foot": "Лижет стопу",
    "kissing_foot": "Целует стопу",
    "foot_in_face": "Стопа на лице",
    "shoe_dangle": "Болтающаяся туфля",
    "removing_shoes": "Снимает обувь",
    "removing_socks": "Снимает носки",
    "sock_pull": "Стягивает носок",
    "one_shoe_off": "Одна туфля снята",
    "one_sock_off": "Один носок снят",
    "anklet": "Браслет на лодыжке",
    "toe_ring": "Кольцо на пальце ноги",

    # Чулки, носки, обувь и одежда
    "stockings": "Чулки",
    "pantyhose": "Колготки",
    "thighhighs": "Гольфы выше колен",
    "kneehighs": "Гольфы до колен",
    "socks": "Носки",
    "loose_socks": "Свободные носки",
    "bobby_socks": "Короткие носочки",
    "ankle_socks": "Короткие носки",
    "tabi": "Носки таби",
    "stirrup_legwear": "Чулки со штрипками",
    "toeless_legwear": "Чулки с открытыми пальцами",
    "torn_pantyhose": "Порванные колготки",
    "torn_stockings": "Порванные чулки",
    "torn_thighhighs": "Порванные гольфы",
    "wet_pantyhose": "Мокрые колготки",
    "see-through_legwear": "Полупрозрачные чулки",
    "striped_thighhighs": "Полосатые гольфы",
    "striped_socks": "Полосатые носки",
    "black_thighhighs": "Чёрные гольфы",
    "white_thighhighs": "Белые гольфы",
    "pink_thighhighs": "Розовые гольфы",
    "black_pantyhose": "Чёрные колготки",
    "white_pantyhose": "Белые колготки",
    "brown_pantyhose": "Бежевые / капроновые колготки",
    "black_stockings": "Чёрные чулки",
    "white_stockings": "Белые чулки",
    "black_socks": "Чёрные носки",
    "white_socks": "Белые носки",
    "leg_warmers": "Гетры",
    "garter_belt": "Пояс для чулок",
    "high_heels": "Высокие каблуки",
    "sandals": "Сандалии / босоножки",
    "gladiator_sandals": "Сандалии с ремешками",
    "clogs": "Сабо",
    "geta": "Гэта",
    "zouri": "Дзори",
    "flip-flops": "Шлёпанцы",
    "slippers": "Тапочки",
    "shoes": "Обувь",
    "footwear": "Обувь",
    "boots": "Сапоги / ботинки",
    "thigh_boots": "Ботфорты",
    "knee_boots": "Сапоги до колен",
    "ankle_boots": "Ботильоны",
    "sneakers": "Кроссовки",
    "CONVERSE": "Кеды",
    "loafers": "Лоферы",
    "mary_janes": "Туфли Мэри Джейн",
    "platform_footwear": "Обувь на платформе",
    "wedge_heels": "Танкетка",
    "strappy_heels": "Босоножки на ремешках",
    "ankle_strap": "Ремешок на щиколотке",
    "bare_legs": "Голые ноги",
    "legs": "Ноги",
    "bare_shoulders": "Оголённые плечи",
    "bare_arms": "Оголённые руки",
    "bare_back": "Оголённая спина",
    "midriff": "Оголённый живот",
    "shirt": "Рубашка / футболка",
    "t-shirt": "Футболка",
    "collared_shirt": "Рубашка с воротником",
    "blouse": "Блузка",
    "sweater": "Свитер",
    "ribbed_sweater": "Свитер в рубчик",
    "turtleneck": "Водолазка",
    "virgin_killer_sweater": "Открытый свитер",
    "hoodie": "Худи / толстовка",
    "jacket": "Куртка / пиджак",
    "coat": "Пальто",
    "cardigan": "Кардиган",
    "vest": "Жилет",
    "crop_top": "Топ",
    "tank_top": "Майка",
    "tube_top": "Топ без бретелек",
    "halterneck": "Халтер (бретель через шею)",
    "off_shoulder": "Открытые плечи",
    "dress": "Платье",
    "sundress": "Сарафан",
    "evening_gown": "Вечернее платье",
    "wedding_dress": "Свадебное платье",
    "china_dress": "Ципао (китайское платье)",
    "skirt": "Юбка",
    "miniskirt": "Мини-юбка",
    "pleated_skirt": "Плиссированная юбка",
    "pencil_skirt": "Юбка-карандаш",
    "long_skirt": "Длинная юбка",
    "shorts": "Шорты",
    "denim_shorts": "Джинсовые шорты",
    "short_shorts": "Короткие шортики",
    "gym_shorts": "Спортивные шорты",
    "bike_shorts": "Велосипедки",
    "pants": "Брюки / штаны",
    "jeans": "Джинсы",
    "leggings": "Леггинсы / лосины",
    "sweatpants": "Спортивные штаны",
    "school_uniform": "Школьная форма",
    "serafuku": "Матроска (школьная форма)",
    "sailor_collar": "Матросский воротник",
    "maid": "Горничная",
    "maid_headdress": "Чепчик горничной",
    "apron": "Фартук",
    "nurse": "Медсестра",
    "nun": "Монахиня",
    "miko": "Мико (жрица)",
    "kimono": "Кимоно",
    "yukata": "Юката",
    "japanese_clothes": "Японская одежда",
    "military_uniform": "Военная форма",
    "police_uniform": "Полицейская форма",
    "office_lady": "Офисный стиль",
    "suit": "Костюм",
    "business_suit": "Деловой костюм",
    "gym_uniform": "Спортивная форма",
    "buruma": "Бурума (спорт. шорты)",
    "sportswear": "Спортивная одежда",
    "track_jacket": "Олимпийка",
    "cheerleader": "Чирлидерша",
    "playboy_bunny": "Костюм кролика",
    "bunny_ears": "Кроличьи ушки",
    "fake_animal_ears": "Ушки-ободок",
    "cat_ears": "Кошачьи ушки",
    "fox_ears": "Лисьи ушки",
    "dog_ears": "Собачьи ушки",
    "elf": "Эльфийка",
    "pointy_ears": "Острые ушки",
    "demon_girl": "Демонесса",
    "horns": "Рожки",
    "demon_tail": "Хвост демона",
    "tail": "Хвост",
    "cat_tail": "Кошачий хвост",
    "fox_tail": "Лисий хвост",
    "wings": "Крылья",
    "angel_wings": "Крылья ангела",
    "demon_wings": "Крылья демона",
    "bat_wings": "Крылья летучей мыши",
    "halo": "Нимб",
    "witch_hat": "Шляпа ведьмы",
    "hat": "Шляпа / шапка",
    "baseball_cap": "Бейсболка",
    "beret": "Берет",
    "beanie": "Шапка бини",
    "crown": "Корона",
    "tiara": "Тиара",
    "hairband": "Ободок для волос",
    "hair_ribbon": "Лента в волосах",
    "ribbon": "Лента",
    "bow": "Бант",
    "hair_bow": "Бант в волосах",
    "hair_ornament": "Украшение в волосах",
    "hairclip": "Заколка",
    "hairpin": "Шпилька",
    "hair_flower": "Цветок в волосах",
    "scrunchie": "Резинка для волос",
    "glasses": "Очки",
    "sunglasses": "Солнцезащитные очки",
    "goggles": "Защитные очки",
    "eyepatch": "Повязка на глаз",
    "mask": "Маска",
    "mouth_mask": "Маска на лице",
    "scarf": "Шарф",
    "necktie": "Галстук",
    "bowtie": "Галстук-бабочка",
    "choker": "Чокер",
    "necklace": "Ожерелье / кулон",
    "earrings": "Серьги",
    "jewelry": "Украшения",
    "bracelet": "Браслет",
    "wristband": "Напульсник",
    "ring": "Кольцо",
    "gloves": "Перчатки",
    "fingerless_gloves": "Перчатки без пальцев",
    "elbow_gloves": "Длинные перчатки",
    "white_gloves": "Белые перчатки",
    "black_gloves": "Чёрные перчатки",
    "belt": "Ремень / пояс",
    "bag": "Сумка",
    "backpack": "Рюкзак",
    "umbrella": "Зонт",

    # Волосы и глаза
    "long_hair": "Длинные волосы",
    "very_long_hair": "Очень длинные волосы",
    "short_hair": "Короткие волосы",
    "medium_hair": "Волосы средней длины",
    "blonde_hair": "Светлые волосы (блондинка)",
    "black_hair": "Чёрные волосы",
    "brown_hair": "Каштановые волосы",
    "red_hair": "Рыжие / красные волосы",
    "pink_hair": "Розовые волосы",
    "blue_hair": "Синие волосы",
    "light_blue_hair": "Голубые волосы",
    "purple_hair": "Фиолетовые волосы",
    "green_hair": "Зелёные волосы",
    "white_hair": "Белые волосы",
    "grey_hair": "Пепельные / серые волосы",
    "silver_hair": "Серебристые волосы",
    "orange_hair": "Оранжевые волосы",
    "multicolored_hair": "Разноцветные волосы",
    "streaked_hair": "Пряди другого цвета",
    "gradient_hair": "Градиент на волосах",
    "twintails": "Два хвостика",
    "low_twintails": "Низкие хвостики",
    "ponytail": "Конский хвост",
    "high_ponytail": "Высокий хвост",
    "side_ponytail": "Хвост набок",
    "braid": "Коса",
    "twin_braids": "Две косички",
    "hair_bun": "Пучок волос",
    "double_bun": "Два пучка",
    "bob_cut": "Каре",
    "hime_cut": "Стрижка химэ",
    "ahoge": "Ахогэ (торчащая прядь)",
    "bangs": "Чёлка",
    "blunt_bangs": "Прямая чёлка",
    "parted_bangs": "Разделённая чёлка",
    "swept_bangs": "Чёлка набок",
    "hair_between_eyes": "Прядь между глаз",
    "hair_over_one_eye": "Волосы закрывают глаз",
    "sidelocks": "Боковые пряди у лица",
    "wavy_hair": "Волнистые волосы",
    "curly_hair": "Кудрявые волосы",
    "straight_hair": "Прямые волосы",
    "messy_hair": "Растрёпанные волосы",
    "floating_hair": "Развевающиеся волосы",
    "blue_eyes": "Голубые глаза",
    "red_eyes": "Красные глаза",
    "green_eyes": "Зелёные глаза",
    "brown_eyes": "Карие глаза",
    "yellow_eyes": "Жёлтые глаза",
    "purple_eyes": "Фиолетовые глаза",
    "pink_eyes": "Розовые глаза",
    "aqua_eyes": "Бирюзовые глаза",
    "orange_eyes": "Оранжевые глаза",
    "grey_eyes": "Серые глаза",
    "black_eyes": "Чёрные глаза",
    "heterochromia": "Гетерохромия (разные глаза)",
    "glowing_eyes": "Светящиеся глаза",
    "heart-shaped_pupils": "Сердечки в глазах",
    "slit_pupils": "Вертикальные зрачки",

    # Эмоции и лицо
    "smile": "Улыбка",
    "grin": "Усмешка / оскал",
    "laughing": "Смеётся",
    "open_mouth": "Открытый рот",
    "closed_mouth": "Закрытый рот",
    "parted_lips": "Приоткрытые губы",
    "lips": "Губы",
    "lipstick": "Помада",
    "tongue": "Язык",
    "tongue_out": "Высунутый язык",
    "teeth": "Зубы",
    "fang": "Клык",
    "fangs": "Клыки",
    "closed_eyes": "Закрытые глаза",
    "one_eye_closed": "Подмигивание",
    "wink": "Подмигивает",
    "half-closed_eyes": "Полуприкрытые глаза",
    "narrowed_eyes": "Прищуренный взгляд",
    "wide-eyed": "Широко раскрытые глаза",
    "crying": "Плачет",
    "tears": "Слёзы",
    "angry": "Злость",
    "annoyed": "Раздражение",
    "frown": "Хмурый взгляд",
    "pout": "Надутые губки",
    "embarrassed": "Смущение",
    "shy": "Застенчивость",
    "nervous": "Нервничает",
    "surprised": "Удивление",
    "scared": "Испуг",
    "expressionless": "Безэмоциональное лицо",
    "serious": "Серьёзный взгляд",
    "seductive_smile": "Соблазнительная улыбка",
    "naughty_face": "Игривое лицо",
    "sleepy": "Сонный вид",
    "sleeping": "Спит",
    "yawning": "Зевает",
    "makeup": "Макияж",
    "eyelashes": "Ресницы",
    "eyeshadow": "Тени для век",
    "freckles": "Веснушки",
    "mole": "Родинка",
    "mole_under_eye": "Родинка под глазом",
    "scar": "Шрам",
    "bandaid": "Пластырь",
    "bandages": "Бинты",
    "tattoo": "Татуировка",
    "dark_skin": "Тёмная кожа / мулатка",
    "dark-skinned_female": "Темнокожая девушка",
    "tan": "Загар",
    "tanlines": "Линии загара",
    "pale_skin": "Бледная кожа",
    "shiny_skin": "Блестящая кожа",
    "shiny": "Блеск / глянец",
    "shiny_hair": "Блестящие волосы",
    "shiny_clothes": "Блестящая одежда",
    "muscular": "Мускулистое тело",
    "muscular_female": "Спортивная девушка",
    "abs": "Пресс",
    "plump": "Пышные формы",
    "curvy": "Фигуристая",
    "slender": "Стройная",
    "petite": "Миниатюрная",

    # Окружение, свет и композиция
    "outdoors": "На улице",
    "indoors": "В помещении",
    "city": "Город",
    "cityscape": "Городской пейзаж",
    "skyscraper": "Небоскрёб",
    "building": "Здание",
    "street": "Улица",
    "road": "Дорога",
    "sidewalk": "Тротуар",
    "bridge": "Мост",
    "ruins": "Руины",
    "bedroom": "Спальня",
    "bed": "Кровать",
    "on_bed": "На кровати",
    "bed_sheet": "Простыня",
    "pillow": "Подушка",
    "blanket": "Одеяло",
    "couch": "Диван",
    "sofa": "Диван",
    "on_couch": "На диване",
    "chair": "Стул / кресло",
    "armchair": "Кресло",
    "desk": "Письменный стол",
    "table": "Стол",
    "window": "Окно",
    "curtains": "Шторы",
    "door": "Дверь",
    "mirror": "Зеркало",
    "floor": "Пол",
    "on_floor": "На полу",
    "wooden_floor": "Деревянный пол",
    "tile_floor": "Плиточный пол",
    "carpet": "Ковёр",
    "rug": "Коврик",
    "stairs": "Лестница",
    "bathroom": "Ванная комната",
    "bathtub": "Ванна",
    "shower": "Душ",
    "classroom": "Классная комната",
    "locker_room": "Раздевалка",
    "gym": "Спортзал",
    "kitchen": "Кухня",
    "living_room": "Гостиная",
    "car": "Автомобиль",
    "car_interior": "Салон автомобиля",
    "train": "Поезд",
    "train_interior": "Вагон поезда",
    "forest": "Лес",
    "tree": "Дерево",
    "grass": "Трава",
    "flower": "Цветок",
    "flowers": "Цветы",
    "petals": "Лепестки",
    "cherry_blossoms": "Сакура",
    "beach": "Пляж",
    "sand": "Песок",
    "ocean": "Океан / море",
    "sea": "Море",
    "pool": "Бассейн",
    "poolside": "У бассейна",
    "onsen": "Горячий источник (онсэн)",
    "water": "Вода",
    "underwater": "Под водой",
    "splashing": "Брызги воды",
    "rain": "Дождь",
    "snow": "Снег",
    "sky": "Небо",
    "blue_sky": "Голубое небо",
    "clouds": "Облака",
    "cloudy_sky": "Облачное небо",
    "day": "День",
    "sunset": "Закат",
    "twilight": "Сумерки",
    "night": "Ночь",
    "night_sky": "Ночное небо",
    "starry_sky": "Звёздное небо",
    "moon": "Луна",
    "full_moon": "Полная луна",
    "sunlight": "Солнечный свет",
    " dappled_sunlight": "Солнечные блики",
    "lens_flare": "Блик объектива",
    "backlighting": "Контровой свет",
    "rim_lighting": "Контурный свет",
    "dramatic_lighting": "Драматичный свет",
    "sidelighting": "Боковой свет",
    "spotlight": "Точечный свет",
    "neon_lights": "Неоновый свет",
    "glowing": "Свечение",
    "fire": "Огонь",
    "smoke": "Дым",
    "shadow": "Тень",
    "depth_of_field": "Размытие фона (боке)",
    "blurry_background": "Размытый фон",
    "blurry_foreground": "Размытый передний план",
    "motion_blur": "Размытие в движении",
    "chromatic_aberration": "Хроматическая аберрация",
    "simple_background": "Простой фон",
    "white_background": "Белый фон",
    "black_background": "Чёрный фон",
    "grey_background": "Серый фон",
    "gradient_background": "Градиентный фон",
    "transparent_background": "Прозрачный фон",
    "abstract_background": "Абстрактный фон",
    "pattern_background": "Узорчатый фон",
    "heart_background": "Фон с сердечками",
    "halftone": "Полутоновый растр",
    "monochrome": "Монохром",
    "greyscale": "Чёрно-белое",
    "spot_color": "Выборочный цвет",
    "lineart": "Лайнарт",
    "sketch": "Скетч / набросок",
    "pixel_art": "Пиксель-арт",
    "3d": "3D рендер",
    "realistic": "Реализм",
    "photorealistic": "Фотореализм",
    "cel_shading": "Сел-шейдинг",
    "comic": "Комикс",
    "4koma": "Ёнкома (4 кадра)",
    "multiple_views": "Несколько ракурсов / лист персонажа",
    "reference_sheet": "Референс-лист",
    "inset": "Врезка крупным планом",
    "zoom_layer": "Увеличенный фрагмент",
    "cut-in": "Врезка кадра",
    "border": "Рамка",
    "white_border": "Белая рамка",
    "letterboxed": "Кинематографичные поля",
    "speech_bubble": "Речевой пузырь",
    "thought_bubble": "Облачко мыслей",
    "english_text": "Английский текст",
    "japanese_text": "Японский текст",
    "chinese_text": "Китайский текст",
    "korean_text": "Корейский текст",
    "russian_text": "Русский текст",
    "translated": "С переводом",
    "sound_effects": "Звуковые эффекты (SFX)",
    "signature": "Подпись автора",
    "watermark": "Водяной знак",
    "date": "Дата",
    "heart": "Сердечко",
    "hearts": "Сердечки",
    "sparkles": "Искры / блёстки",
    " musical_note": "Ноты",

    # NSFW / 18+ (nsfw:*)
    "nude": "Обнажённая",
    "completely_nude": "Полностью обнажённая",
    "topless": "Топлес",
    "bottomless": "Без низа",
    "uncensored": "Без цензуры",
    "censored": "Цензура",
    "mosaic_censoring": "Мозаичная цензура",
    "bar_censor": "Полоски цензуры",
    "light_censor": "Световая цензура",
    "steam_censor": "Цензура паром",
    "explicit": "Откровенное 18+",
    "erotic": "Эротика",
    "fetish": "Фетиш",
    "underwear": "Нижнее бельё",
    "panties": "Трусики",
    "white_panties": "Белые трусики",
    "black_panties": "Чёрные трусики",
    "pink_panties": "Розовые трусики",
    "striped_panties": "Полосатые трусики",
    "lace_panties": "Кружевные трусики",
    "string_panties": "Трусики на завязках",
    "side-tie_panties": "Трусики с бантиками по бокам",
    "thong": "Стринги",
    "g-string": "Тонкие стринги",
    "panties_aside": "Отодвинутые трусики",
    "panties_under_pantyhose": "Трусики под колготками",
    "no_panties": "Без трусиков",
    "no_bra": "Без бюстгальтера",
    "panty_pull": "Стягивает трусики",
    "pantyshot": "Панцушот",
    "upskirt": "Заглядывание под юбку",
    "lingerie": "Кружевное бельё",
    "bra": "Бюстгальтер",
    "sports_bra": "Спортивный топ-бра",
    "bikini": "Бикини",
    "micro_bikini": "Микро-бикини",
    "string_bikini": "Бикини на завязках",
    "slingshot_swimsuit": "Открытый купальник",
    "swimsuit": "Купальник",
    "one-piece_swimsuit": "Слитный купальник",
    "school_swimsuit": "Школьный купальник",
    "competition_swimsuit": "Спортивный купальник",
    "see_through": "Прозрачная одежда",
    "see-through": "Прозрачная одежда",
    "wet_shirt": "Мокрая рубашка",
    "garter_straps": "Подвязки для чулок",
    "thigh_strap": "Ремешок на бедре",
    "fishnets": "Чулки в сеточку",
    "fishnet_pantyhose": "Колготки в сеточку",
    "fishnet_thighhighs": "Гольфы в сеточку",
    "latex": "Латекс",
    "leather": "Кожаная одежда",
    "bodysuit": "Боди / комбинезон",
    "leotard": "Купальник-боди",
    "corset": "Корсет",
    "breasts": "Грудь",
    "large_breasts": "Большая грудь",
    "huge_breasts": "Огромная грудь",
    "gigantic_breasts": "Гигантская грудь",
    "medium_breasts": "Средняя грудь",
    "small_breasts": "Маленькая грудь",
    "flat_chest": "Плоская грудь",
    "nipples": "Соски",
    "puffy_nipples": "Припухшие соски",
    "covered_nipples": "Прикрытые соски (под тканью)",
    "areolae": "Ареолы",
    "cleavage": "Декольте / ложбинка",
    "sideboob": "Грудь сбоку",
    "underboob": "Грудь снизу",
    "bouncing_breasts": "Колышущаяся грудь",
    "breast_grab": "Сжатие груди",
    "breast_press": "Прижатая грудь",
    "breast_hold": "Поддерживает грудь",
    "ass": "Ягодицы",
    "huge_ass": "Большие ягодицы",
    "ass_focus": "Акцент на ягодицах",
    "ass_visible_through_thighs": "Ягодицы видны между бёдер",
    "buttjob": "Трение о ягодицы",
    "spanking": "Шлепок по попе",
    "thighs": "Бёдра",
    "thick_thighs": "Пышные бёдра",
    "thigh_gap": "Просвет между бёдрами",
    "skindentation": "Вмятинка от резинки чулок",
    "thighjob": "Сжатие бёдрами",
    "hips": "Бёдра / таз",
    "wide_hips": "Широкие бёдра",
    "pelvic_curtain": "Разрез платья у бёдер",
    "groin": "Пах",
    "pubic_hair": "Лобковые волосы",
    "pussy": "Киска / интимная зона",
    "pussy_juice": "Влажность / смазка",
    "cameltoe": "Верблюжья лапка",
    " anus": "Анус",
    "anus": "Анус",
    "navel": "Пупок",
    "stomach": "Животик",
    "armpits": "Подмышки",
    "collarbone": "Ключицы",
    "nape": "Затылок / шея",
    "presenting": "Демонстративная поза",
    "bondage": "Бондаж / связывание",
    "bound": "Связанная",
    "bound_wrists": "Связанные запястья",
    "bound_ankles": "Связанные лодыжки",
    "shibari": "Шибари (верёвки)",
    "rope": "Верёвка",
    "chains": "Цепи",
    "handcuffs": "Наручники",
    "collar": "Ошейник",
    "leash": "Поводок",
    "gag": "Кляп",
    "ball_gag": "Кляп-шарик",
    "tape_gag": "Заклеенный рот",
    "blindfold": "Повязка на глазах",
    "bdsm": "БДСМ",
    "footjob": "Футджоб",
    "two-footed_footjob": "Футджоб двумя стопами",
    "shoejob": "Футджоб в обуви",
    "paizuri": "Пайзури (между грудей)",
    "handjob": "Хэндджоб (рукой)",
    "fellatio": "Минет / орал",
    "oral": "Оральный секс",
    "cunnilingus": "Куннилингус",
    "deepthroat": "Глубокий минет",
    "sex": "Секс",
    "vaginal": "Вагинальный секс",
    "anal": "Анальный секс",
    "doggystyle": "Догги-стайл",
    "cowgirl_position": "Поза наездницы",
    "reverse_cowgirl": "Обратная наездница",
    "missionary": "Миссионерская поза",
    "mating_press": "Мейтинг-пресс",
    "masturbation": "Мастурбация",
    "fingering": "Фингеринг",
    "sex_toy": "Секс-игрушка",
    "vibrator": "Вибратор",
    "dildo": "Дилдо",
    "butt_plug": "Анальная пробка",
    "cum": "Сперма",
    "cum_on_feet": "Сперма на стопах",
    "cum_on_soles": "Сперма на подошвах",
    "cum_on_body": "Сперма на теле",
    "cum_on_face": "Сперма на лице",
    "cum_on_breasts": "Сперма на груди",
    "cum_in_mouth": "Сперма во рту",
    "cum_in_pussy": "Кремпай",
    "creampie": "Кремпай",
    "facial": "На лицо",
    "bukkake": "Буккаке",
    "precum": "Предэякулят",
    "ejaculation": "Эякуляция",
    "orgasm": "Оргазм",
    "ahegao": "Ахегао",
    "torogao": "Торогао (истома)",
    "blush": "Румянец",
    "heavy_breathing": "Тяжёлое дыхание",
    "trembling": "Дрожь",
    "penis": "Пенис / член",
    "erection": "Эрекция",
    "cock": "Пенис",
    "large_penis": "Большой пенис",
    "veiny_penis": "Венозный пенис",
    "testicles": "Яички",
    "balls": "Яички",
    "futa": "Футанари",
    "futanari": "Футанари",
    "hermaphrodite": "Футанари / гермафродит",
    "femboy": "Фембой",
    "otoko_no_ko": "Фембой",
    "crossdressing": "Кроссдрессинг / фембой",
    "trap": "Трап / фембой",
    "girly": "Женственный парень",
    "yaoi": "Яой",
    "yuri": "Юри (лесби)",
    "tentacles": "Тентакли",
    "lactation": "Лактация",
}

# Словарь морфем и основ для композитного перевода тегов
WORD_RU_PARTS: Dict[str, str] = {
    "white": "Бел.",
    "black": "Чёрн.",
    "red": "Красн.",
    "blue": "Син.",
    "light_blue": "Голуб.",
    "aqua": "Бирюз.",
    "green": "Зелён.",
    "yellow": "Жёлт.",
    "pink": "Розов.",
    "purple": "Фиолетов.",
    "orange": "Оранжев.",
    "brown": "Коричнев.",
    "grey": "Сер.",
    "silver": "Серебрист.",
    "gold": "Золот.",
    "golden": "Золотист.",
    "dark": "Тёмн.",
    "light": "Светл.",
    "bright": "Ярк.",
    "pale": "Бледн.",
    "neon": "Неонов.",
    "pastel": "Пастельн.",
    "multicolored": "Разноцветн.",
    "two-tone": "Двухцветн.",
    "striped": "Полосат.",
    "plaid": "Клетчат.",
    "polka_dot": "В горошек",
    "checkered": "В клетку",
    "floral": "Цветочн.",
    "frilled": "С оборками",
    "lace": "Кружевн.",
    "ribbed": "В рубчик",
    "knitted": "Вязан.",
    "leather": "Кожан.",
    "latex": "Латексн.",
    "denim": "Джинсов.",
    "silk": "Шёлков.",
    "satin": "Атласн.",
    "sheer": "Полупрозрачн.",
    "transparent": "Прозрачн.",
    "translucent": "Полупрозрачн.",
    "wet": "Мокр.",
    "sweaty": "Потн.",
    "dirty": "Грязн.",
    "muddy": "В грязи",
    "bloody": "В крови",
    "torn": "Порван.",
    "ripped": "Рван.",
    "tight": "Обтягивающ.",
    "loose": "Свободн.",
    "baggy": "Мешковат.",
    "sleeveless": "Без рукавов",
    "strapless": "Без бретелек",
    "backless": "С открытой спиной",
    "open": "Открыт.",
    "closed": "Закрыт.",
    "unbuttoned": "Расстёгнут.",
    "unzipped": "Расстёгнут.",
    "lifted": "Приподнят.",
    "pulled": "Оттянут.",
    "removed": "Снят.",
    "single": "Один",
    "double": "Двойн.",
    "multiple": "Несколько",
    "long": "Длинн.",
    "short": "Коротк.",
    "large": "Больш.",
    "big": "Больш.",
    "huge": "Огромн.",
    "giant": "Гигантск.",
    "small": "Маленьк.",
    "tiny": "Крошечн.",
    "mini": "Мини",
    "micro": "Микро",
    "high": "Высок.",
    "low": "Низк.",
    "deep": "Глубок.",
    "wide": "Широк.",
    "narrow": "Узк.",
    "thick": "Пышн.",
    "thin": "Тонк.",
    "slender": "Стройн.",
    "bare": "Гол.",
    "naked": "Обнажён.",
    "exposed": "Оголён.",
    "covered": "Прикрыт.",
    "raised": "Поднят.",
    "crossed": "Скрещен.",
    "spread": "Разведён.",
    "bent": "Согнут.",
    "folded": "Сложен.",
    "outstretched": "Вытянут.",
    "holding": "Держит",
    "grabbing": "Сжимает",
    "touching": "Касается",
    "rubbing": "Трёт",
    "licking": "Лижет",
    "kissing": "Целует",
    "biting": "Кусает",
    "sucking": "Сосёт",
    "looking": "Смотрит",
    "watching": "Наблюдает",
    "smiling": "Улыбается",
    "crying": "Плачет",
    "blushing": "Краснеет",
    "sitting": "Сидя",
    "standing": "Стоя",
    "lying": "Лёжа",
    "walking": "Шагает",
    "running": "Бежит",
    "floating": "Парит",
    "hair": "волосы",
    "eyes": "глаза",
    "eye": "глаз",
    "face": "лицо",
    "head": "голова",
    "ear": "ухо",
    "ears": "уши",
    "mouth": "рот",
    "lips": "губы",
    "tongue": "язык",
    "teeth": "зубы",
    "neck": "шея",
    "shoulder": "плечо",
    "shoulders": "плечи",
    "arm": "рука",
    "arms": "руки",
    "hand": "кисть руки",
    "hands": "руки",
    "finger": "палец руки",
    "fingers": "пальцы рук",
    "nails": "ногти",
    "nail": "ноготь",
    "toenails": "ногти стоп",
    "fingernails": "ногти рук",
    "chest": "грудь",
    "breast": "грудь",
    "breasts": "грудь",
    "nipple": "сосок",
    "nipples": "соски",
    "back": "спина",
    "waist": "талия",
    "belly": "живот",
    "stomach": "живот",
    "navel": "пупок",
    "hip": "бедро",
    "hips": "бёдра",
    "ass": "ягодицы",
    "butt": "попа",
    "thigh": "бедро",
    "thighs": "бёдра",
    "knee": "колено",
    "knees": "колени",
    "leg": "нога",
    "legs": "ноги",
    "calf": "голень",
    "ankle": "лодыжка",
    "ankles": "лодыжки",
    "foot": "стопа",
    "feet": "стопы",
    "sole": "подошва",
    "soles": "подошвы",
    "heel": "пятка",
    "heels": "каблуки / пятки",
    "toe": "палец стопы",
    "toes": "пальцы стоп",
    "arch": "свод стопы",
    "skin": "кожа",
    "body": "тело",
    "girl": "девушка",
    "girls": "девушки",
    "boy": "парень",
    "boys": "парни",
    "woman": "женщина",
    "man": "мужчина",
    "female": "жен.",
    "male": "муж.",
    "shirt": "рубашка",
    "dress": "платье",
    "skirt": "юбка",
    "shorts": "шорты",
    "pants": "брюки",
    "jacket": "куртка",
    "coat": "пальто",
    "sweater": "свитер",
    "hoodie": "худи",
    "top": "топ",
    "vest": "жилет",
    "uniform": "форма",
    "suit": "костюм",
    "swimsuit": "купальник",
    "bikini": "бикини",
    "bra": "бюстгальтер",
    "panties": "трусики",
    "underwear": "бельё",
    "lingerie": "бельё",
    "leotard": "боди",
    "bodysuit": "комбинезон",
    "pantyhose": "колготки",
    "stockings": "чулки",
    "thighhighs": "гольфы",
    "kneehighs": "гольфы до колен",
    "socks": "носки",
    "sock": "носок",
    "legwear": "чулочные изделия",
    "footwear": "обувь",
    "shoes": "туфли / обувь",
    "shoe": "туфля",
    "boots": "сапоги",
    "boot": "сапог",
    "sandals": "сандалии",
    "sandal": "сандалия",
    "slippers": "тапочки",
    "sneakers": "кроссовки",
    "gloves": "перчатки",
    "glove": "перчатка",
    "hat": "шляпа / шапка",
    "cap": "кепка",
    "ribbon": "лента",
    "bow": "бант",
    "choker": "чокер",
    "collar": "воротник / ошейник",
    "necklace": "ожерелье",
    "earrings": "серьги",
    "bracelet": "браслет",
    "ring": "кольцо",
    "glasses": "очки",
    "mask": "маска",
    "wings": "крылья",
    "tail": "хвост",
    "horns": "рога",
    "flower": "цветок",
    "flowers": "цветы",
    "rose": "роза",
    "tree": "дерево",
    "water": "вода",
    "sky": "небо",
    "cloud": "облако",
    "clouds": "облака",
    "light": "свет",
    "lighting": "освещение",
    "shadow": "тень",
    "background": "фон",
    "border": "рамка",
    "view": "вид",
    "focus": "акцент",
    "pose": "поза",
    "print": "принт",
    "pattern": "узор",
    "ornament": "украшение",
    "accessory": "аксессуар",
    "weapon": "оружие",
    "sword": "меч",
    "gun": "пистолет / оружие",
    "book": "книга",
    "phone": "телефон",
    "cellphone": "смартфон",
    "smartphone": "смартфон",
    "camera": "камера",
    "cup": "чашка",
    "glass": "бокал / стакан",
    "bottle": "бутылка",
    "food": "еда",
    "fruit": "фрукты",
    "cat": "кошка",
    "dog": "собака",
    "bunny": "кролик",
    "rabbit": "кролик",
    "fox": "лиса",
    "wolf": "волк",
    "bird": "птица",
    "butterfly": "бабочка",
}


def translate_single_tag_ru(raw_tag: str, db_cache: Optional[Dict[str, str]] = None) -> str:
    """Переводит любой тег (включая префиксы detail:, angle:, pose:, nsfw:, char:) на русский язык."""
    if not raw_tag:
        return ""
    clean = raw_tag.strip()
    ns = ""
    short = clean
    if ":" in clean:
        ns, short = clean.split(":", 1)

    short_low = short.lower().strip()
    if ns == "char":
        return short.replace("_", " ").title()

    if db_cache:
        if clean.lower() in db_cache:
            return db_cache[clean.lower()]
        if short_low in db_cache:
            return db_cache[short_low]

    if short_low in TAG_RU_DICT:
        return TAG_RU_DICT[short_low]

    # Нормализация суффикса уточнения вида tag_(context)
    no_paren = re.sub(r"_\(.*?\)", "", short_low)
    if no_paren in TAG_RU_DICT:
        return TAG_RU_DICT[no_paren]

    # Пословный перевод составных тегов
    parts = [p for p in no_paren.split("_") if p]
    if parts:
        translated_parts = []
        any_translated = False
        i = 0
        while i < len(parts):
            if i + 1 < len(parts):
                pair = f"{parts[i]}_{parts[i+1]}"
                if pair in TAG_RU_DICT:
                    translated_parts.append(TAG_RU_DICT[pair].lower())
                    any_translated = True
                    i += 2
                    continue
                if pair in WORD_RU_PARTS:
                    translated_parts.append(WORD_RU_PARTS[pair])
                    any_translated = True
                    i += 2
                    continue
            w = parts[i]
            if w in WORD_RU_PARTS:
                translated_parts.append(WORD_RU_PARTS[w])
                any_translated = True
            elif w in TAG_RU_DICT:
                translated_parts.append(TAG_RU_DICT[w].lower())
                any_translated = True
            else:
                translated_parts.append(w)
            i += 1

        if any_translated:
            res = " ".join(translated_parts).strip()
            if res:
                return res[0].upper() + res[1:]

    return short.replace("_", " ")

PUBLIC_ANGLE_TAGS: Dict[str, str] = {
    "from_below": "angle:from_below",
    "from_above": "angle:from_above",
    "from_behind": "angle:from_behind",
    "from_side": "angle:from_side",
    "pov": "angle:pov",
    "pov_hands": "angle:pov_hands",
    "pov_crotch": "angle:from_below",
    "close-up": "angle:closeup",
    "full_body": "angle:full_body",
    "lower_body": "angle:lower_body",
    "upper_body": "angle:upper_body",
    "cowboy_shot": "angle:cowboy_shot",
    "portrait": "angle:portrait",
    "wide_shot": "angle:wide_shot",
    "foreshortening": "angle:foreshortening",
    "dutch_angle": "angle:dutch_angle",
    "fisheye": "angle:fisheye",
    "panoramic": "angle:panoramic",
    "profile": "angle:profile",
    "back": "angle:back_view",
    "straight-on": "angle:straight_on",
}

PUBLIC_POSE_TAGS: Dict[str, str] = {
    "standing": "pose:standing",
    "sitting": "pose:sitting",
    "squatting": "pose:squatting",
    "kneeling": "pose:kneeling",
    "lying": "pose:lying",
    "on_back": "pose:on_back",
    "on_stomach": "pose:on_stomach",
    "on_side": "pose:on_side",
    "all_fours": "pose:all_fours",
    "leaning_forward": "pose:leaning_forward",
    "leaning_back": "pose:leaning_back",
    "arched_back": "pose:arched_back",
    "arms_up": "pose:arms_up",
    "arms_behind_back": "pose:arms_behind_back",
    "arms_behind_head": "pose:arms_behind_head",
    "crossed_arms": "pose:crossed_arms",
    "crossed_legs": "pose:crossed_legs",
    "legs_up": "pose:legs_up",
    "spread_legs": "pose:spread_legs",
    "straddling": "pose:straddling",
    "tiptoes": "pose:tiptoe",
    "spread_toes": "pose:spread_toes",
    "clenched_toes": "pose:curled_toes",
    "curled_toes": "pose:curled_toes",
    "stepping": "pose:stepping",
    "trampling": "pose:crushing",
    "crushing": "pose:crushing",
    "foot_worship": "pose:foot_worship",
    "walking": "pose:walking",
    "running": "pose:running",
    "jumping": "pose:jumping",
    "floating": "pose:floating",
    "flying": "pose:flying",
    "stretching": "pose:stretching",
    "hugging": "pose:hugging",
    "fighting_stance": "pose:fighting_stance",
    "reaching": "pose:reaching",
    "looking_at_viewer": "pose:looking_at_viewer",
    "looking_back": "pose:looking_back",
    "looking_up": "pose:looking_up",
    "looking_down": "pose:looking_down",
    "looking_away": "pose:looking_away",
    "head_tilt": "pose:head_tilt",
    "peace_sign": "pose:peace_sign",
    "pointing": "pose:pointing",
    "waving": "pose:waving",
    "carrying": "pose:carrying",
    "holding": "pose:holding",
}

PUBLIC_NSFW_TAGS: Dict[str, str] = {
    "nude": "nsfw:nude",
    "completely_nude": "nsfw:nude",
    "topless": "nsfw:topless",
    "bottomless": "nsfw:bottomless",
    "uncensored": "nsfw:uncensored",
    "censored": "nsfw:censored",
    "underwear": "nsfw:underwear",
    "panties": "nsfw:panties",
    "lingerie": "nsfw:lingerie",
    "bra": "nsfw:bra",
    "bikini": "nsfw:bikini",
    "micro_bikini": "nsfw:bikini",
    "swimsuit": "nsfw:swimsuit",
    "see-through": "nsfw:see_through",
    "garter_straps": "nsfw:garter_straps",
    "fishnets": "nsfw:fishnets",
    "latex": "nsfw:latex",
    "bodysuit": "nsfw:bodysuit",
    "breasts": "nsfw:breasts",
    "large_breasts": "nsfw:large_breasts",
    "huge_breasts": "nsfw:huge_breasts",
    "medium_breasts": "nsfw:medium_breasts",
    "small_breasts": "nsfw:small_breasts",
    "flat_chest": "nsfw:flat_chest",
    "nipples": "nsfw:nipples",
    "cleavage": "nsfw:cleavage",
    "sideboob": "nsfw:sideboob",
    "underboob": "nsfw:underboob",
    "ass": "nsfw:ass",
    "thighs": "nsfw:thighs",
    "thick_thighs": "nsfw:thick_thighs",
    "pussy": "nsfw:pussy",
    "cameltoe": "nsfw:cameltoe",
    "navel": "nsfw:navel",
    "presenting": "nsfw:presenting",
    "bondage": "nsfw:bondage",
    "bound": "nsfw:bondage",
    "shibari": "nsfw:shibari",
    "collar": "nsfw:collar",
    "leash": "nsfw:leash",
    "gag": "nsfw:gag",
    "blindfold": "nsfw:blindfold",
    "footjob": "nsfw:footjob",
    "paizuri": "nsfw:paizuri",
    "handjob": "nsfw:handjob",
    "fellatio": "nsfw:fellatio",
    "sex": "nsfw:sex",
    "vaginal": "nsfw:vaginal",
    "anal": "nsfw:anal",
    "cum": "nsfw:cum",
    "ahegao": "nsfw:ahegao",
    "blush": "nsfw:blush",
    "penis": "nsfw:penis",
    "erection": "nsfw:penis",
    "cock": "nsfw:penis",
    "testicles": "nsfw:testicles",
    "futanari": "nsfw:futa",
    "futa": "nsfw:futa",
    "hermaphrodite": "nsfw:futa",
    "otoko_no_ko": "nsfw:femboy",
    "femboy": "nsfw:femboy",
    "crossdressing": "nsfw:femboy",
    "trap": "nsfw:femboy",
    "bdsm": "nsfw:bdsm",
    "yuri": "nsfw:yuri",
    "yaoi": "nsfw:yaoi",
    "tentacles": "nsfw:tentacles",
}

PRESET_NSFW_TAGS: List[str] = sorted(set(PUBLIC_NSFW_TAGS.values()) | {"nsfw:explicit", "nsfw:erotic", "nsfw:fetish"})

EVA02_FOOT_EVIDENCE_TAGS = {
    "barefoot", "soles", "sole_focus", "toes", "toe_gap", "foot_focus", "feet",
    "arch_(foot)", "high_arch", "heel", "toenails", "painted_toenails", "pedicure",
    "dirty_feet", "spread_toes", "clenched_toes", "curled_toes", "tiptoes",
    "trampling", "foot_worship", "sandals", "high_heels", "socks",
}

NSFW_EXACT_TAGS: Set[str] = {
    "nude", "completely_nude", "topless", "bottomless", "uncensored", "censored",
    "mosaic_censoring", "bar_censor", "light_censor", "steam_censor", "hair_censor",
    "convenient_censoring", "novelty_censor", "explicit", "erotic", "questionable", "fetish",
    "underwear", "panties", "white_panties", "black_panties", "pink_panties", "striped_panties",
    "lace_panties", "string_panties", "side-tie_panties", "thong", "g-string", "fundoshi",
    "panties_aside", "panties_under_pantyhose", "no_panties", "no_bra", "panty_pull",
    "pantyshot", "upskirt", "lingerie", "bra", "sports_bra", "bikini", "micro_bikini",
    "string_bikini", "slingshot_swimsuit", "swimsuit", "one-piece_swimsuit", "school_swimsuit",
    "competition_swimsuit", "see-through", "see_through", "see-through_clothes",
    "see-through_legwear", "see-through_silhouette", "wet_shirt", "wet_clothes",
    "wet_pantyhose", "torn_clothes", "torn_pantyhose", "torn_stockings", "torn_thighhighs",
    "garter_straps", "garter_belt", "thigh_strap", "fishnets", "fishnet_pantyhose",
    "fishnet_thighhighs", "latex", "bodysuit", "reverse_bunnysuit", "playboy_bunny",
    "leotard", "corset", "virgin_killer_sweater", "naked_apron", "naked_shirt",
    "naked_sweater", "naked_coat", "naked_towel", "naked_ribbon", "pasties",
    "breasts", "large_breasts", "huge_breasts", "gigantic_breasts", "medium_breasts",
    "small_breasts", "flat_chest", "perky_breasts", "sagging_breasts", "bouncing_breasts",
    "breast_press", "breast_grab", "breast_hold", "breast_squeeze", "breast_poke",
    "nipples", "puffy_nipples", "inverted_nipples", "covered_nipples", "nipple_slip",
    "nipple_tweak", "nipple_piercing", "areolae", "areola_slip", "cleavage", "sideboob",
    "underboob", "ass", "huge_ass", "ass_focus", "ass_grab", "ass_visible_through_thighs",
    "buttjob", "spanking", "thighs", "thick_thighs", "thigh_gap", "skindentation",
    "thighjob", "hips", "wide_hips", "pelvic_curtain", "groin", "crotch", "crotch_seam",
    "pubic_hair", "female_pubic_hair", "male_pubic_hair", "pussy", "pussy_juice",
    "spread_pussy", "cameltoe", "clitoris", "labia", "anus", "spread_anus", "navel",
    "presenting", "presenting_pussy", "presenting_ass", "bondage", "bound", "bound_wrists",
    "bound_ankles", "hogtie", "suspension", "shibari", "rope", "chains", "handcuffs",
    "shackles", "collar", "leash", "chain_leash", "gag", "ball_gag", "bit_gag",
    "ring_gag", "tape_gag", "blindfold", "bdsm", "whip", "riding_crop", "spreader_bar",
    "chastity_cage", "footjob", "two-footed_footjob", "shoejob", "paizuri", "handjob",
    "fellatio", "blowjob", "oral", "cunnilingus", "irrumatio", "deepthroat", "sex",
    "vaginal", "anal", "doggystyle", "cowgirl_position", "reverse_cowgirl", "missionary",
    "girl_on_top", "mating_press", "leg_lock", "after_sex", "after_vaginal", "after_anal",
    "after_fellatio", "after_paizuri", "masturbation", "female_masturbation", "fingering",
    "sex_toy", "vibrator", "egg_vibrator", "magic_wand", "dildo", "butt_plug", "anal_beads",
    "anal_tail", "cum", "cum_on_feet", "cum_on_soles", "cum_on_body", "cum_on_face",
    "cum_on_breasts", "cum_on_ass", "cum_on_stomach", "cum_in_mouth", "cum_in_pussy",
    "cum_drip", "cum_pool", "creampie", "facial", "bukkake", "precum", "ejaculation",
    "internal_cumshot", "x-ray", "cross-section", "orgasm", "ahegao", "torogao",
    "fucked_silly", "naughty_face", "seductive_smile", "heavy_breathing", "trembling",
    "saliva", "saliva_trail", "drooling", "penis", "large_penis", "veiny_penis",
    "erection", "bulge", "cock", "testicles", "balls", "futa", "futanari",
    "hermaphrodite", "femboy", "otoko_no_ko", "crossdressing", "trap", "yaoi", "yuri",
    "tentacles", "tentacle_sex", "lactation", "breast_milk", "milking_machine", "peeing",
    "urine", "enema", "humiliation", "exhibitionism", "public_indecency", "voyeurism",
    "groping", "molestation", "body_writing", "pubic_tattoo", "womb_tattoo", "femdom",
    "dominatrix", "slave", "clothes_lift", "shirt_lift", "skirt_lift", "dress_lift",
    "bra_lift", "bikini_lift", "open_shirt", "open_clothes", "unbuttoned", "unzipped",
    "undressing", "removing_bra", "removing_panties", "bandaid_on_pussy", "bandaid_on_nipples",
    "foot_worship", "licking_foot", "kissing_foot", "smelling_feet", "foot_in_mouth",
}

NSFW_SUBSTRINGS = (
    "nude", "naked", "nipple", "areola", "breast", "boob", "pussy", "vagina",
    "clitoris", "labia", "cameltoe", "anus", "anal", "buttjob", "crotch", "groin",
    "pubic", "penis", "cock", "testicle", "erection", "futa", "hermaphrodite",
    "cum", "creampie", "bukkake", "ejaculat", "precum", "sex", "orgasm",
    "masturbat", "fingering", "fellatio", "blowjob", "cunnilingus", "irrumatio",
    "deepthroat", "paizuri", "footjob", "handjob", "thighjob", "shoejob",
    "ahegao", "torogao", "fucked", "censor", "panties", "panty", "thong",
    "g-string", "underwear", "lingerie", "bikini", "swimsuit", "garter",
    "fishnet", "see-through", "see_through", "topless", "bottomless", "upskirt",
    "cleavage", "bondage", "shibari", "bdsm", "gag", "blindfold", "leash",
    "dildo", "vibrator", "tentacle", "lactat", "urine", "peeing", "femdom",
    "dominatrix", "yuri", "yaoi", "femboy", "otoko_no_ko", "crossdressing",
    "exhibitionis", "voyeur", "groping", "molest", "spanking", "womb_tattoo",
    "body_writing", "pasties", "virgin_killer", "reverse_bunny", "mating_press",
    "doggystyle", "cowgirl", "missionary", "ass_focus", "ass_grab", "huge_ass",
    "spread_legs", "straddling", "all_fours",
)


def is_wd_tag_nsfw(raw_tag: str) -> bool:
    """Определяет, относится ли тег к категории 18+ (чтобы он никогда не попадал в «Общие теги»)."""
    clean = raw_tag.split(":", 1)[1] if ":" in raw_tag else raw_tag
    clean = clean.strip().lower()
    if clean in NSFW_EXACT_TAGS:
        return True
    if clean in ("ass", "thighs", "thick_thighs", "bra", "leotard", "corset", "latex", "bodysuit", "blush", "navel"):
        return True
    return any(sub in clean for sub in NSFW_SUBSTRINGS)

RU_SEARCH_SYNONYMS: Dict[str, List[str]] = {
    "босиком": ["detail:barefoot", "barefoot"],
    "подошва": ["detail:soles", "detail:sole_focus", "angle:under_sole", "soles"],
    "подошвы": ["detail:soles", "detail:sole_focus", "angle:under_sole", "soles"],
    "снизу": ["angle:under_sole", "angle:pov_ground", "angle:from_below", "from_below"],
    "сверху": ["angle:top_down", "angle:from_above", "from_above"],
    "сзади": ["angle:back_heel", "angle:from_behind", "angle:back_view", "from_behind"],
    "сбоку": ["angle:side_arch", "angle:from_side", "angle:profile", "from_side"],
    "земли": ["angle:pov_ground", "angle:from_below"],
    "пятка": ["angle:back_heel", "detail:heel", "heel"],
    "пятки": ["angle:back_heel", "detail:heel", "heel"],
    "свод": ["angle:side_arch", "pose:tiptoe", "detail:high_arch", "detail:arch_(foot)"],
    "пальцы": ["detail:toes", "pose:spread_toes", "pose:curled_toes", "toes"],
    "поджатые": ["pose:curled_toes", "curled_toes"],
    "сжатые": ["pose:curled_toes", "clenched_toes"],
    "растопыренные": ["pose:spread_toes", "spread_toes"],
    "веер": ["pose:spread_toes"],
    "мысок": ["pose:tiptoe", "tiptoe"],
    "цыпочки": ["pose:tiptoe", "tiptoes"],
    "шаг": ["pose:stepping", "stepping"],
    "нажим": ["pose:crushing", "crushing"],
    "складки": ["detail:wrinkled_sole", "detail:wrinkled_skin"],
    "морщинки": ["detail:wrinkled_sole", "detail:wrinkled_skin"],
    "вены": ["detail:veins_tendons", "detail:veins"],
    "сухожилия": ["detail:veins_tendons"],
    "влажная": ["detail:wet_sweaty", "detail:wet"],
    "пот": ["detail:wet_sweaty", "detail:sweat"],
    "грязь": ["detail:dirty_sole", "detail:dirty_feet"],
    "пыль": ["detail:dirty_sole"],
    "чулки": ["detail:stockings", "detail:pantyhose", "detail:thighhighs"],
    "колготки": ["detail:pantyhose"],
    "гольфы": ["detail:thighhighs", "detail:kneehighs"],
    "носки": ["detail:socks"],
    "каблуки": ["detail:high_heels"],
    "сандалии": ["detail:sandals"],
    "обувь": ["detail:shoes"],
    "педикюр": ["detail:painted_nails", "detail:toenails", "detail:pedicure"],
    "город": ["detail:micro_city", "detail:city"],
    "люди": ["detail:micro_people", "detail:giantess"],
    "видео": ["detail:video"],
    "video": ["detail:video"],
    "ню": ["nsfw:nude", "nsfw:topless", "nsfw:bottomless"],
    "голая": ["nsfw:nude"],
    "обнаженная": ["nsfw:nude"],
    "обнажённая": ["nsfw:nude"],
    "белье": ["nsfw:lingerie", "nsfw:underwear", "nsfw:panties", "nsfw:bra"],
    "бельё": ["nsfw:lingerie", "nsfw:underwear", "nsfw:panties", "nsfw:bra"],
    "трусики": ["nsfw:panties"],
    "лифчик": ["nsfw:bra"],
    "бикини": ["nsfw:bikini"],
    "купальник": ["nsfw:swimsuit", "nsfw:bikini"],
    "прозрачное": ["nsfw:see_through"],
    "сетка": ["nsfw:fishnets"],
    "латекс": ["nsfw:latex"],
    "бондаж": ["nsfw:bondage", "nsfw:shibari"],
    "шибари": ["nsfw:shibari", "nsfw:bondage"],
    "ошейник": ["nsfw:collar", "nsfw:leash"],
    "грудь": ["nsfw:breasts", "nsfw:large_breasts", "nsfw:cleavage"],
    "попа": ["nsfw:ass"],
    "ягодицы": ["nsfw:ass"],
    "бедра": ["nsfw:thighs"],
    "бёдра": ["nsfw:thighs"],
    "футджоб": ["nsfw:footjob"],
    "эротика": ["nsfw:erotic", "nsfw:explicit"],
    "18+": ["nsfw:explicit", "nsfw:erotic", "nsfw:nude"],
    "nsfw": ["nsfw:explicit", "nsfw:erotic", "nsfw:nude"],
    "член": ["nsfw:penis"],
    "пенис": ["nsfw:penis"],
    "cock": ["nsfw:penis"],
    "penis": ["nsfw:penis"],
    "фута": ["nsfw:futa"],
    "футанари": ["nsfw:futa"],
    "futa": ["nsfw:futa"],
    "фембой": ["nsfw:femboy"],
    "трап": ["nsfw:femboy"],
    "femboy": ["nsfw:femboy"],
}

# Индексация русских синонимов для полнотекстового поиска
for _eng_k, _ru_v in TAG_RU_DICT.items():
    _ru_low = _ru_v.lower().strip()
    RU_SEARCH_SYNONYMS.setdefault(_ru_low, []).extend([
        _eng_k, f"detail:{_eng_k}", f"angle:{_eng_k}", f"pose:{_eng_k}", f"nsfw:{_eng_k}"
    ])
    for _word in re.split(r"[^а-яё0-9]+", _ru_low):
        if len(_word) >= 3:
            RU_SEARCH_SYNONYMS.setdefault(_word, []).extend([
                _eng_k, f"detail:{_eng_k}", f"angle:{_eng_k}", f"pose:{_eng_k}", f"nsfw:{_eng_k}"
            ])


def detect_gpu_hardware_available() -> bool:
    if sys.platform.startswith("win"):
        return True
    try:
        import onnxruntime as ort
        provs = ort.get_available_providers()
        if "CUDAExecutionProvider" in provs or "DmlExecutionProvider" in provs:
            return True
    except Exception:
        pass
    try:
        res = subprocess.run(["nvidia-smi"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
        if res.returncode == 0:
            return True
    except Exception:
        pass
    return False

def load_hub_settings() -> Dict:
    default_cfg = {
        "external_player_path": "",
        "default_sort": "newest",
        "ai_enabled": True,
        "gpu_enabled": True,
        "ai_model": "eva02",
        "theme": "system",
        "disable_videos": False,
        "artist_mode": False,
        "nsfw_enabled": False,
        "cat_angle_enabled": True,
        "cat_pose_enabled": True,
        "cat_detail_enabled": True,
        "cat_nsfw_enabled": True,
        "cat_char_enabled": True,
        "tags_in_russian": True,
        "img_translate_enabled": True,
        "img_translate_auto": False,
        "translate_provider": "google",
        "translate_target_lang": "ru",
        "translate_api_url": "",
        "translate_api_key": "",
        "ocr_api_key": "helloworld",
        "language": "",
        "first_run_completed": False,
    }
    if SETTINGS_PATH.is_file():
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                default_cfg.update(data)
        except Exception:
            pass
    return default_cfg


def save_hub_settings(cfg: Dict) -> Dict:
    cur = load_hub_settings()
    cur.update(cfg)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    return cur

# =====================================================================
# 2. БАЗА ДАННЫХ SQLITE + ТАБЛИЦЫ ПЕРЕВОДОВ И РЕЗЕРВНЫХ КОПИЙ
# =====================================================================

def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_categories (
            key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            category TEXT DEFAULT 'general',
            label TEXT DEFAULT '',
            added_at REAL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            subfolder_name TEXT DEFAULT '',
            folder_category TEXT DEFAULT 'general',
            file_md5 TEXT DEFAULT '',
            dhash TEXT DEFAULT '',
            file_size INTEGER DEFAULT 0,
            art_type TEXT DEFAULT 'image',
            is_favorite INTEGER DEFAULT 0,
            width INTEGER,
            height INTEGER,
            aspect_cat TEXT,
            dominant_hex TEXT,
            palette_json TEXT,
            pose_ready INTEGER DEFAULT 0,
            pose_confidence REAL DEFAULT 0.0,
            flip_h INTEGER DEFAULT 0,
            grayscale INTEGER DEFAULT 0,
            feature_vec TEXT,
            clip_vec TEXT,
            notes TEXT DEFAULT '',
            mtime REAL,
            indexed_at REAL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS image_tags (
            image_id INTEGER NOT NULL,
            tag TEXT NOT NULL,
            source TEXT DEFAULT 'ai',
            confidence REAL DEFAULT 1.0,
            UNIQUE(image_id, tag),
            FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_translations (
            tag_en TEXT PRIMARY KEY,
            tag_ru TEXT NOT NULL,
            updated_at REAL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS image_translations (
            image_id INTEGER NOT NULL,
            target_lang TEXT NOT NULL,
            provider TEXT DEFAULT 'google',
            blocks_json TEXT DEFAULT '[]',
            full_text TEXT DEFAULT '',
            updated_at REAL,
            PRIMARY KEY (image_id, target_lang)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS backup_item_state (
            file_md5 TEXT,
            filename TEXT,
            orig_path TEXT,
            is_favorite INTEGER DEFAULT 0,
            folder_category TEXT DEFAULT 'general',
            flip_h INTEGER DEFAULT 0,
            grayscale INTEGER DEFAULT 0,
            notes TEXT DEFAULT '',
            tags_json TEXT DEFAULT '[]',
            PRIMARY KEY (file_md5, filename)
        )
        """
    )

    # Миграция системной категории general и очистка устаревших пресетов
    cur.execute(
        "INSERT OR IGNORE INTO custom_categories (key, label, sort_order) VALUES ('general', '🗂️ Все файлы', 0)"
    )
    cur.execute(
        "UPDATE custom_categories SET label='🗂️ Все файлы' WHERE key='general'"
    )
    for old_key in LEGACY_PRESET_CATEGORY_KEYS:
        cur.execute("UPDATE images SET folder_category='general' WHERE folder_category=?", (old_key,))
        cur.execute("UPDATE folders SET category='general' WHERE category=?", (old_key,))
        cur.execute("DELETE FROM custom_categories WHERE key=?", (old_key,))

    cur.execute("PRAGMA table_info(images)")
    i_cols = {r["name"] for r in cur.fetchall()}
    for col_name, col_def in [
        ("subfolder_name", "TEXT DEFAULT ''"),
        ("folder_category", "TEXT DEFAULT 'general'"),
        ("file_md5", "TEXT DEFAULT ''"),
        ("dhash", "TEXT DEFAULT ''"),
        ("file_size", "INTEGER DEFAULT 0"),
        ("art_type", "TEXT DEFAULT 'image'"),
        ("is_favorite", "INTEGER DEFAULT 0"),
    ]:
        if col_name not in i_cols:
            cur.execute(f"ALTER TABLE images ADD COLUMN {col_name} {col_def}")

    cur.execute("DELETE FROM image_tags WHERE tag LIKE 'light:%'")
    cur.execute("DELETE FROM image_tags WHERE source = 'ai'")
    cur.execute("DELETE FROM image_translations WHERE provider NOT LIKE '%_v37' OR blocks_json = '[]'")

    # Миграция 18+ тегов из пространства имён detail:* в nsfw:*
    cur.execute("SELECT DISTINCT tag FROM image_tags WHERE tag LIKE 'detail:%'")
    detail_tags_in_db = [r["tag"] for r in cur.fetchall()]
    for dt in detail_tags_in_db:
        short_t = dt.split(":", 1)[1]
        if is_wd_tag_nsfw(short_t):
            new_nsfw_t = WD14_TO_KOKEK_MAP.get(short_t, f"nsfw:{short_t}") if "WD14_TO_KOKEK_MAP" in globals() else PUBLIC_NSFW_TAGS.get(short_t, f"nsfw:{short_t}")
            if not new_nsfw_t.startswith("nsfw:"):
                new_nsfw_t = f"nsfw:{short_t}"
            cur.execute("UPDATE OR IGNORE image_tags SET tag=? WHERE tag=?", (new_nsfw_t, dt))
            cur.execute("DELETE FROM image_tags WHERE tag=?", (dt,))


    conn.commit()
    conn.close()


init_db()


def get_all_categories_dict() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT key, label, sort_order FROM custom_categories ORDER BY sort_order ASC, rowid ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def load_cached_tag_translations() -> Dict[str, str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT tag_en, tag_ru FROM tag_translations")
    res = {r["tag_en"]: r["tag_ru"] for r in cur.fetchall()}
    conn.close()
    return res


# =====================================================================
# 2.5 ОНЛАЙН-ПЕРЕВОДЧИК (ДЛЯ ТЕКСТА НА КАРТИНКАХ И ДОПЕРЕВОДА ТЕГОВ)
# =====================================================================

GLOBAL_NLLB_TRANSLATOR = None
GLOBAL_NLLB_SP = None
GLOBAL_NLLB_LOCK = threading.Lock()


def is_local_trans_model_installed() -> bool:
    m_bin = TRANS_MODEL_DIR / "model.bin"
    sp_m = TRANS_MODEL_DIR / "sentencepiece.bpe.model"
    return m_bin.is_file() and m_bin.stat().st_size > 100_000_000 and sp_m.is_file()


def _detect_src_nllb_lang(text: str) -> str:
    if re.search(r"[\u3040-\u30ff]", text):
        return "jpn_Jpan"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zho_Hans"
    if re.search(r"[\uac00-\ud7af]", text):
        return "kor_Hang"
    if re.search(r"[а-яА-ЯёЁ]", text):
        return "rus_Cyrl"
    return "eng_Latn"


def translate_with_local_nllb(text: str, target_lang: str = "ru") -> Optional[str]:
    global GLOBAL_NLLB_TRANSLATOR, GLOBAL_NLLB_SP
    if not is_local_trans_model_installed():
        return None
    with GLOBAL_NLLB_LOCK:
        try:
            if GLOBAL_NLLB_TRANSLATOR is None or GLOBAL_NLLB_SP is None:
                import ctranslate2
                import sentencepiece as spm
                GLOBAL_NLLB_TRANSLATOR = ctranslate2.Translator(str(TRANS_MODEL_DIR), device="cpu", compute_type="int8")
                GLOBAL_NLLB_SP = spm.SentencePieceProcessor()
                GLOBAL_NLLB_SP.Load(str(TRANS_MODEL_DIR / "sentencepiece.bpe.model"))
            src_code = _detect_src_nllb_lang(text)
            tgt_code = NLLB_LANG_MAP.get(target_lang, "rus_Cyrl")
            if src_code == tgt_code:
                return text.strip()
            tokens = GLOBAL_NLLB_SP.EncodeAsPieces(text)
            src_tokens = [src_code] + tokens + ["</s>"]
            results = GLOBAL_NLLB_TRANSLATOR.translate_batch([src_tokens], target_prefix=[[tgt_code]], beam_size=2)
            out_tokens = results[0].hypotheses[0]
            if out_tokens and out_tokens[0] == tgt_code:
                out_tokens = out_tokens[1:]
            return GLOBAL_NLLB_SP.DecodePieces(out_tokens).strip()
        except Exception:
            return None


def translate_text_online_single(text: str, target_lang: str = "ru", cfg: Optional[Dict] = None) -> str:
    """Переводит строку через выбранный онлайн API переводчик (Google / MyMemory / LibreTranslate / OpenAI)."""
    clean_txt = (text or "").strip()
    if not clean_txt:
        return ""
    if cfg is None:
        cfg = load_hub_settings()
    provider = (cfg.get("translate_provider") or "google").lower()

    if provider == "local_nllb":
        local_res = translate_with_local_nllb(clean_txt, target_lang)
        if local_res:
            return local_res
    api_url = (cfg.get("translate_api_url") or "").strip()
    api_key = (cfg.get("translate_api_key") or "").strip()

    # 1) OpenAI-совместимый API (OpenAI / DeepSeek / OpenRouter / Gemini)
    if provider == "openai" and api_key:
        endpoint = api_url or "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "system",
                    "content": f"Translate the user's text into '{target_lang}'. Output ONLY the translated text without quotes or explanations.",
                },
                {"role": "user", "content": clean_txt},
            ],
            "temperature": 0.1,
        }
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "Mozilla/5.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
        except Exception:
            pass

    # 2) LibreTranslate / DeepLX совместимый эндпоинт
    if provider == "libre":
        endpoint = api_url or "https://libretranslate.de/translate"
        payload = {"q": clean_txt, "source": "auto", "target": target_lang, "format": "text"}
        if api_key:
            payload["api_key"] = api_key
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if "translatedText" in data:
                    return str(data["translatedText"]).strip()
                if "data" in data:
                    return str(data["data"]).strip()
        except Exception:
            pass

    # 3) MyMemory Translated API (бесплатный публичный эндпоинт)
    if provider == "mymemory":
        try:
            q_enc = urllib.parse.quote(clean_txt)
            url = f"https://api.mymemory.translated.net/get?q={q_enc}&langpair=autodetect|{target_lang}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=7) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                tr = (data.get("responseData") or {}).get("translatedText")
                if tr:
                    return str(tr).strip()
        except Exception:
            pass

    # 4) Google Translate веб-API (несколько независимых эндпоинтов без лимитов)
    for g_url in (
        f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={target_lang}&dt=t&q={urllib.parse.quote(clean_txt)}",
        f"https://clients5.google.com/translate_a/single?client=dict-chrome-ex&sl=auto&tl={target_lang}&dt=t&q={urllib.parse.quote(clean_txt)}",
    ):
        try:
            req = urllib.request.Request(
                g_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            with urllib.request.urlopen(req, timeout=7) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list) and data and isinstance(data[0], list):
                    joined = "".join(seg[0] for seg in data[0] if seg and isinstance(seg, list) and seg[0])
                    if joined.strip():
                        return joined.strip()
        except Exception:
            continue

    # 5) Запасной фоллбэк на MyMemory API, если Google временно недоступен
    try:
        q_enc = urllib.parse.quote(clean_txt)
        url = f"https://api.mymemory.translated.net/get?q={q_enc}&langpair=autodetect|{target_lang}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=7) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tr = (data.get("responseData") or {}).get("translatedText")
            if tr and str(tr).strip():
                return str(tr).strip()
    except Exception:
        pass

    return clean_txt


GLOBAL_RAPID_OCR = None
GLOBAL_RAPID_OCR_LOCK = threading.Lock()
RAPID_OCR_INSTALL_TRIED = False


def _import_rapid_ocr_cls():
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        return RapidOCR
    except ImportError:
        from rapidocr import RapidOCR  # type: ignore
        return RapidOCR


def get_or_init_rapid_ocr():
    """Ленивая инициализация синглтона RapidOCR (поддерживает rapidocr и rapidocr_onnxruntime)."""
    global GLOBAL_RAPID_OCR, RAPID_OCR_INSTALL_TRIED
    with GLOBAL_RAPID_OCR_LOCK:
        if GLOBAL_RAPID_OCR is not None:
            return GLOBAL_RAPID_OCR
        try:
            cls = _import_rapid_ocr_cls()
            GLOBAL_RAPID_OCR = cls()
            return GLOBAL_RAPID_OCR
        except ImportError:
            if not RAPID_OCR_INSTALL_TRIED and not IS_FROZEN:
                RAPID_OCR_INSTALL_TRIED = True
                for pkg in ("rapidocr-onnxruntime", "rapidocr"):
                    try:
                        subprocess.call([sys.executable, "-m", "pip", "install", pkg], creationflags=WIN_NO_WINDOW)
                        cls = _import_rapid_ocr_cls()
                        GLOBAL_RAPID_OCR = cls()
                        return GLOBAL_RAPID_OCR
                    except Exception:
                        continue
        except Exception:
            pass
    return None


def extract_windows_winrt_ocr(img_bytes: bytes, work_w: int, work_h: int, preferred_lang: Optional[str] = None) -> List[Dict]:
    """Офлайн-OCR через встроенный в Windows 10/11 WinRT API (Windows.Media.Ocr) без лимитов."""
    if not sys.platform.startswith("win"):
        return []
    tmp_jpg = APP_DIR / "_tmp_ocr_frame.jpg"
    pref_lang_safe = (preferred_lang or "").replace("'", "")
    try:
        tmp_jpg.write_bytes(img_bytes)
        ps_script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{ $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' }})[0]
function Await($WinRtTask, $ResultType) {{
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('{str(tmp_jpg).replace("'", "''")}')) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = $null
$prefLang = '{pref_lang_safe}'
if ($prefLang) {{
    try {{
        $langObj = [Windows.Globalization.Language]::new($prefLang)
        if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($langObj)) {{
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($langObj)
        }}
    }} catch {{}}
}}
if ($null -eq $engine) {{ $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }}
if ($null -eq $engine) {{ $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US')) }}
$ocrResult = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$outLines = @()
foreach ($ln in $ocrResult.Lines) {{
    $x0 = 99999; $y0 = 99999; $x1 = 0; $y1 = 0
    foreach ($w in $ln.Words) {{
        $r = $w.BoundingRect
        if ($r.X -lt $x0) {{ $x0 = $r.X }}
        if ($r.Y -lt $y0) {{ $y0 = $r.Y }}
        if (($r.X + $r.Width) -gt $x1) {{ $x1 = $r.X + $r.Width }}
        if (($r.Y + $r.Height) -gt $y1) {{ $y1 = $r.Y + $r.Height }}
    }}
    $outLines += [PSCustomObject]@{{ text = $ln.Text; x = $x0; y = $y0; w = ($x1 - $x0); h = ($y1 - $y0) }}
}}
$outLines | ConvertTo-Json -Compress
"""
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=12,
            creationflags=WIN_NO_WINDOW,
        )
        if res.returncode == 0 and res.stdout.strip():
            raw_json = json.loads(res.stdout.decode("utf-8", errors="ignore"))
            if isinstance(raw_json, dict):
                raw_json = [raw_json]
            out_blocks: List[Dict] = []
            for ln in raw_json or []:
                t_s = str(ln.get("text") or "").strip()
                if not t_s:
                    continue
                x0 = max(0.0, float(ln.get("x", 0)))
                y0 = max(0.0, float(ln.get("y", 0)))
                bw = max(8.0, float(ln.get("w", 40)))
                bh = max(8.0, float(ln.get("h", 16)))
                out_blocks.append({
                    "orig": t_s,
                    "x": round((x0 / work_w) * 100.0, 2),
                    "y": round((y0 / work_h) * 100.0, 2),
                    "w": round(min(100.0, (bw / work_w) * 100.0), 2),
                    "h": round(min(100.0, (bh / work_h) * 100.0), 2),
                })
            return out_blocks
    except Exception:
        return []
    finally:
        try:
            if tmp_jpg.is_file():
                tmp_jpg.unlink()
        except Exception:
            pass
    return []



def _looks_like_garbled_cyrillic(blocks: List[Dict], avg_conf: float) -> bool:
    """Определяет, что RapidOCR (без кириллицы) прочитал русский текст как латинский мусор."""
    if not blocks:
        return False
    full = " ".join(str(b.get("orig", "")) for b in blocks)
    # Если уже есть кириллица — всё ок
    if re.search(r"[а-яА-ЯёЁ]", full):
        return False
    # Признаки кириллицы, прочитанной латинским OCR: цифры внутри слов (Bce3abIBalo), смешанный регистр (BcerAa, CKOJbKO), низкая уверенность
    if re.search(r"[A-Za-z][0-9][A-Za-z]", full):
        return True
    if re.search(r"[a-z][A-Z]", full):
        return True
    if re.search(r"[A-Z]{2,}[a-z][A-Z]{2,}", full):
        return True
    if avg_conf < 0.78 and len(full.strip()) >= 4:
        return True
    return False

def extract_ocr_blocks_from_image(img_path: Path, cfg: Dict, target_lang: str = "ru") -> List[Dict]:
    """Извлекает текстовые блоки и их координаты в % от размеров изображения (RapidOCR -> WinRT OCR -> OCR.space)."""
    with Image.open(img_path) as raw_im:
        im = ImageOps.exif_transpose(raw_im).convert("RGB")
        orig_w, orig_h = im.size
        max_dim = 1280
        if max(orig_w, orig_h) > max_dim:
            ratio = max_dim / float(max(orig_w, orig_h))
            work_w = max(1, int(orig_w * ratio))
            work_h = max(1, int(orig_h * ratio))
            im_work = im.resize((work_w, work_h), Image.Resampling.BILINEAR)
        else:
            work_w, work_h = orig_w, orig_h
            im_work = im

        # RapidOCR ожидает массив в формате BGR
        work_bgr = np.ascontiguousarray(np.asarray(im_work)[:, :, ::-1])
        buf = io.BytesIO()
        im_work.save(buf, format="JPEG", quality=82)
        jpg_bytes = buf.getvalue()

    blocks: List[Dict] = []

    # 0. Если переводим на английский (или другой не-русский язык), сначала пробуем Windows OCR с ru-RU,
    # так как стандартный RapidOCR не содержит кириллицу и выдаёт латинский мусор вместо русского текста.
    if target_lang != "ru" and sys.platform.startswith("win"):
        ru_winrt = extract_windows_winrt_ocr(jpg_bytes, work_w, work_h, preferred_lang="ru-RU")
        if ru_winrt and any(re.search(r"[а-яА-ЯёЁ]", b.get("orig", "")) for b in ru_winrt):
            return ru_winrt

    # 1. Локальный RapidOCR
    try:
        ocr_eng = get_or_init_rapid_ocr()
        if ocr_eng is not None:
            raw_out = ocr_eng(work_bgr)
            res = raw_out[0] if isinstance(raw_out, tuple) else getattr(raw_out, "boxes", None)
            # Поддержка как RapidOCR v1/v2 (tuple), так и v3 (RapidOCROutput)
            if hasattr(raw_out, "txts") and hasattr(raw_out, "boxes") and raw_out.boxes is not None:
                res = list(zip(raw_out.boxes, raw_out.txts or [], raw_out.scores or [1.0] * len(raw_out.boxes)))
            rapid_confs: List[float] = []
            if res:
                for item in res:
                    box, txt_val, conf = item[0], str(item[1]).strip(), float(item[2])
                    if len(txt_val) < 1 or conf < 0.30:
                        continue
                    rapid_confs.append(conf)
                    xs = [pt[0] for pt in box]
                    ys = [pt[1] for pt in box]
                    x0, x1 = max(0.0, min(xs)), min(float(work_w), max(xs))
                    y0, y1 = max(0.0, min(ys)), min(float(work_h), max(ys))
                    blocks.append({
                        "orig": txt_val,
                        "x": round((x0 / work_w) * 100.0, 2),
                        "y": round((y0 / work_h) * 100.0, 2),
                        "w": round(max(4.0, ((x1 - x0) / work_w) * 100.0), 2),
                        "h": round(max(2.5, ((y1 - y0) / work_h) * 100.0), 2),
                    })
                avg_c = (sum(rapid_confs) / len(rapid_confs)) if rapid_confs else 1.0
                if blocks and not _looks_like_garbled_cyrillic(blocks, avg_c):
                    return blocks
    except Exception:
        pass

    # 2. Встроенный офлайн-OCR Windows 10/11 (без лимитов)
    winrt_blocks = extract_windows_winrt_ocr(jpg_bytes, work_w, work_h)
    if winrt_blocks:
        return winrt_blocks

    # Попытка 2: Бесплатный облачный OCR.space API (работает без установки внешних программ)
    ocr_key = (cfg.get("ocr_api_key") or "helloworld").strip() or "helloworld"
    b64_img = "data:image/jpeg;base64," + base64.b64encode(jpg_bytes).decode("ascii")

    ocr_attempts = [("1", "rus"), ("2", "")] if target_lang != "ru" else [("2", ""), ("1", "")]
    for engine_num, ocr_lang in ocr_attempts:
        try:
            _ocr_params = {
                "apikey": ocr_key,
                "base64Image": b64_img,
                "isOverlayRequired": "true",
                "OCREngine": engine_num,
                "scale": "true",
                "detectOrientation": "true",
            }
            if ocr_lang:
                _ocr_params["language"] = ocr_lang
            form_data = urllib.parse.urlencode(_ocr_params).encode("utf-8")
            req = urllib.request.Request(
                "https://api.ocr.space/parse/image",
                data=form_data,
                headers={"User-Agent": "Mozilla/5.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=14) as resp:
                ocr_json = json.loads(resp.read().decode("utf-8"))
            parsed_list = ocr_json.get("ParsedResults") or []
            if parsed_list:
                overlay = parsed_list[0].get("TextOverlay") or {}
                lines = overlay.get("Lines") or []
                for ln in lines:
                    line_txt = str(ln.get("LineText") or "").strip()
                    words = ln.get("Words") or []
                    if not line_txt or not words:
                        continue
                    lefts = [float(w.get("Left", 0)) for w in words]
                    tops = [float(w.get("Top", 0)) for w in words]
                    rights = [float(w.get("Left", 0)) + float(w.get("Width", 0)) for w in words]
                    bottoms = [float(w.get("Top", 0)) + float(w.get("Height", 0)) for w in words]
                    x0, y0 = min(lefts), min(tops)
                    x1, y1 = max(rights), max(bottoms)
                    blocks.append({
                        "orig": line_txt,
                        "x": round(max(0.0, min(96.0, (x0 / work_w) * 100.0)), 2),
                        "y": round(max(0.0, min(96.0, (y0 / work_h) * 100.0)), 2),
                        "w": round(max(4.0, min(100.0, ((x1 - x0) / work_w) * 100.0)), 2),
                        "h": round(max(2.5, min(100.0, ((y1 - y0) / work_h) * 100.0)), 2),
                    })
                if not blocks:
                    raw_text = str(parsed_list[0].get("ParsedText") or "").strip()
                    if raw_text:
                        for idx, line_s in enumerate([s.strip() for s in raw_text.splitlines() if s.strip()][:12]):
                            blocks.append({
                                "orig": line_s,
                                "x": 8.0,
                                "y": round(8.0 + idx * 7.0, 2),
                                "w": 84.0,
                                "h": 6.0,
                            })
                if blocks:
                    return blocks
        except Exception:
            continue

    return blocks

# =====================================================================
# 3. СОСТОЯНИЕ ФОНОВЫХ ЗАДАЧ, БЫСТРЫЕ ХЕШИ И УСКОРЕННОЕ ЧТЕНИЕ ВИДЕО
# =====================================================================

class BackgroundJobsState:
    def __init__(self):
        self.ai_downloading: bool = False
        self.ai_progress_pct: float = 0.0
        self.ai_step_text: str = ""
        self.ai_downloaded_mb: float = 0.0
        self.ai_total_mb: float = 0.0
        self.ai_error: str = ""

        self.trans_downloading: bool = False
        self.trans_progress_pct: float = 0.0
        self.trans_step_text: str = ""
        self.trans_error: str = ""

        self.scan_running: bool = False
        self.scan_cancel_requested: bool = False
        self.scan_folder_path: str = ""
        self.scan_total: int = 0
        self.scan_current: int = 0
        self.scan_new_added: int = 0
        self.scan_skipped_existing: int = 0
        self.scan_current_file: str = ""
        self.scan_op_stage: str = ""
        self.scan_start_time: float = 0.0
        self.scan_eta_minutes: int = 0
        self.scan_eta_seconds: int = 0
        self.scan_speed_fps: float = 0.0
        self.scan_message: str = ""


JOBS = BackgroundJobsState()


def detect_brawl_stars_tags(fpath: Path, allow_nsfw: bool = True) -> Dict[str, Tuple[str, float]]:
    found: Dict[str, Tuple[str, float]] = {}
    haystack = f"{fpath.parent.name} {fpath.stem}".lower()
    tokens = set(re.split(r"[^a-z0-9\-]+", haystack))
    for ch in BRAWL_STARS_CHARS:
        if ch in tokens:
            found[f"char:{ch}"] = ("filename", 1.0)
    if allow_nsfw and ("femboy" in tokens or "otoko_no_ko" in tokens or "фембой" in haystack):
        found["nsfw:femboy"] = ("filename", 1.0)
    return found


def compute_fast_video_hash(vid_path: Path) -> Tuple[str, int]:
    """Мгновенный хеш видеофайла (< 0.1 мс): размер + 64 КБ в начале + 64 КБ в конце."""
    st = vid_path.stat()
    f_size = st.st_size
    h_md5 = hashlib.md5()
    h_md5.update(f"{f_size}:{int(st.st_mtime)}".encode("utf-8"))
    chunk_sz = 65536
    with open(vid_path, "rb") as f:
        h_md5.update(f.read(chunk_sz))
        if f_size > chunk_sz * 2:
            f.seek(max(0, f_size - chunk_sz))
            h_md5.update(f.read(chunk_sz))
    return h_md5.hexdigest(), f_size


def compute_file_hashes(img_path: Path, pil_img: Image.Image) -> Tuple[str, str, int]:
    f_size = img_path.stat().st_size
    h_md5 = hashlib.md5()
    if f_size <= 8 * 1024 * 1024:
        with open(img_path, "rb") as f:
            h_md5.update(f.read())
    else:
        chunk_sz = 262144
        h_md5.update(str(f_size).encode("utf-8"))
        with open(img_path, "rb") as f:
            h_md5.update(f.read(chunk_sz))
            f.seek(f_size // 2)
            h_md5.update(f.read(chunk_sz))
            f.seek(max(0, f_size - chunk_sz))
            h_md5.update(f.read(chunk_sz))
    file_md5 = h_md5.hexdigest()

    gray_small = pil_img.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    pixels = np.asarray(gray_small, dtype=np.int16)
    diff = pixels[:, 1:] > pixels[:, :-1]
    bits = "".join("1" if b else "0" for b in diff.flatten())
    dhash_hex = f"{int(bits, 2):016x}"
    return file_md5, dhash_hex, f_size


def create_video_placeholder_thumb(vid_path: Path, thumb_file: Path):
    w, h = 420, 260
    img = Image.new("RGB", (w, h), (20, 23, 34))
    draw = ImageDraw.Draw(img)
    draw.rectangle([8, 8, w - 9, h - 9], outline=(72, 82, 115), width=2)
    cx, cy = w // 2, h // 2 - 10
    draw.ellipse([cx - 38, cy - 38, cx + 38, cy + 38], fill=(124, 92, 255), outline=(255, 255, 255), width=2)
    draw.polygon([(cx - 12, cy - 18), (cx - 12, cy + 18), (cx + 20, cy)], fill=(255, 255, 255))
    ext_txt = vid_path.suffix.upper().lstrip(".") or "VIDEO"
    draw.text((20, 18), f"VIDEO • {ext_txt}", fill=(200, 210, 235))
    draw.text((20, h - 34), vid_path.name[:42], fill=(148, 163, 184))
    img.save(thumb_file, "JPEG", quality=85)


def pil_to_ort_tensor(img: Image.Image, inp_size: int = 448) -> np.ndarray:
    w, h = img.size
    canvas = Image.new("RGB", (inp_size, inp_size), (255, 255, 255))
    ratio = min(inp_size / max(1, w), inp_size / max(1, h))
    nw, nh = max(1, int(w * ratio)), max(1, int(h * ratio))
    resized = img.resize((nw, nh), Image.Resampling.BICUBIC)
    canvas.paste(resized, ((inp_size - nw) // 2, (inp_size - nh) // 2))
    arr_bgr = np.asarray(canvas, dtype=np.float32)[:, :, ::-1]
    return np.ascontiguousarray(arr_bgr, dtype=np.float32)


def cv2_bgr_to_ort_tensor(bgr_frame: np.ndarray, cv2_mod, inp_size: int = 448) -> np.ndarray:
    """Быстрое C++ масштабирование кадра OpenCV BGR сразу в C-contiguous (448, 448, 3) float32."""
    h, w = bgr_frame.shape[:2]
    ratio = min(inp_size / max(1, w), inp_size / max(1, h))
    nw, nh = max(1, int(w * ratio)), max(1, int(h * ratio))
    resized = cv2_mod.resize(bgr_frame, (nw, nh), interpolation=cv2_mod.INTER_LINEAR)
    canvas = np.full((inp_size, inp_size, 3), 255.0, dtype=np.float32)
    top = (inp_size - nh) // 2
    left = (inp_size - nw) // 2
    canvas[top : top + nh, left : left + nw] = resized
    return np.ascontiguousarray(canvas, dtype=np.float32)

# =====================================================================
# 4. ИИ-ДВИЖОК (САМОВОССТАНОВЛЕНИЕ ONNXRUNTIME + GPU/CPU ИНФЕРЕНС)
# =====================================================================

def _test_ort_in_subprocess() -> bool:
    """Проверяет в отдельном процессе, что onnxruntime импортируется и содержит InferenceSession (не блокируя .pyd файлы)."""
    try:
        res = subprocess.run(
            [sys.executable, "-c", "import onnxruntime as ort; assert hasattr(ort, 'InferenceSession'); print(ort.get_available_providers())"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            creationflags=WIN_NO_WINDOW,
        )
        return res.returncode == 0
    except Exception:
        return False


def get_win_safe_path(p: Path) -> str:
    """Возвращает короткий ASCII-путь 8.3 в Windows (если профиль пользователя содержит кириллицу)."""
    s = str(p.resolve())
    if sys.platform.startswith("win"):
        try:
            buf = ctypes.create_unicode_buffer(2048)
            res = ctypes.windll.kernel32.GetShortPathNameW(s, buf, 2048)
            if res > 0 and buf.value:
                return buf.value
        except Exception:
            pass
    return s


def create_ort_session_safe(ort_mod, onnx_path: Path, sess_opts, providers: List):
    """Создаёт InferenceSession с защитой от кириллических путей в Windows (через короткий путь или чтение байтов)."""
    safe_str = get_win_safe_path(onnx_path)
    try:
        return ort_mod.InferenceSession(safe_str, sess_options=sess_opts, providers=providers)
    except Exception as e1:
        # Если путь содержит не-ASCII символы и C++ загрузчик ONNX не смог открыть файл по пути, передаём bytes
        if any(ord(ch) > 127 for ch in safe_str):
            model_bytes = onnx_path.read_bytes()
            return ort_mod.InferenceSession(model_bytes, sess_options=sess_opts, providers=providers)
        raise e1


def ensure_onnxruntime_healthy(use_gpu: bool = True):
    """
    Быстро проверяет работоспособность onnxruntime.
    Если пакет уже исправен — мгновенно возвращает его без вызова pip и без подпроцессов.
    Если пакет повреждён (например, смешались файлы _pybind_state.py от разных версий),
    сначала полностью очищает оба пакета, чтобы не возникала ошибка set_default_logger_callback,
    и устанавливает рабочую версию.
    """
    try:
        import onnxruntime as ort
        if hasattr(ort, "InferenceSession") and hasattr(ort, "get_available_providers"):
            _ = ort.get_available_providers()
            return ort
    except Exception:
        pass

    for mod_k in list(sys.modules.keys()):
        if mod_k == "onnxruntime" or mod_k.startswith("onnxruntime."):
            sys.modules.pop(mod_k, None)

    if not IS_FROZEN and not _test_ort_in_subprocess():
        # Сначала полностью удаляем остатки обоих пакетов, чтобы файлы в site-packages/onnxruntime/capi не смешивались
        subprocess.call(
            [sys.executable, "-m", "pip", "uninstall", "-y", "onnxruntime", "onnxruntime-directml", "onnxruntime-gpu"],
            creationflags=WIN_NO_WINDOW,
        )
        # На Python <= 3.12 пробуем DirectML, а если не поддерживается (например Python 3.14) — чистый onnxruntime
        if use_gpu and sys.platform.startswith("win") and sys.version_info < (3, 13):
            subprocess.call(
                [sys.executable, "-m", "pip", "install", "--no-cache-dir", "onnxruntime-directml"],
                creationflags=WIN_NO_WINDOW,
            )
        if not IS_FROZEN and not _test_ort_in_subprocess():
            subprocess.call(
                [sys.executable, "-m", "pip", "uninstall", "-y", "onnxruntime-directml"],
                creationflags=WIN_NO_WINDOW,
            )
            subprocess.call(
                [sys.executable, "-m", "pip", "install", "--no-cache-dir", "onnxruntime"],
                creationflags=WIN_NO_WINDOW,
            )

    for mod_k in list(sys.modules.keys()):
        if mod_k == "onnxruntime" or mod_k.startswith("onnxruntime."):
            sys.modules.pop(mod_k, None)

    import onnxruntime as ort
    return ort


class KokekAIEngine:
    def __init__(self):
        self._lock = threading.Lock()
        self.loaded = False
        self.ort_session = None
        self.ort_input_name = None
        self.ort_tags: List[Tuple[str, int]] = []
        self.tag_to_idx: Dict[str, int] = {}
        self.model_label = ""
        self.active_gpu_provider = "CPU"
        self.last_load_error = ""
        self._repair_attempted = False

    def reload_from_settings(self):
        self.loaded = False
        self.ort_session = None
        self.try_load_models()

    def get_best_available_onnx_files(self) -> Optional[Tuple[Path, Path, str]]:
        cfg = load_hub_settings()
        choice = cfg.get("ai_model", "eva02")
        script_dir = Path(__file__).resolve().parent
        candidate_dirs = [
            MODELS_DIR,
            Path.home() / ".kokekbooru" / "ai_models",
            Path.home() / ".kokekbooru" / "ai_models",
            script_dir / "ai_models",
            script_dir,
        ]
        eva_pair = None
        vit_pair = None
        for d in candidate_dirs:
            if not d.is_dir():
                continue
            e_onnx = d / "wd_eva02_large_tagger_v3.onnx"
            if eva_pair is None and e_onnx.is_file() and e_onnx.stat().st_size > 100_000_000:
                e_csv = d / "eva02_selected_tags.csv"
                alt_csv = d / "selected_tags.csv"
                if not e_csv.is_file() and alt_csv.is_file():
                    e_csv = alt_csv
                elif not e_csv.is_file():
                    try:
                        urllib.request.urlretrieve(EVA02_CSV_URL, str(e_csv))
                    except Exception:
                        pass
                if e_csv.is_file():
                    eva_pair = (e_onnx, e_csv, "WD-EVA02-Large v3")

            v_onnx = d / "wd_vit_tagger_v3.onnx"
            if vit_pair is None and v_onnx.is_file() and v_onnx.stat().st_size > 10_000_000:
                v_csv = d / "selected_tags.csv"
                alt_v_csv = d / "eva02_selected_tags.csv"
                if not v_csv.is_file() and alt_v_csv.is_file():
                    v_csv = alt_v_csv
                elif not v_csv.is_file():
                    try:
                        urllib.request.urlretrieve(VIT_CSV_URL, str(v_csv))
                    except Exception:
                        pass
                if v_csv.is_file():
                    vit_pair = (v_onnx, v_csv, "WD-ViT v3")

        if choice == "vit" and (vit_pair or eva_pair):
            return vit_pair or eva_pair
        if eva_pair or vit_pair:
            return eva_pair or vit_pair

        # Дополнительный поиск любого .onnx файла (> 10 МБ) в папках моделей
        for d in candidate_dirs:
            if not d.is_dir():
                continue
            for onnx_f in d.glob("*.onnx"):
                try:
                    sz = onnx_f.stat().st_size
                except Exception:
                    continue
                if sz > 10_000_000:
                    csv_candidates = list(d.glob("*.csv"))
                    csv_f = csv_candidates[0] if csv_candidates else (d / "selected_tags.csv")
                    if not csv_f.is_file():
                        try:
                            url = EVA02_CSV_URL if sz > 500_000_000 else VIT_CSV_URL
                            urllib.request.urlretrieve(url, str(csv_f))
                        except Exception:
                            pass
                    if csv_f.is_file():
                        lbl = "WD-EVA02-Large v3" if sz > 500_000_000 else "WD-ViT v3"
                        return (onnx_f, csv_f, lbl)
        return None

    def are_core_models_downloaded(self) -> bool:
        return self.get_best_available_onnx_files() is not None

    def _create_cpu_fallback_session(self, onnx_path: Path):
        import onnxruntime as ort
        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_opts.intra_op_num_threads = CPU_PREFETCH_WORKERS
        self.ort_session = create_ort_session_safe(
            ort, onnx_path, sess_opts=sess_opts, providers=["CPUExecutionProvider"]
        )
        self.ort_input_name = self.ort_session.get_inputs()[0].name
        self.active_gpu_provider = "CPU"

    def try_load_models(self):
        with self._lock:
            self._try_load_models_locked()

    def _try_load_models_locked(self):
        cfg = load_hub_settings()
        if not cfg.get("ai_enabled", True):
            self.ort_session = None
            self.loaded = False
            return

        if self.loaded and self.ort_session is not None and self.ort_tags:
            return

        best_files = self.get_best_available_onnx_files()
        if best_files is None:
            self.ort_session = None
            self.loaded = False
            return

        onnx_path, csv_path, m_label = best_files
        self.model_label = m_label
        use_gpu = bool(cfg.get("gpu_enabled", True))

        try:
            if not self._repair_attempted:
                self._repair_attempted = True
                ort = ensure_onnxruntime_healthy(use_gpu=use_gpu)
            else:
                import onnxruntime as ort

            if hasattr(ort, "preload_dlls"):
                try:
                    ort.preload_dlls()
                except Exception:
                    pass

            avail = ort.get_available_providers()
            self.ort_session = None

            if use_gpu and ("DmlExecutionProvider" in avail or "CUDAExecutionProvider" in avail):
                try:
                    sess_opts = ort.SessionOptions()
                    sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                    sess_opts.intra_op_num_threads = CPU_PREFETCH_WORKERS
                    # Для стабильной работы DirectML обязательно отключаем mem_pattern и включаем последовательный режим
                    if "DmlExecutionProvider" in avail:
                        sess_opts.enable_mem_pattern = False
                        sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

                    preferred_providers = []
                    if "DmlExecutionProvider" in avail:
                        preferred_providers.append("DmlExecutionProvider")
                    if "CUDAExecutionProvider" in avail:
                        preferred_providers.append("CUDAExecutionProvider")
                    preferred_providers.append("CPUExecutionProvider")

                    self.ort_session = create_ort_session_safe(
                        ort, onnx_path, sess_opts=sess_opts, providers=preferred_providers
                    )
                except Exception as gpu_exc:
                    print(f"[Kokekbooru] Переключение с GPU на CPU из-за ошибки провайдера: {gpu_exc}")
                    self.ort_session = None

            if self.ort_session is None:
                self._create_cpu_fallback_session(onnx_path)

            self.ort_input_name = self.ort_session.get_inputs()[0].name
            active_provs = self.ort_session.get_providers()
            if "CUDAExecutionProvider" in active_provs:
                self.active_gpu_provider = "CUDA"
            elif "DmlExecutionProvider" in active_provs:
                self.active_gpu_provider = "DirectML"
            else:
                self.active_gpu_provider = "CPU"

            self.ort_tags = []
            self.tag_to_idx = {}
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader, None)
                for idx, row in enumerate(reader):
                    if len(row) >= 3:
                        t_name = row[1].strip()
                        try:
                            cat_id = int(row[2])
                        except ValueError:
                            cat_id = 0
                        self.ort_tags.append((t_name, cat_id))
                        self.tag_to_idx[t_name] = idx

            self.loaded = (self.ort_session is not None and len(self.ort_tags) > 0)
            self.last_load_error = ""
            print(f"[Kokekbooru] Загружена ИИ-модель {self.model_label} ({len(self.ort_tags)} тегов) на {self.active_gpu_provider}")
        except Exception as exc:
            self.last_load_error = str(exc)
            print(f"[Kokekbooru] Ошибка инициализации ONNX: {exc}")
            self.ort_session = None
            self.loaded = False

    def run_ort_inference_batch(self, tensors: List[np.ndarray]) -> List[np.ndarray]:
        """
        Надёжный запуск инференса для списка тензоров (448, 448, 3):
        - Гарантирует C-contiguous float32
        - Пробует батч-инференс; если модель (например EVA02) или DirectML требует batch=1, выполняет по одному
        - При сбое GPU-сессии автоматически переключается на CPU и повторяет расчёт без потери тегов
        """
        if not tensors or self.ort_session is None:
            return []

        # Для EVA02-Large в ONNX внутренние слои внимания требуют batch=1
        allow_multi_batch = ("EVA02" not in self.model_label) and (len(tensors) > 1)

        if allow_multi_batch:
            try:
                batch_arr = np.ascontiguousarray(np.stack(tensors, axis=0), dtype=np.float32)
                out = self.ort_session.run(None, {self.ort_input_name: batch_arr})[0]
                if out is not None and len(out) == len(tensors):
                    return [out[i] for i in range(len(tensors))]
            except Exception as b_exc:
                print(f"[Kokekbooru] Переход на поштучный инференс: {b_exc}")

        preds_list: List[np.ndarray] = []
        switched_to_cpu = False
        for t in tensors:
            single_inp = np.ascontiguousarray(np.expand_dims(t, axis=0), dtype=np.float32)
            try:
                pred = self.ort_session.run(None, {self.ort_input_name: single_inp})[0][0]
                preds_list.append(pred)
            except Exception as single_exc:
                print(f"[Kokekbooru] Ошибка инференса кадра ({self.active_gpu_provider}): {single_exc}")
                if not switched_to_cpu and self.active_gpu_provider != "CPU":
                    switched_to_cpu = True
                    best_files = self.get_best_available_onnx_files()
                    if best_files is not None:
                        try:
                            self._create_cpu_fallback_session(best_files[0])
                            pred = self.ort_session.run(None, {self.ort_input_name: single_inp})[0][0]
                            preds_list.append(pred)
                            continue
                        except Exception as cpu_exc:
                            print(f"[Kokekbooru] Ошибка CPU-фоллбэка: {cpu_exc}")
                preds_list.append(np.zeros(len(self.ort_tags), dtype=np.float32))
        return preds_list

    def map_predictions_to_tags(self, preds: np.ndarray, target_tags_dict: Dict[str, Tuple[str, float]]):
        """
        Публичная версия: выдаёт ВСЕ поддерживаемые ИИ-моделью теги из selected_tags.csv.
        При выключенном режиме 18+ полностью фильтрует 18+ теги.
        """
        cfg = load_hub_settings()
        nsfw_on = bool(cfg.get("nsfw_enabled", False))
        gen_thresh = 0.37 if "EVA02" in self.model_label else 0.35
        foot_thresh = 0.48 if "EVA02" in self.model_label else 0.44
        char_thresh = 0.32
        has_foot_evidence = False

        char_candidates: List[Tuple[str, float]] = []
        general_candidates: List[Tuple[str, float]] = []

        for idx, prob in enumerate(preds):
            if idx >= len(self.ort_tags):
                break
            raw_t, cat_id = self.ort_tags[idx]
            p_val = float(prob)

            if cat_id == 4 and p_val >= char_thresh:
                clean_char = re.sub(r"_\(.*?\)", "", raw_t)
                char_candidates.append((clean_char, p_val))
            elif cat_id == 9 and p_val >= 0.45:
                if nsfw_on:
                    if raw_t == "explicit":
                        target_tags_dict["nsfw:explicit"] = ("wd_ai", round(p_val, 3))
                    elif raw_t == "questionable":
                        target_tags_dict["nsfw:erotic"] = ("wd_ai", round(p_val, 3))
            elif cat_id == 0 and p_val >= gen_thresh:
                if raw_t in EVA02_FOOT_EVIDENCE_TAGS and p_val >= foot_thresh:
                    has_foot_evidence = True

                if raw_t in PUBLIC_NSFW_TAGS or is_wd_tag_nsfw(raw_t):
                    if nsfw_on and p_val >= 0.38:
                        mapped_nsfw = PUBLIC_NSFW_TAGS.get(raw_t, f"nsfw:{raw_t}")
                        target_tags_dict[mapped_nsfw] = ("wd_ai", round(p_val, 3))
                elif raw_t in PUBLIC_ANGLE_TAGS:
                    target_tags_dict[PUBLIC_ANGLE_TAGS[raw_t]] = ("wd_ai", round(p_val, 3))
                elif raw_t in PUBLIC_POSE_TAGS:
                    target_tags_dict[PUBLIC_POSE_TAGS[raw_t]] = ("wd_ai", round(p_val, 3))
                else:
                    general_candidates.append((raw_t, p_val))

        char_candidates.sort(key=lambda x: -x[1])
        for c_name, c_prob in char_candidates[:6]:
            target_tags_dict[f"char:{c_name}"] = ("wd_char", round(c_prob, 3))

        general_candidates.sort(key=lambda x: -x[1])
        for g_name, g_prob in general_candidates[:MAX_GENERAL_TAGS_PER_ITEM]:
            target_tags_dict[f"detail:{g_name}"] = ("wd_ai", round(g_prob, 3))

        if not has_foot_evidence:
            for k in list(target_tags_dict.keys()):
                if any(w in k for w in ("barefoot", "soles", "toes", "heel", "arch", "tiptoe")):
                    del target_tags_dict[k]

    def extract_visual_signature_and_palette(self, img: Image.Image) -> Tuple[str, List[str], List[float]]:
        small = img.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
        arr = np.asarray(small, dtype=np.float32) / 255.0

        quant = small.quantize(colors=5, method=Image.Quantize.MEDIANCUT).convert("RGB")
        q_colors = quant.getcolors(64 * 64) or []
        q_colors.sort(key=lambda x: x[0], reverse=True)
        hex_palette = [f"#{r:02x}{g:02x}{b:02x}" for _, (r, g, b) in q_colors[:5]]
        while len(hex_palette) < 5:
            hex_palette.append("#2a2d35")

        grid_4x4 = arr.reshape(4, 16, 4, 16, 3).mean(axis=(1, 3)).flatten()
        gray = arr.mean(axis=2)
        gx = np.abs(np.diff(gray, axis=1, append=gray[:, -1:])).reshape(4, 16, 4, 16).mean(axis=(1, 3)).flatten()
        gy = np.abs(np.diff(gray, axis=0, append=gray[-1:, :])).reshape(4, 16, 4, 16).mean(axis=(1, 3)).flatten()
        edge_16 = (gx + gy) * 0.5

        vec = np.concatenate([grid_4x4, edge_16])
        norm = np.linalg.norm(vec)
        if norm > 1e-6:
            vec = vec / norm

        return hex_palette[0], hex_palette, [round(float(x), 5) for x in vec]

    def cpu_prepare_single_image(self, img_path: Path) -> Optional[Dict]:
        cfg = load_hub_settings()
        ai_on = bool(cfg.get("ai_enabled", True))
        allow_nsfw = bool(cfg.get("nsfw_enabled", True))
        try:
            with Image.open(img_path) as raw_img:
                img = ImageOps.exif_transpose(raw_img).convert("RGB")
                width, height = img.size
                file_md5, dhash, file_size = compute_file_hashes(img_path, img)

                if width > height * 1.15:
                    aspect_cat = "landscape"
                elif height > width * 1.15:
                    aspect_cat = "portrait"
                else:
                    aspect_cat = "square"

                dom_hex, palette, feat_vec = self.extract_visual_signature_and_palette(img)

                ort_tensor = None
                if ai_on and self.ort_session is not None:
                    ort_tensor = pil_to_ort_tensor(img, 448)

                thumb_hash = hashlib.md5(str(img_path).encode("utf-8")).hexdigest()
                thumb_file = THUMBS_DIR / f"{thumb_hash}.jpg"
                if not thumb_file.is_file():
                    thumb_img = img.copy()
                    thumb_img.thumbnail((420, 420), Image.Resampling.BILINEAR)
                    thumb_img.save(thumb_file, "JPEG", quality=85)

                tags_init = detect_brawl_stars_tags(img_path, allow_nsfw=allow_nsfw)

                return {
                    "path": img_path,
                    "art_type": "image",
                    "file_md5": file_md5,
                    "dhash": dhash,
                    "file_size": file_size,
                    "width": width,
                    "height": height,
                    "aspect_cat": aspect_cat,
                    "dominant_hex": dom_hex,
                    "palette": palette,
                    "feature_vec": feat_vec,
                    "tags": tags_init,
                    "ort_tensor": ort_tensor,
                }
        except Exception as exc:
            print(f"[Kokekbooru] Пропуск поврежденного файла {img_path}: {exc}")
            return None

    def cpu_prepare_single_video_fast(self, vid_path: Path) -> Optional[Dict]:
        """
        Ускоренная параллельная подготовка видео:
        - Мгновенный 64КБ хеш (без чтения всего гигабайтного видеофайла)
        - Чтение 3 ключевых кадров (15%, 50%, 85%) с прямым C++ ресайзом OpenCV в 448x448 BGR тензор
        """
        cfg = load_hub_settings()
        ai_on = bool(cfg.get("ai_enabled", True))
        allow_nsfw = bool(cfg.get("nsfw_enabled", True))
        try:
            file_md5, file_size = compute_fast_video_hash(vid_path)
            width, height = 1920, 1080
            thumb_hash = hashlib.md5(str(vid_path).encode("utf-8")).hexdigest()
            thumb_file = THUMBS_DIR / f"{thumb_hash}.jpg"
            need_thumb = not thumb_file.is_file()
            need_tensors = ai_on and (self.ort_session is not None)

            ort_tensors: List[np.ndarray] = []
            thumb_saved = not need_thumb

            if need_thumb or need_tensors:
                try:
                    import cv2
                    cap = cv2.VideoCapture(str(vid_path))
                    if cap.isOpened():
                        w_cv = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                        h_cv = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                        if w_cv > 0 and h_cv > 0:
                            width, height = w_cv, h_cv

                        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                        if need_tensors:
                            fractions = [0.15, 0.50, 0.85][:VIDEO_SAMPLE_FRAMES]
                        else:
                            fractions = [0.25]

                        if total_frames >= len(fractions):
                            indices = sorted({min(total_frames - 1, max(0, int(total_frames * f))) for f in fractions})
                            mid_i = len(indices) // 2
                            for idx_pos, f_idx in enumerate(indices):
                                cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
                                ok, frame = cap.read()
                                if ok and frame is not None:
                                    if need_tensors:
                                        ort_tensors.append(cv2_bgr_to_ort_tensor(frame, cv2, 448))
                                    if not thumb_saved and idx_pos == mid_i:
                                        th_ratio = min(420.0 / max(1, frame.shape[1]), 420.0 / max(1, frame.shape[0]))
                                        tw = max(1, int(frame.shape[1] * th_ratio))
                                        th = max(1, int(frame.shape[0] * th_ratio))
                                        small_bgr = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
                                        cv2.imwrite(str(thumb_file), small_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                                        thumb_saved = True
                        else:
                            ok, frame = cap.read()
                            if ok and frame is not None:
                                if need_tensors:
                                    ort_tensors.append(cv2_bgr_to_ort_tensor(frame, cv2, 448))
                                if not thumb_saved:
                                    th_ratio = min(420.0 / max(1, frame.shape[1]), 420.0 / max(1, frame.shape[0]))
                                    tw = max(1, int(frame.shape[1] * th_ratio))
                                    th = max(1, int(frame.shape[0] * th_ratio))
                                    small_bgr = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
                                    cv2.imwrite(str(thumb_file), small_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                                    thumb_saved = True
                        cap.release()
                except Exception:
                    pass

            if not thumb_file.is_file():
                create_video_placeholder_thumb(vid_path, thumb_file)

            vtags = {"detail:video": ("system", 1.0)}
            vtags.update(detect_brawl_stars_tags(vid_path, allow_nsfw=allow_nsfw))

            if width > height * 1.15:
                aspect_cat = "landscape"
            elif height > width * 1.15:
                aspect_cat = "portrait"
            else:
                aspect_cat = "square"

            return {
                "path": vid_path,
                "art_type": "video",
                "file_md5": file_md5,
                "dhash": "",
                "file_size": file_size,
                "width": width,
                "height": height,
                "aspect_cat": aspect_cat,
                "dominant_hex": "#1e2230",
                "palette": ["#1e2230", "#7c5cff", "#2a2d35", "#101217", "#38bdf8"],
                "pose_ready": 0,
                "pose_confidence": 0.0,
                "feature_vec": [],
                "clip_vec": None,
                "tags": vtags,
                "ort_tensors": ort_tensors,
            }
        except Exception as exc:
            print(f"[Kokekbooru] Ошибка чтения видео {vid_path}: {exc}")
            return None

    def analyze_video_batch_gpu(self, video_paths: List[Path], stage_callback=None) -> List[Dict]:
        """Параллельно извлекает ключевые кадры из батча видео и запускает единый GPU-инференс."""
        cfg = load_hub_settings()
        ai_on = bool(cfg.get("ai_enabled", True))
        self.try_load_models()

        if stage_callback:
            stage_callback(f"Быстрое чтение видео ({len(video_paths)} шт.)...")

        with ThreadPoolExecutor(max_workers=CPU_PREFETCH_WORKERS) as pool:
            prepped_videos = [x for x in pool.map(self.cpu_prepare_single_video_fast, video_paths) if x is not None]

        if not prepped_videos:
            return []

        if ai_on and self.ort_session is not None and self.ort_tags:
            all_tensors: List[np.ndarray] = []
            slices: List[Tuple[int, int]] = []
            for item in prepped_videos:
                t_list = item.get("ort_tensors") or []
                start_i = len(all_tensors)
                all_tensors.extend(t_list)
                slices.append((start_i, len(all_tensors)))

            if all_tensors:
                if stage_callback:
                    stage_callback(f"ИИ-теги видео ({len(prepped_videos)} рол.)...")
                batch_preds = self.run_ort_inference_batch(all_tensors)
                if len(batch_preds) == len(all_tensors):
                    for item, (s_i, e_i) in zip(prepped_videos, slices):
                        if e_i > s_i:
                            max_preds = np.max(np.stack(batch_preds[s_i:e_i], axis=0), axis=0)
                            self.map_predictions_to_tags(max_preds, item["tags"])

        for item in prepped_videos:
            item.pop("ort_tensors", None)
        return prepped_videos

    def analyze_batch_gpu(self, paths_batch: List[Path], stage_callback=None) -> List[Dict]:
        cfg = load_hub_settings()
        ai_on = bool(cfg.get("ai_enabled", True))
        self.try_load_models()
        if stage_callback:
            stage_callback(f"Чтение батча ({len(paths_batch)} шт.)...")

        with ThreadPoolExecutor(max_workers=CPU_PREFETCH_WORKERS) as pool:
            prepped_list = [x for x in pool.map(self.cpu_prepare_single_image, paths_batch) if x is not None]

        if not prepped_list:
            return []

        if ai_on and self.ort_session is not None and self.ort_tags:
            if stage_callback:
                stage_callback(f"Инференс {self.model_label} ({len(prepped_list)})...")
            valid_items = [item for item in prepped_list if item["ort_tensor"] is not None]
            tensors = [item["ort_tensor"] for item in valid_items]
            if tensors:
                batch_preds = self.run_ort_inference_batch(tensors)
                for item, preds in zip(valid_items, batch_preds):
                    self.map_predictions_to_tags(preds, item["tags"])

        results = []
        for item in prepped_list:
            try:
                results.append(
                    {
                        "path": item["path"],
                        "art_type": "image",
                        "file_md5": item["file_md5"],
                        "dhash": item["dhash"],
                        "file_size": item["file_size"],
                        "width": item["width"],
                        "height": item["height"],
                        "aspect_cat": item["aspect_cat"],
                        "dominant_hex": item["dominant_hex"],
                        "palette": item["palette"],
                        "pose_ready": 0,
                        "pose_confidence": 0.0,
                        "feature_vec": item["feature_vec"],
                        "clip_vec": None,
                        "tags": item["tags"],
                    }
                )
            except Exception as exc:
                print(f"[Kokekbooru] Ошибка пост-обработки {item.get('path')}: {exc}")
                continue

        return results


AI_ENGINE = KokekAIEngine()

# =====================================================================
# 5. ФОНОВЫЙ ВОРКЕР СКАНИРОВАНИЯ (С АВТО-ВОССТАНОВЛЕНИЕМ ИЗ БЭКАПА)
# =====================================================================

def save_scanned_meta_to_db(cur, meta: Dict, root: Path, category: str, existing_paths: Dict[str, int]):
    fpath = meta["path"]
    f_str = str(fpath)
    sub_name = fpath.parent.name if fpath.parent != root else root.name
    eff_cat = category if category and category != "all" else "general"
    mtime = fpath.stat().st_mtime
    art_type = meta.get("art_type", "image")

    # Восстановление пользовательских флагов из снапшота бэкапа
    b_row = None
    if meta.get("file_md5"):
        cur.execute(
            "SELECT * FROM backup_item_state WHERE file_md5=? OR filename=? LIMIT 1",
            (meta["file_md5"], fpath.name),
        )
        b_row = cur.fetchone()

    if f_str in existing_paths:
        img_id = existing_paths[f_str]
        cur.execute(
            """
            UPDATE images SET subfolder_name=?, file_md5=?, dhash=?, file_size=?,
            art_type=?, width=?, height=?, aspect_cat=?, dominant_hex=?, palette_json=?,
            pose_ready=?, pose_confidence=?, feature_vec=?, clip_vec=?, mtime=?, indexed_at=?
            WHERE id=?
            """,
            (
                sub_name,
                meta["file_md5"],
                meta["dhash"],
                meta["file_size"],
                art_type,
                meta["width"],
                meta["height"],
                meta["aspect_cat"],
                meta["dominant_hex"],
                json.dumps(meta["palette"]),
                meta["pose_ready"],
                meta["pose_confidence"],
                json.dumps(meta["feature_vec"]),
                json.dumps(meta["clip_vec"]) if meta["clip_vec"] else None,
                mtime,
                time.time(),
                img_id,
            ),
        )
        cur.execute("DELETE FROM image_tags WHERE image_id=? AND source != 'user'", (img_id,))
    else:
        init_fav = int(b_row["is_favorite"]) if b_row else 0
        init_cat = (b_row["folder_category"] or eff_cat) if b_row else eff_cat
        init_flip = int(b_row["flip_h"]) if b_row else 0
        init_gray = int(b_row["grayscale"]) if b_row else 0
        init_notes = (b_row["notes"] or "") if b_row else ""

        cur.execute(
            """
            INSERT INTO images (
                path, filename, folder_path, subfolder_name, folder_category,
                file_md5, dhash, file_size, art_type, is_favorite, width, height, aspect_cat, dominant_hex,
                palette_json, pose_ready, pose_confidence, flip_h, grayscale, feature_vec, clip_vec, notes, mtime, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f_str,
                fpath.name,
                str(root),
                sub_name,
                init_cat,
                meta["file_md5"],
                meta["dhash"],
                meta["file_size"],
                art_type,
                init_fav,
                meta["width"],
                meta["height"],
                meta["aspect_cat"],
                meta["dominant_hex"],
                json.dumps(meta["palette"]),
                meta["pose_ready"],
                meta["pose_confidence"],
                init_flip,
                init_gray,
                json.dumps(meta["feature_vec"]),
                json.dumps(meta["clip_vec"]) if meta["clip_vec"] else None,
                init_notes,
                mtime,
                time.time(),
            ),
        )
        img_id = cur.lastrowid

        if b_row and b_row["tags_json"]:
            try:
                saved_tags = json.loads(b_row["tags_json"])
                for st in saved_tags:
                    if isinstance(st, dict) and st.get("tag"):
                        cur.execute(
                            "INSERT OR IGNORE INTO image_tags (image_id, tag, source, confidence) VALUES (?, ?, ?, ?)",
                            (img_id, st["tag"], st.get("source", "user"), float(st.get("confidence", 1.0))),
                        )
            except Exception:
                pass

    for tag_name, (src, conf) in meta["tags"].items():
        cur.execute(
            "INSERT OR IGNORE INTO image_tags (image_id, tag, source, confidence) VALUES (?, ?, ?, ?)",
            (img_id, tag_name, src, conf),
        )


def update_scan_eta(processed_new: int, total_new: int, start_ts: float):
    elapsed = max(0.05, time.time() - start_ts)
    fps = processed_new / elapsed
    JOBS.scan_speed_fps = round(fps, 1)
    remaining_new = max(0, total_new - processed_new)
    eta_sec = int(math.ceil(remaining_new / max(0.05, fps))) if remaining_new > 0 else 0
    JOBS.scan_eta_seconds = eta_sec
    JOBS.scan_eta_minutes = int(math.ceil(eta_sec / 60.0)) if eta_sec > 0 else 0


def background_scan_worker(folder_path: str, category: str, force_rescan: bool = False):
    cfg = load_hub_settings()
    disable_videos = bool(cfg.get("disable_videos", False))

    JOBS.scan_running = True
    JOBS.scan_cancel_requested = False
    JOBS.scan_folder_path = folder_path
    JOBS.scan_current = 0
    JOBS.scan_new_added = 0
    JOBS.scan_skipped_existing = 0
    JOBS.scan_start_time = time.time()
    JOBS.scan_eta_minutes = 0
    JOBS.scan_eta_seconds = 0
    JOBS.scan_speed_fps = 0.0
    JOBS.scan_op_stage = "Scanning..."
    JOBS.scan_message = ""

    try:
        root = Path(folder_path).expanduser().resolve()
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO folders (path, category, label, added_at) VALUES (?, ?, ?, ?)",
            (str(root), category, root.name, time.time()),
        )
        conn.commit()

        allowed_exts = IMAGE_EXTENSIONS if disable_videos else ALL_MEDIA_EXTENSIONS
        found_files = [
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in allowed_exts and ".kokekbooru" not in str(p)
        ]
        JOBS.scan_total = len(found_files)

        AI_ENGINE.try_load_models()
        ai_ready_now = bool(cfg.get("ai_enabled", True)) and (AI_ENGINE.ort_session is not None)

        cur.execute("SELECT path, id FROM images")
        existing_paths = {r["path"]: r["id"] for r in cur.fetchall()}

        cur.execute("SELECT DISTINCT image_id FROM image_tags WHERE source = 'wd_ai'")
        ai_tagged_ids = {r["image_id"] for r in cur.fetchall()}

        if force_rescan:
            cur.execute("DELETE FROM image_tags WHERE source != 'user'")
            conn.commit()

        image_files_to_process: List[Path] = []
        video_files_to_process: List[Path] = []

        for fpath in found_files:
            f_str = str(fpath)
            existing_id = existing_paths.get(f_str)
            # Доразметка проиндексированных файлов без ИИ-тегов
            needs_ai_backfill = (existing_id is not None) and ai_ready_now and (existing_id not in ai_tagged_ids)
            if not force_rescan and (existing_id is not None) and not needs_ai_backfill:
                JOBS.scan_skipped_existing += 1
                JOBS.scan_current += 1
            else:
                if fpath.suffix.lower() in VIDEO_EXTENSIONS:
                    video_files_to_process.append(fpath)
                else:
                    image_files_to_process.append(fpath)

        ai_start_ts = time.time()
        processed_new = 0
        total_new_to_process = len(image_files_to_process) + len(video_files_to_process)

        def update_stage(st_text):
            JOBS.scan_op_stage = st_text

        # 1) Пакетный ускоренный анализ видео (параллельное извлечение кадров + единый GPU батч)
        _dyn_img_batch, _dyn_vid_batch = get_optimal_batch_sizes(getattr(AI_ENGINE, 'active_gpu_provider', 'CPU'))
        for v_start in range(0, len(video_files_to_process), _dyn_vid_batch):
            if JOBS.scan_cancel_requested:
                break
            v_batch = video_files_to_process[v_start : v_start + _dyn_vid_batch]
            JOBS.scan_current_file = f"🎬 {v_batch[0].name} (+{len(v_batch) - 1})"
            try:
                v_metas = AI_ENGINE.analyze_video_batch_gpu(v_batch, stage_callback=update_stage)
            except Exception as v_exc:
                print(f"[Kokekbooru] Ошибка видео-батча: {v_exc}")
                processed_new += len(v_batch)
                JOBS.scan_current = min(JOBS.scan_total, JOBS.scan_skipped_existing + processed_new)
                update_scan_eta(processed_new, total_new_to_process, ai_start_ts)
                continue

            for meta in v_metas:
                if JOBS.scan_cancel_requested:
                    break
                try:
                    save_scanned_meta_to_db(cur, meta, root, category, existing_paths)
                    JOBS.scan_new_added += 1
                except Exception as db_exc:
                    print(f"[Kokekbooru] Ошибка записи видео в БД: {db_exc}")
            conn.commit()
            processed_new += len(v_batch)
            JOBS.scan_current = min(JOBS.scan_total, JOBS.scan_skipped_existing + processed_new)
            update_scan_eta(processed_new, total_new_to_process, ai_start_ts)

        # 2) Батчевая обработка изображений
        _dyn_img_batch, _ = get_optimal_batch_sizes(getattr(AI_ENGINE, 'active_gpu_provider', 'CPU'))
        for b_start in range(0, len(image_files_to_process), _dyn_img_batch):
            if JOBS.scan_cancel_requested:
                conn.commit()
                JOBS.scan_message = f"🛑 Остановлено ({JOBS.scan_new_added})"
                break

            batch_paths = image_files_to_process[b_start : b_start + _dyn_img_batch]
            JOBS.scan_current_file = f"{batch_paths[0].name} (+{len(batch_paths) - 1})"

            try:
                batch_metas = AI_ENGINE.analyze_batch_gpu(batch_paths, stage_callback=update_stage)
            except Exception as batch_exc:
                print(f"[Kokekbooru] Ошибка батча: {batch_exc}")
                processed_new += len(batch_paths)
                JOBS.scan_current = min(JOBS.scan_total, JOBS.scan_skipped_existing + processed_new)
                update_scan_eta(processed_new, total_new_to_process, ai_start_ts)
                continue

            for meta in batch_metas:
                if JOBS.scan_cancel_requested:
                    break
                try:
                    save_scanned_meta_to_db(cur, meta, root, category, existing_paths)
                    JOBS.scan_new_added += 1
                except Exception as db_exc:
                    print(f"[Kokekbooru] Ошибка записи файла в БД: {db_exc}")
                    continue

            conn.commit()
            processed_new += len(batch_paths)
            JOBS.scan_current = min(JOBS.scan_total, JOBS.scan_skipped_existing + processed_new)
            update_scan_eta(processed_new, total_new_to_process, ai_start_ts)

        conn.commit()
        conn.close()
        if not JOBS.scan_cancel_requested:
            if bool(cfg.get("ai_enabled", True)) and AI_ENGINE.ort_session is None:
                if not AI_ENGINE.are_core_models_downloaded():
                    JOBS.scan_message = f"⚠️ Файлы (+{JOBS.scan_new_added}) добавлены, но файл ИИ-модели не найден в {MODELS_DIR}"
                else:
                    err_hint = AI_ENGINE.last_load_error or "перезапустите приложение"
                    JOBS.scan_message = f"⚠️ Ошибка запуска ONNX ({err_hint})"
            else:
                JOBS.scan_message = f"✅ Размечено тегами: +{JOBS.scan_new_added} (пропущено: {JOBS.scan_skipped_existing})"
    except Exception as exc:
        JOBS.scan_message = f"Ошибка: {exc}"
    finally:
        JOBS.scan_eta_minutes = 0
        JOBS.scan_eta_seconds = 0
        JOBS.scan_running = False
        JOBS.scan_cancel_requested = False


def background_ai_download_worker(model_choice: Optional[str] = None):
    cfg = load_hub_settings()
    choice = model_choice or cfg.get("ai_model", "eva02")
    use_gpu = bool(cfg.get("gpu_enabled", True))

    JOBS.ai_downloading = True
    JOBS.ai_error = ""
    JOBS.ai_progress_pct = 5.0
    JOBS.ai_step_text = "ONNX Runtime..."
    JOBS.ai_downloaded_mb = 0.0
    JOBS.ai_total_mb = 0.0

    try:
        AI_ENGINE._repair_attempted = False
        ensure_onnxruntime_healthy(use_gpu=use_gpu)

        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", "opencv-python"],
            creationflags=WIN_NO_WINDOW,
        )
        JOBS.ai_progress_pct = 15.0

        if choice == "vit":
            csv_url, csv_path = VIT_CSV_URL, VIT_CSV_PATH
            onnx_url, onnx_path = VIT_ONNX_URL, VIT_ONNX_PATH
            model_title = "WD-ViT v3"
            min_bytes = 10_000_000
        else:
            csv_url, csv_path = EVA02_CSV_URL, EVA02_CSV_PATH
            onnx_url, onnx_path = EVA02_ONNX_URL, EVA02_ONNX_PATH
            model_title = "WD-EVA02-Large v3"
            min_bytes = 500_000_000

        if not csv_path.is_file():
            JOBS.ai_step_text = f"Словарь тегов ({model_title})..."
            urllib.request.urlretrieve(csv_url, str(csv_path))
        JOBS.ai_progress_pct = 20.0

        if not onnx_path.is_file() or onnx_path.stat().st_size < min_bytes:
            JOBS.ai_step_text = f"Скачивание {model_title}..."

            def reporthook(block_num, block_size, total_size):
                if total_size > 0:
                    downloaded = block_num * block_size
                    JOBS.ai_downloaded_mb = round(downloaded / (1024 * 1024), 1)
                    JOBS.ai_total_mb = round(total_size / (1024 * 1024), 1)
                    ratio = min(1.0, downloaded / total_size)
                    JOBS.ai_progress_pct = round(20.0 + ratio * 78.0, 1)

            urllib.request.urlretrieve(onnx_url, str(onnx_path), reporthook=reporthook)

        with open(AI_DONE_FLAG, "w", encoding="utf-8") as f:
            json.dump({"installed_at": time.time(), "model": choice}, f)

        JOBS.ai_progress_pct = 99.0
        AI_ENGINE.reload_from_settings()
        JOBS.ai_progress_pct = 100.0
        JOBS.ai_step_text = "Готово!"
    except Exception as exc:
        JOBS.ai_error = str(exc)
        JOBS.ai_step_text = f"Ошибка: {exc}"
    finally:
        JOBS.ai_downloading = False


def background_trans_model_download_worker():
    JOBS.trans_downloading = True
    JOBS.trans_error = ""
    JOBS.trans_progress_pct = 5.0
    JOBS.trans_step_text = "Установка ctranslate2 и sentencepiece..."

    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", "ctranslate2", "sentencepiece"],
            creationflags=WIN_NO_WINDOW,
        )
        TRANS_MODEL_DIR.mkdir(parents=True, exist_ok=True)
        total_files = len(NLLB_FILES)
        for idx, (fname, url) in enumerate(NLLB_FILES):
            dest = TRANS_MODEL_DIR / fname
            if not dest.is_file() or (fname == "model.bin" and dest.stat().st_size < 100_000_000):
                JOBS.trans_step_text = f"Скачивание {fname}..."

                def reporthook(b_num, b_sz, t_sz, base_i=idx):
                    if t_sz > 0:
                        frac = min(1.0, (b_num * b_sz) / t_sz)
                        JOBS.trans_progress_pct = round(10.0 + ((base_i + frac) / total_files) * 88.0, 1)

                urllib.request.urlretrieve(url, str(dest), reporthook=reporthook)

        JOBS.trans_progress_pct = 100.0
        JOBS.trans_step_text = "Готово!"
    except Exception as exc:
        JOBS.trans_error = str(exc)
        JOBS.trans_step_text = f"Ошибка: {exc}"
    finally:
        JOBS.trans_downloading = False

# =====================================================================
# 6. FASTAPI ЭНДПОИНТЫ
# =====================================================================

app = FastAPI(title="Kokekbooru Public")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartScanRequest(BaseModel):
    path: str
    category: str = "general"
    force_rescan: bool = False


class CategoryManageRequest(BaseModel):
    action: str
    key: Optional[str] = None
    label: Optional[str] = None


class TagUpdateRequest(BaseModel):
    image_id: int
    tag: str
    action: str = "add"


class BulkMoveRequest(BaseModel):
    image_ids: List[int]
    folder_category: str


class BulkTagRequest(BaseModel):
    image_ids: List[int]
    tag: str


class BulkFavoriteRequest(BaseModel):
    image_ids: List[int]
    is_favorite: bool = True


class ImageFlagsRequest(BaseModel):
    image_id: int
    flip_h: Optional[bool] = None
    grayscale: Optional[bool] = None
    is_favorite: Optional[bool] = None
    notes: Optional[str] = None
    folder_category: Optional[str] = None



class OpenExternalPlayerRequest(BaseModel):
    image_id: int


class OpenInExplorerRequest(BaseModel):
    image_id: int


class DeleteAiModelRequest(BaseModel):
    model: str


class FullResetRequest(BaseModel):
    delete_ai_models: bool = False


class SaveSettingsRequest(BaseModel):
    external_player_path: Optional[str] = None
    ai_enabled: Optional[bool] = None
    gpu_enabled: Optional[bool] = None
    ai_model: Optional[str] = None
    theme: Optional[str] = None
    disable_videos: Optional[bool] = None
    artist_mode: Optional[bool] = None
    nsfw_enabled: Optional[bool] = None
    cat_angle_enabled: Optional[bool] = None
    cat_pose_enabled: Optional[bool] = None
    cat_detail_enabled: Optional[bool] = None
    cat_nsfw_enabled: Optional[bool] = None
    cat_char_enabled: Optional[bool] = None
    tags_in_russian: Optional[bool] = None
    img_translate_enabled: Optional[bool] = None
    img_translate_auto: Optional[bool] = None
    translate_provider: Optional[str] = None
    translate_target_lang: Optional[str] = None
    translate_api_url: Optional[str] = None
    translate_api_key: Optional[str] = None
    ocr_api_key: Optional[str] = None
    language: Optional[str] = None
    first_run_completed: Optional[bool] = None


class DeleteDuplicateRequest(BaseModel):
    image_id: int
    delete_from_disk: bool = False


class AutoCleanDuplicatesRequest(BaseModel):
    delete_from_disk: bool = True


class TranslateImageRequest(BaseModel):
    image_id: int
    force_refresh: bool = False


class TranslateTagsBatchRequest(BaseModel):
    tags: List[str]


class ImportBackupRequest(BaseModel):
    backup_data: Dict


def image_quality_score(row: sqlite3.Row) -> Tuple[int, int, int]:
    pixels = int(row["width"] or 0) * int(row["height"] or 0)
    ext = Path(row["path"]).suffix.lower()
    fmt_rank = 3 if ext == ".png" else (2 if ext in (".webp", ".bmp") else 1)
    fsize = int(row["file_size"] or 0)
    if fsize == 0:
        try:
            fsize = Path(row["path"]).stat().st_size
        except Exception:
            fsize = 0
    return (pixels, fmt_rank, fsize)


def is_video_row(row: sqlite3.Row) -> bool:
    at = (row["art_type"] or "").lower() if "art_type" in row.keys() else ""
    if at == "video":
        return True
    return Path(row["path"]).suffix.lower() in VIDEO_EXTENSIONS


def serialize_image_row(
    row: sqlite3.Row,
    tags_map: Dict[int, List[str]],
    dup_group: Optional[str] = None,
    is_worse_dup: bool = False,
) -> Dict:
    img_id = row["id"]
    is_vid = is_video_row(row)
    is_fav = bool(row["is_favorite"]) if "is_favorite" in row.keys() else False
    row_tags = list(tags_map.get(img_id, []))
    if is_vid and "detail:video" not in row_tags and "video" not in row_tags:
        row_tags.insert(0, "detail:video")
    return {
        "id": img_id,
        "path": row["path"],
        "filename": row["filename"],
        "folder_path": row["folder_path"],
        "subfolder_name": row["subfolder_name"] or "",
        "folder_category": row["folder_category"] or "general",
        "file_md5": row["file_md5"] or "",
        "dhash": row["dhash"] or "",
        "file_size": row["file_size"] or 0,
        "art_type": "video" if is_vid else "image",
        "is_video": is_vid,
        "is_favorite": is_fav,
        "width": row["width"],
        "height": row["height"],
        "aspect_cat": row["aspect_cat"],
        "dominant_hex": row["dominant_hex"],
        "palette": json.loads(row["palette_json"] or "[]"),
        "flip_h": bool(row["flip_h"]),
        "grayscale": bool(row["grayscale"]),
        "notes": row["notes"] or "",
        "tags": row_tags,
        "dup_group": dup_group,
        "is_worse_dup": is_worse_dup,
    }


def token_matches_item(tok: str, itags: Set[str], short_tags: Set[str], haystack: str) -> bool:
    if not tok:
        return True
    tok_norm = tok.strip().lower()
    tok_under = tok_norm.replace(" ", "_")
    for candidate in (tok_norm, tok_under):
        if (
            candidate in itags
            or candidate in short_tags
            or f"detail:{candidate}" in itags
            or f"angle:{candidate}" in itags
            or f"pose:{candidate}" in itags
            or f"nsfw:{candidate}" in itags
            or f"char:{candidate}" in itags
        ):
            return True
        if candidate in RU_SEARCH_SYNONYMS:
            syn_set = {s.lower() for s in RU_SEARCH_SYNONYMS[candidate]}
            syn_short = {s.split(":", 1)[1] if ":" in s else s for s in syn_set}
            if (syn_set | syn_short) & (itags | short_tags):
                return True
    return (tok_norm in haystack) or (tok_under in haystack)


def parse_search_query(q_raw: str) -> Tuple[List[str], List[str]]:
    q_clean = (q_raw or "").strip().lower()
    if not q_clean:
        return [], []
    if "," in q_clean:
        raw_parts = [p.strip() for p in q_clean.split(",") if p.strip()]
    else:
        raw_parts = [p.strip() for p in q_clean.split() if p.strip()]

    pos_tokens: List[str] = []
    neg_tokens: List[str] = []
    for part in raw_parts:
        if part.startswith("!") and len(part) > 1:
            neg_tokens.append(part[1:].strip())
        elif part.startswith("-") and len(part) > 1:
            neg_tokens.append(part[1:].strip())
        else:
            pos_tokens.append(part)
    return pos_tokens, neg_tokens


@app.get("/api/state")
def api_get_state(
    q: str = "",
    include_tags: str = "",
    exclude_tags: str = "",
    aspect: str = "all",
    folder_cat: str = "all",
    subfolder: str = "all",
    media_filter: str = "all",
    favorites_only: bool = False,
    duplicates_only: bool = False,
    sort: str = "newest",
    seed: int = 0,
):
    cfg = load_hub_settings()
    disable_videos = bool(cfg.get("disable_videos", False))
    nsfw_on = bool(cfg.get("nsfw_enabled", True if EDITION == "personal" else False))
    if not AI_ENGINE.loaded and cfg.get("ai_enabled", True):
        threading.Thread(target=AI_ENGINE.try_load_models, daemon=True).start()
    custom_cats = get_all_categories_dict()
    cached_ru = load_cached_tag_translations()

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT path, category, label FROM folders ORDER BY id DESC")
    folders = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT image_id, tag FROM image_tags WHERE tag NOT LIKE 'light:%'")
    tags_map: Dict[int, List[str]] = {}
    all_tag_counts: Dict[str, int] = {}
    char_counts: Dict[str, int] = {}

    enabled_ns = {
        "angle": bool(cfg.get("cat_angle_enabled", True)),
        "pose": bool(cfg.get("cat_pose_enabled", True)),
        "detail": bool(cfg.get("cat_detail_enabled", True)),
        "nsfw": nsfw_on,
        "char": bool(cfg.get("cat_char_enabled", True)),
    }

    for r in cur.fetchall():
        tg = r["tag"]
        ns_part = tg.split(":", 1)[0] if ":" in tg else "detail"
        if is_wd_tag_nsfw(tg):
            ns_part = "nsfw"
            if not tg.startswith("nsfw:"):
                tg = f"nsfw:{tg.split(':', 1)[-1]}"
        if tg != "detail:video" and not enabled_ns.get(ns_part, True):
            continue
        tags_map.setdefault(r["image_id"], []).append(tg)

    cur.execute("SELECT * FROM images ORDER BY id DESC")
    all_rows = cur.fetchall()
    conn.close()

    cat_counts: Dict[str, int] = {c["key"]: 0 for c in custom_cats}
    subfolder_counts: Dict[str, int] = {}
    rows_by_id: Dict[int, sqlite3.Row] = {}
    md5_groups: Dict[str, List[int]] = {}
    dhash_groups: Dict[str, List[int]] = {}
    favorites_count = 0

    visible_rows = []
    for r in all_rows:
        is_vid = is_video_row(r)
        if disable_videos and is_vid:
            continue
        visible_rows.append(r)

        iid = r["id"]
        rows_by_id[iid] = r
        fc = r["folder_category"] or "general"
        cat_counts[fc] = cat_counts.get(fc, 0) + 1
        sf = r["subfolder_name"] or ""
        if sf:
            subfolder_counts[sf] = subfolder_counts.get(sf, 0) + 1
        if "is_favorite" in r.keys() and r["is_favorite"]:
            favorites_count += 1
        if is_vid and "detail:video" not in tags_map.get(iid, []):
            tags_map.setdefault(iid, []).append("detail:video")

        for t in tags_map.get(iid, []):
            all_tag_counts[t] = all_tag_counts.get(t, 0) + 1
            if t.startswith("char:"):
                c_name = t.split(":", 1)[1]
                char_counts[c_name] = char_counts.get(c_name, 0) + 1

        if r["file_md5"]:
            md5_groups.setdefault(r["file_md5"], []).append(iid)
        if r["dhash"]:
            dhash_groups.setdefault(r["dhash"], []).append(iid)

    # Корневая категория отражает общее число элементов
    cat_counts["general"] = len(visible_rows)

    dup_id_to_group: Dict[int, str] = {}
    worse_dup_ids: Set[int] = set()

    def process_dup_clusters(groups_dict: Dict[str, List[int]], prefix: str):
        for h_val, ids in groups_dict.items():
            if len(ids) > 1:
                sorted_ids = sorted(ids, key=lambda x: image_quality_score(rows_by_id[x]), reverse=True)
                best_id = sorted_ids[0]
                dup_id_to_group.setdefault(best_id, f"👑 {prefix} ({h_val[:6]})")
                for w_id in sorted_ids[1:]:
                    dup_id_to_group[w_id] = f"🗑 {prefix} ({h_val[:6]})"
                    worse_dup_ids.add(w_id)

    process_dup_clusters(md5_groups, "MD5")
    process_dup_clusters(dhash_groups, "dHash")

    inc_set = {t.strip().lower() for t in include_tags.split(",") if t.strip()}
    exc_set = {t.strip().lower() for t in exclude_tags.split(",") if t.strip()}
    pos_tokens, neg_tokens = parse_search_query(q)

    # Формирование карты локализации активных тегов
    tag_ru_map: Dict[str, str] = {}
    all_known_tags = set(all_tag_counts.keys()) | set(PRESET_NSFW_TAGS if nsfw_on else [])
    for tg in all_known_tags:
        ru_lbl = translate_single_tag_ru(tg, cached_ru)
        tag_ru_map[tg] = ru_lbl
        short_k = tg.split(":", 1)[1] if ":" in tg else tg
        tag_ru_map[short_k] = ru_lbl

    results = []
    for r in visible_rows:
        iid = r["id"]
        itags = {t.lower() for t in tags_map.get(iid, [])}
        short_tags = {t.split(":", 1)[1] if ":" in t else t for t in itags}
        ru_tags_str = " ".join(tag_ru_map.get(t, "") for t in tags_map.get(iid, [])).lower()
        is_fav = bool(r["is_favorite"]) if "is_favorite" in r.keys() else False
        is_vid = is_video_row(r)

        if media_filter == "images" and is_vid:
            continue
        if media_filter == "videos" and not is_vid:
            continue
        if favorites_only and not is_fav:
            continue
        if duplicates_only and iid not in dup_id_to_group:
            continue
        if inc_set and not inc_set.issubset(itags):
            continue
        if exc_set and (exc_set & itags):
            continue
        if aspect != "all" and r["aspect_cat"] != aspect:
            continue
        if folder_cat not in ("all", "general") and (r["folder_category"] or "general") != folder_cat:
            continue
        if subfolder != "all" and (r["subfolder_name"] or "") != subfolder:
            continue

        if pos_tokens or neg_tokens:
            haystack = f"{r['filename']} {r['subfolder_name'] or ''} {r['notes'] or ''} {' '.join(itags)} {' '.join(short_tags)} {ru_tags_str}".lower()
            if any(token_matches_item(nt, itags, short_tags, haystack) for nt in neg_tokens):
                continue
            if not all(token_matches_item(pt, itags, short_tags, haystack) for pt in pos_tokens):
                continue

        results.append(
            serialize_image_row(
                r,
                tags_map,
                dup_id_to_group.get(iid),
                is_worse_dup=(iid in worse_dup_ids),
            )
        )

    if duplicates_only:
        results.sort(key=lambda x: (x["dhash"] or x["file_md5"], x["is_worse_dup"], x["id"]))
    else:
        if sort == "oldest":
            results.sort(key=lambda x: x["id"])
        elif sort == "name_asc":
            results.sort(key=lambda x: (x["filename"] or "").lower())
        elif sort == "name_desc":
            results.sort(key=lambda x: (x["filename"] or "").lower(), reverse=True)
        elif sort == "res_desc":
            results.sort(key=lambda x: (x["width"] or 0) * (x["height"] or 0), reverse=True)
        elif sort == "res_asc":
            results.sort(key=lambda x: (x["width"] or 0) * (x["height"] or 0))
        elif sort == "size_desc":
            results.sort(key=lambda x: x["file_size"] or 0, reverse=True)
        elif sort == "random":
            rng = random.Random(seed if seed else None)
            rng.shuffle(results)

    sorted_tags = sorted(all_tag_counts.items(), key=lambda x: (-x[1], x[0]))
    sorted_chars = sorted(char_counts.items(), key=lambda x: (-x[1], x[0]))
    sorted_subfolders = sorted(subfolder_counts.items(), key=lambda x: (-x[1], x[0]))

    return {
        "version": HUB_VERSION,
        "edition": EDITION,
        "models_installed": AI_ENGINE.are_core_models_downloaded(),
        "gpu_hardware_available": detect_gpu_hardware_available(),
        "ai_vocab_size": len(AI_ENGINE.ort_tags),
        "settings": cfg,
        "folders": folders,
        "folder_categories": [
            {"key": c["key"], "label": c["label"], "count": cat_counts.get(c["key"], 0)}
            for c in custom_cats
        ],
        "subfolders": [{"name": k, "count": v} for k, v in sorted_subfolders],
        "characters": [{"name": k, "count": v} for k, v in sorted_chars],
        "favorites_count": favorites_count,
        "duplicates_count": len(dup_id_to_group),
        "worse_duplicates_count": len(worse_dup_ids),
        "total_indexed": len(visible_rows),
        "tags": [{"tag": k, "count": v} for k, v in sorted_tags],
        "tag_ru_map": tag_ru_map,
        "preset_nsfw_tags": PRESET_NSFW_TAGS if enabled_ns.get("nsfw", False) else [],
        "brawl_stars_chars": sorted(BRAWL_STARS_CHARS),
        "images": results,
    }


@app.post("/api/translate_tags_batch")
def api_translate_tags_batch(req: TranslateTagsBatchRequest):
    """Автоматически допереводит редкие английские теги на русский через онлайн-переводчик и кэширует в SQLite."""
    cfg = load_hub_settings()
    cached_ru = load_cached_tag_translations()
    conn = get_db()
    cur = conn.cursor()
    out_map: Dict[str, str] = {}

    for raw_t in (req.tags or [])[:45]:
        short = raw_t.split(":", 1)[1] if ":" in raw_t else raw_t
        short_low = short.lower().strip()
        if not short_low:
            continue
        if short_low in cached_ru:
            out_map[raw_t] = cached_ru[short_low]
            out_map[short_low] = cached_ru[short_low]
            continue
        local_tr = translate_single_tag_ru(raw_t, cached_ru)
        # Если локальный перевод всё ещё содержит латиницу (редкий тег), переводим через онлайн-API
        if re.search(r"[a-zA-Z]{3,}", local_tr):
            phrase = re.sub(r"_\(.*?\)", "", short_low).replace("_", " ")
            tr_online = translate_text_online_single(phrase, "ru", cfg)
            if tr_online and not re.fullmatch(r"[a-zA-Z0-9\s\-_]+", tr_online):
                tr_clean = tr_online[0].upper() + tr_online[1:]
                cur.execute(
                    "INSERT OR REPLACE INTO tag_translations (tag_en, tag_ru, updated_at) VALUES (?, ?, ?)",
                    (short_low, tr_clean, time.time()),
                )
                cached_ru[short_low] = tr_clean
                local_tr = tr_clean
        out_map[raw_t] = local_tr
        out_map[short_low] = local_tr

    conn.commit()
    conn.close()
    return {"status": "ok", "translations": out_map}


@app.post("/api/translate_image_text")
def api_translate_image_text(req: TranslateImageRequest):
    """Распознаёт текст на картинке (OCR) и переводит его через выбранный онлайн-переводчик."""
    cfg = load_hub_settings()
    if not cfg.get("img_translate_enabled", True):
        raise HTTPException(status_code=400, detail="Перевод текста на картинках отключён в настройках")

    target_lang = (cfg.get("translate_target_lang") or "ru").strip() or "ru"
    if (cfg.get("language") or "ru") == "en" and target_lang == "ru":
        target_lang = "en"
    provider = (cfg.get("translate_provider") or "google").strip() or "google"

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT path, art_type FROM images WHERE id=?", (req.image_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Image not found")

    if not req.force_refresh:
        cur.execute(
            "SELECT blocks_json, full_text, provider FROM image_translations WHERE image_id=? AND target_lang=?",
            (req.image_id, target_lang),
        )
        cached = cur.fetchone()
        if cached and cached["blocks_json"]:
            try:
                parsed_cached = json.loads(cached["blocks_json"])
            except Exception:
                parsed_cached = []
            if parsed_cached:
                conn.close()
                return {
                    "status": "ok",
                    "cached": True,
                    "target_lang": target_lang,
                    "provider": cached["provider"],
                    "blocks": parsed_cached,
                    "full_text": cached["full_text"] or "",
                }

    img_path = Path(row["path"])
    if not img_path.is_file():
        conn.close()
        raise HTTPException(status_code=404, detail="File not found on disk")

    ocr_blocks = extract_ocr_blocks_from_image(img_path, cfg)
    translated_blocks: List[Dict] = []
    full_lines: List[str] = []

    if ocr_blocks:
        # Сортируем распознанные строки в естественном порядке чтения (сверху вниз, слева направо)
        ocr_blocks.sort(key=lambda b: (round(b["y"] / 6.0), b["x"]))

        # Сортируем строки сверху вниз, слева направо и объединяем строки внутри одного баббла/абзаца для точного контекстного перевода
        ocr_blocks.sort(key=lambda b: (round(b["y"] / 4.0), b["x"]))
        bubbles: List[Dict] = []
        for b in ocr_blocks:
            clean_line = re.sub(r"\s+", " ", b["orig"]).strip()
            if not clean_line:
                continue
            merged = False
            for prev in bubbles:
                prev_bottom = prev["y"] + prev["h"]
                vert_gap = b["y"] - prev_bottom
                horiz_overlap = min(prev["x"] + prev["w"], b["x"] + b["w"]) - max(prev["x"], b["x"])
                prev_cx = prev["x"] + prev["w"] * 0.5
                cur_cx = b["x"] + b["w"] * 0.5
                # Строки одного баббла находятся близко по вертикали и выровнены по горизонтали
                if -2.0 <= vert_gap <= max(4.5, b["h"] * 1.15) and horiz_overlap >= -4.0 and abs(prev_cx - cur_cx) <= 22.0:
                    if prev["orig"].endswith("-"):
                        prev["orig"] = prev["orig"][:-1] + clean_line
                    else:
                        prev["orig"] = prev["orig"] + " " + clean_line
                    nx0 = min(prev["x"], b["x"])
                    ny0 = min(prev["y"], b["y"])
                    nx1 = max(prev["x"] + prev["w"], b["x"] + b["w"])
                    ny1 = max(prev["y"] + prev["h"], b["y"] + b["h"])
                    prev["x"] = round(nx0, 2)
                    prev["y"] = round(ny0, 2)
                    prev["w"] = round(nx1 - nx0, 2)
                    prev["h"] = round(ny1 - ny0, 2)
                    prev["line_count"] = prev.get("line_count", 1) + 1
                    merged = True
                    break
            if not merged:
                bubbles.append(dict(b, orig=clean_line, line_count=1))

        # Определяем цвет фона каждого баббла по углам области, чтобы плашка гармонично перекрывала оригинал
        bg_samples = []
        try:
            with Image.open(img_path) as raw_im:
                rgb_im = ImageOps.exif_transpose(raw_im).convert("RGB")
                iw, ih = rgb_im.size
                for bub in bubbles:
                    sx = min(iw - 1, max(0, int((bub["x"] / 100.0) * iw)))
                    sy = min(ih - 1, max(0, int((bub["y"] / 100.0) * ih)))
                    r_c, g_c, b_c = rgb_im.getpixel((sx, sy))
                    lum = 0.299 * r_c + 0.587 * g_c + 0.114 * b_c
                    if lum >= 150:
                        bg_samples.append((f"rgb({r_c},{g_c},{b_c})", "#111827"))
                    else:
                        bg_samples.append((f"rgb({r_c},{g_c},{b_c})", "#f8fafc"))
        except Exception:
            bg_samples = [("#ffffff", "#111827") for _ in bubbles]

        for idx, bub in enumerate(bubbles):
            tr_txt = translate_text_online_single(bub["orig"], target_lang, cfg)
            bg_col, fg_col = bg_samples[idx] if idx < len(bg_samples) else ("#ffffff", "#111827")
            translated_blocks.append({
                "orig": bub["orig"],
                "trans": tr_txt,
                "x": bub["x"],
                "y": bub["y"],
                "w": max(4.0, bub["w"]),
                "h": max(2.5, bub["h"]),
                "lines": bub.get("line_count", 1),
                "bg": bg_col,
                "fg": fg_col,
            })
            full_lines.append(f"{bub['orig']} → {tr_txt}")

    full_text_str = "\n".join(full_lines)
    if translated_blocks:
        cur.execute(
            """
            INSERT OR REPLACE INTO image_translations (image_id, target_lang, provider, blocks_json, full_text, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (req.image_id, target_lang, f"{provider}_v37", json.dumps(translated_blocks, ensure_ascii=False), full_text_str, time.time()),
        )
        conn.commit()
    conn.close()

    return {
        "status": "ok",
        "cached": False,
        "target_lang": target_lang,
        "provider": provider,
        "blocks": translated_blocks,
        "full_text": full_text_str,
    }


@app.get("/api/export_backup")
def api_export_backup():
    """Экспортирует все настройки, избранное, пользовательские папки, заметки и теги в JSON-файл."""
    cfg = load_hub_settings()
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT key, label, sort_order FROM custom_categories ORDER BY sort_order ASC")
    categories = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT path, category, label, added_at FROM folders")
    folders = [dict(r) for r in cur.fetchall()]


    cur.execute("SELECT tag_en, tag_ru FROM tag_translations")
    tag_trans = {r["tag_en"]: r["tag_ru"] for r in cur.fetchall()}

    cur.execute("SELECT image_id, tag, source, confidence FROM image_tags")
    tags_by_img: Dict[int, List[Dict]] = {}
    for r in cur.fetchall():
        tags_by_img.setdefault(r["image_id"], []).append({
            "tag": r["tag"],
            "source": r["source"],
            "confidence": r["confidence"],
        })

    cur.execute("SELECT * FROM images")
    items_backup = []
    for r in cur.fetchall():
        iid = r["id"]
        items_backup.append({
            "path": r["path"],
            "filename": r["filename"],
            "file_md5": r["file_md5"] or "",
            "dhash": r["dhash"] or "",
            "is_favorite": int(r["is_favorite"] or 0),
            "folder_category": r["folder_category"] or "general",
            "flip_h": int(r["flip_h"] or 0),
            "grayscale": int(r["grayscale"] or 0),
            "notes": r["notes"] or "",
            "tags": tags_by_img.get(iid, []),
        })
    conn.close()

    backup_payload = {
        "hub_backup_version": "3.0",
        "edition": EDITION,
        "exported_at": time.time(),
        "settings": cfg,
        "custom_categories": categories,
        "folders": folders,
        "tag_translations": tag_trans,
        "items": items_backup,
    }
    return JSONResponse(content=backup_payload)


@app.post("/api/import_backup")
def api_import_backup(req: ImportBackupRequest):
    """Загружает настройки, избранное, пользовательские папки, заметки и теги из JSON-файла резервной копии."""
    data = req.backup_data or {}
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Некорректный формат файла резервной копии")

    if isinstance(data.get("settings"), dict):
        save_hub_settings(data["settings"])
        AI_ENGINE.reload_from_settings()

    conn = get_db()
    cur = conn.cursor()

    if isinstance(data.get("custom_categories"), list):
        for c in data["custom_categories"]:
            if isinstance(c, dict) and c.get("key") and c.get("label"):
                if c["key"] in LEGACY_PRESET_CATEGORY_KEYS:
                    continue
                lbl = "🗂️ Все файлы" if c["key"] == "general" else c["label"]
                cur.execute(
                    "INSERT OR REPLACE INTO custom_categories (key, label, sort_order) VALUES (?, ?, ?)",
                    (c["key"], lbl, int(c.get("sort_order", 0))),
                )

    if isinstance(data.get("folders"), list):
        for f in data["folders"]:
            if isinstance(f, dict) and f.get("path"):
                cur.execute(
                    "INSERT OR IGNORE INTO folders (path, category, label, added_at) VALUES (?, ?, ?, ?)",
                    (f["path"], f.get("category", "general"), f.get("label", ""), float(f.get("added_at") or time.time())),
                )

    if isinstance(data.get("tag_translations"), dict):
        for k_en, v_ru in data["tag_translations"].items():
            if k_en and v_ru:
                cur.execute(
                    "INSERT OR REPLACE INTO tag_translations (tag_en, tag_ru, updated_at) VALUES (?, ?, ?)",
                    (str(k_en), str(v_ru), time.time()),
                )

    restored_items = 0
    restored_favs = 0
    cur.execute("SELECT id, path, filename, file_md5 FROM images")
    db_rows = cur.fetchall()
    by_path = {r["path"]: r["id"] for r in db_rows}
    by_md5 = {r["file_md5"]: r["id"] for r in db_rows if r["file_md5"]}
    by_name = {r["filename"]: r["id"] for r in db_rows}

    if isinstance(data.get("items"), list):
        for it in data["items"]:
            if not isinstance(it, dict):
                continue
            p_str = str(it.get("path") or "")
            f_name = str(it.get("filename") or "")
            f_md5 = str(it.get("file_md5") or "")
            is_fav = int(bool(it.get("is_favorite", 0)))
            f_cat = str(it.get("folder_category") or "general")
            if f_cat in LEGACY_PRESET_CATEGORY_KEYS:
                f_cat = "general"
            flip_h = int(bool(it.get("flip_h", 0)))
            gray = int(bool(it.get("grayscale", 0)))
            notes_txt = str(it.get("notes") or "")
            it_tags = it.get("tags") if isinstance(it.get("tags"), list) else []

            if is_fav:
                restored_favs += 1

            # Сохраняем в backup_item_state для автоматического применения при будущих сканах
            if f_md5 or f_name:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO backup_item_state
                    (file_md5, filename, orig_path, is_favorite, folder_category, flip_h, grayscale, notes, tags_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (f_md5, f_name, p_str, is_fav, f_cat, flip_h, gray, notes_txt, json.dumps(it_tags, ensure_ascii=False)),
                )

            target_id = by_path.get(p_str) or (by_md5.get(f_md5) if f_md5 else None) or by_name.get(f_name)
            if target_id is not None:
                cur.execute(
                    """
                    UPDATE images SET is_favorite=?, folder_category=?, flip_h=?, grayscale=?, notes=?
                    WHERE id=?
                    """,
                    (is_fav, f_cat, flip_h, gray, notes_txt, target_id),
                )
                for tg_obj in it_tags:
                    if isinstance(tg_obj, dict) and tg_obj.get("tag"):
                        cur.execute(
                            "INSERT OR REPLACE INTO image_tags (image_id, tag, source, confidence) VALUES (?, ?, ?, ?)",
                            (target_id, tg_obj["tag"], tg_obj.get("source", "user"), float(tg_obj.get("confidence", 1.0))),
                        )
                restored_items += 1

    conn.commit()
    conn.close()
    return {
        "status": "ok",
        "restored_items": restored_items,
        "restored_favorites": restored_favs,
    }


@app.post("/api/shutdown")
def api_shutdown():
    threading.Timer(0.25, lambda: os._exit(0)).start()
    return {"status": "shutting_down"}


@app.get("/api/settings")
def api_get_settings():
    cfg = load_hub_settings()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM images")
    total_items = cur.fetchone()["cnt"]
    cur.execute("SELECT COUNT(*) as cnt FROM folders")
    total_folders = cur.fetchone()["cnt"]
    conn.close()

    thumbs_size_bytes = sum(f.stat().st_size for f in THUMBS_DIR.glob("*") if f.is_file())
    db_size_bytes = DB_PATH.stat().st_size if DB_PATH.is_file() else 0

    return {
        **cfg,
        "version": HUB_VERSION,
        "edition": EDITION,
        "app_dir": str(APP_DIR),
        "total_items": total_items,
        "total_folders": total_folders,
        "cache_mb": round((thumbs_size_bytes + db_size_bytes) / (1024 * 1024), 2),
        "eva02_installed": any(
            (d / "wd_eva02_large_tagger_v3.onnx").is_file() and (d / "wd_eva02_large_tagger_v3.onnx").stat().st_size > 100_000_000
            for d in (MODELS_DIR, Path.home() / ".kokekbooru" / "ai_models", Path.home() / ".kokekbooru" / "ai_models")
        ),
        "local_trans_installed": is_local_trans_model_installed(),
        "vit_installed": any(
            (d / "wd_vit_tagger_v3.onnx").is_file() and (d / "wd_vit_tagger_v3.onnx").stat().st_size > 10_000_000
            for d in (MODELS_DIR, Path.home() / ".kokekbooru" / "ai_models", Path.home() / ".kokekbooru" / "ai_models")
        ),
    }


@app.post("/api/save_settings")
def api_save_settings(req: SaveSettingsRequest):
    updates = {}
    if req.external_player_path is not None:
        updates["external_player_path"] = req.external_player_path.strip()
    if req.ai_enabled is not None:
        updates["ai_enabled"] = bool(req.ai_enabled)
    if req.gpu_enabled is not None:
        updates["gpu_enabled"] = bool(req.gpu_enabled)
    if req.ai_model is not None and req.ai_model in ("eva02", "vit"):
        updates["ai_model"] = req.ai_model
    if req.theme is not None and req.theme in ("system", "dark", "light"):
        updates["theme"] = req.theme
    if req.disable_videos is not None:
        updates["disable_videos"] = bool(req.disable_videos)
    if req.artist_mode is not None:
        updates["artist_mode"] = bool(req.artist_mode)
    if req.nsfw_enabled is not None:
        updates["nsfw_enabled"] = bool(req.nsfw_enabled)
    if req.cat_angle_enabled is not None:
        updates["cat_angle_enabled"] = bool(req.cat_angle_enabled)
    if req.cat_pose_enabled is not None:
        updates["cat_pose_enabled"] = bool(req.cat_pose_enabled)
    if req.cat_detail_enabled is not None:
        updates["cat_detail_enabled"] = bool(req.cat_detail_enabled)
    if req.cat_nsfw_enabled is not None:
        updates["cat_nsfw_enabled"] = bool(req.cat_nsfw_enabled)
    if req.cat_char_enabled is not None:
        updates["cat_char_enabled"] = bool(req.cat_char_enabled)
    if req.tags_in_russian is not None:
        updates["tags_in_russian"] = bool(req.tags_in_russian)
    if req.img_translate_enabled is not None:
        updates["img_translate_enabled"] = bool(req.img_translate_enabled)
    if req.img_translate_auto is not None:
        updates["img_translate_auto"] = bool(req.img_translate_auto)
    if req.translate_provider is not None:
        updates["translate_provider"] = req.translate_provider.strip()
    if req.translate_target_lang is not None:
        updates["translate_target_lang"] = req.translate_target_lang.strip()
    if req.translate_api_url is not None:
        updates["translate_api_url"] = req.translate_api_url.strip()
    if req.translate_api_key is not None:
        updates["translate_api_key"] = req.translate_api_key.strip()
    if req.ocr_api_key is not None:
        updates["ocr_api_key"] = req.ocr_api_key.strip() or "helloworld"
    if req.language is not None and req.language in ("", "ru", "en"):
        updates["language"] = req.language
    if req.first_run_completed is not None:
        updates["first_run_completed"] = bool(req.first_run_completed)

    cfg = save_hub_settings(updates)
    if any(k in updates for k in ("ai_enabled", "gpu_enabled", "ai_model")):
        AI_ENGINE.reload_from_settings()
    return {"status": "ok", "settings": cfg}


@app.post("/api/delete_ai_model")
def api_delete_ai_model(req: DeleteAiModelRequest):
    if req.model == "vit":
        targets = (VIT_ONNX_PATH, VIT_CSV_PATH)
    else:
        targets = (EVA02_ONNX_PATH, EVA02_CSV_PATH)

    removed = 0
    for f in targets:
        try:
            if f.is_file():
                f.unlink()
                removed += 1
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    if AI_DONE_FLAG.is_file():
        try:
            AI_DONE_FLAG.unlink()
        except Exception:
            pass

    AI_ENGINE.reload_from_settings()
    return {"status": "ok", "removed": removed}


@app.post("/api/full_reset")
def api_full_reset(req: Optional[FullResetRequest] = None):
    if JOBS.scan_running:
        JOBS.scan_cancel_requested = True
        time.sleep(0.35)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM image_tags")
    cur.execute("DELETE FROM images")
    cur.execute("DELETE FROM folders")
    cur.execute("DELETE FROM custom_categories")
    cur.execute("DELETE FROM image_translations")
    cur.execute("DELETE FROM backup_item_state")

    for k, lbl, order_i in DEFAULT_FOLDER_CATEGORIES:
        cur.execute(
            "INSERT OR IGNORE INTO custom_categories (key, label, sort_order) VALUES (?, ?, ?)",
            (k, lbl, order_i),
        )
    conn.commit()
    try:
        cur.execute("VACUUM")
    except Exception:
        pass
    conn.close()

    for cache_dir in (THUMBS_DIR, FLIPPED_DIR):
        for f in cache_dir.glob("*"):
            try:
                if f.is_file():
                    f.unlink()
            except Exception:
                pass

    try:
        if SETTINGS_PATH.is_file():
            SETTINGS_PATH.unlink()
    except Exception:
        pass

    deleted_models = 0
    if req and req.delete_ai_models:
        for mf in (EVA02_ONNX_PATH, EVA02_CSV_PATH, VIT_ONNX_PATH, VIT_CSV_PATH, AI_DONE_FLAG):
            try:
                if mf.is_file():
                    mf.unlink()
                    deleted_models += 1
            except Exception:
                pass

    AI_ENGINE.reload_from_settings()
    return {"status": "ok", "deleted_models": deleted_models}


@app.post("/api/open_external_player")
def api_open_external_player(req: OpenExternalPlayerRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT path FROM images WHERE id=?", (req.image_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    fpath = Path(row["path"])
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {fpath}")

    cfg = load_hub_settings()
    custom_player = (cfg.get("external_player_path") or "").strip()

    try:
        if custom_player and Path(custom_player).is_file():
            subprocess.Popen([custom_player, str(fpath)], creationflags=WIN_NO_WINDOW)
        elif sys.platform.startswith("win"):
            os.startfile(str(fpath))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(fpath)])
        else:
            subprocess.Popen(["xdg-open", str(fpath)])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"status": "ok"}


@app.post("/api/open_in_explorer")
def api_open_in_explorer(req: OpenInExplorerRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT path FROM images WHERE id=?", (req.image_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    fpath = Path(row["path"])
    norm_path = os.path.normpath(str(fpath))
    try:
        if sys.platform.startswith("win"):
            if fpath.is_file():
                subprocess.Popen(f'explorer /select,"{norm_path}"')
            elif fpath.parent.is_dir():
                subprocess.Popen(f'explorer "{os.path.normpath(str(fpath.parent))}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", norm_path])
        else:
            subprocess.Popen(["xdg-open", str(fpath.parent if fpath.is_file() else fpath)])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"status": "ok"}


@app.post("/api/bulk_move_category")
def api_bulk_move_category(req: BulkMoveRequest):
    if not req.image_ids:
        return {"status": "ok", "updated": 0}
    conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in req.image_ids)
    cur.execute(
        f"UPDATE images SET folder_category=? WHERE id IN ({placeholders})",
        [req.folder_category, *req.image_ids],
    )
    updated = cur.rowcount
    conn.commit()
    conn.close()
    return {"status": "ok", "updated": updated}


@app.post("/api/bulk_favorite")
def api_bulk_favorite(req: BulkFavoriteRequest):
    if not req.image_ids:
        return {"status": "ok", "updated": 0}
    conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in req.image_ids)
    cur.execute(
        f"UPDATE images SET is_favorite=? WHERE id IN ({placeholders})",
        [1 if req.is_favorite else 0, *req.image_ids],
    )
    updated = cur.rowcount
    conn.commit()
    conn.close()
    return {"status": "ok", "updated": updated}


@app.post("/api/bulk_add_tag")
def api_bulk_add_tag(req: BulkTagRequest):
    if not req.image_ids:
        return {"status": "ok", "updated": 0}
    tag_clean = req.tag.strip().lower().replace(" ", "_")
    if not tag_clean:
        raise HTTPException(status_code=400, detail="Empty tag")
    if ":" not in tag_clean:
        tag_clean = f"detail:{tag_clean}"

    conn = get_db()
    cur = conn.cursor()
    for iid in req.image_ids:
        cur.execute(
            "INSERT OR REPLACE INTO image_tags (image_id, tag, source, confidence) VALUES (?, ?, 'user', 1.0)",
            (iid, tag_clean),
        )
    conn.commit()
    conn.close()
    return {"status": "ok", "tag": tag_clean, "updated": len(req.image_ids)}


@app.post("/api/manage_category")
def api_manage_category(req: CategoryManageRequest):
    conn = get_db()
    cur = conn.cursor()

    if req.action == "create":
        lbl = (req.label or "").strip()
        if not lbl:
            conn.close()
            raise HTTPException(status_code=400, detail="Empty name")
        slug = re.sub(r"[^a-zA-Z0-9_]", "_", lbl.lower()).strip("_")
        if not slug or slug == "_":
            slug = f"cat_{int(time.time())}"
        else:
            slug = f"{slug}_{int(time.time()) % 10000}"
        if not any(ord(ch) > 127000 for ch in lbl[:2]):
            lbl = f"📁 {lbl}"
        cur.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 as next_ord FROM custom_categories")
        next_ord = cur.fetchone()["next_ord"]
        cur.execute("INSERT INTO custom_categories (key, label, sort_order) VALUES (?, ?, ?)", (slug, lbl, next_ord))
        conn.commit()
        conn.close()
        return {"status": "ok", "key": slug, "label": lbl}

    elif req.action == "rename":
        if not req.key or not (req.label or "").strip():
            conn.close()
            raise HTTPException(status_code=400, detail="Invalid args")
        cur.execute("UPDATE custom_categories SET label=? WHERE key=?", (req.label.strip(), req.key))
        conn.commit()
        conn.close()
        return {"status": "ok"}

    elif req.action == "delete":
        if not req.key or req.key == "general":
            conn.close()
            raise HTTPException(status_code=400, detail="Cannot delete general")
        cur.execute("UPDATE images SET folder_category='general' WHERE folder_category=?", (req.key,))
        cur.execute("UPDATE folders SET category='general' WHERE category=?", (req.key,))
        cur.execute("DELETE FROM custom_categories WHERE key=?", (req.key,))
        conn.commit()
        conn.close()
        return {"status": "ok"}

    conn.close()
    raise HTTPException(status_code=400, detail="Unknown action")


@app.post("/api/auto_clean_duplicates")
def api_auto_clean_duplicates(req: AutoCleanDuplicatesRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM images")
    all_rows = cur.fetchall()

    rows_by_id = {r["id"]: r for r in all_rows}
    md5_groups: Dict[str, List[int]] = {}
    dhash_groups: Dict[str, List[int]] = {}

    for r in all_rows:
        iid = r["id"]
        if r["file_md5"]:
            md5_groups.setdefault(r["file_md5"], []).append(iid)
        if r["dhash"]:
            dhash_groups.setdefault(r["dhash"], []).append(iid)

    to_delete_map: Dict[int, int] = {}

    for groups_dict in (md5_groups, dhash_groups):
        for _, ids in groups_dict.items():
            active_ids = [i for i in ids if i not in to_delete_map]
            if len(active_ids) > 1:
                sorted_ids = sorted(active_ids, key=lambda x: image_quality_score(rows_by_id[x]), reverse=True)
                winner_id = sorted_ids[0]
                for loser_id in sorted_ids[1:]:
                    to_delete_map[loser_id] = winner_id

    deleted_count = 0
    freed_bytes = 0

    for loser_id, winner_id in to_delete_map.items():
        loser_row = rows_by_id[loser_id]
        cur.execute(
            """
            INSERT OR IGNORE INTO image_tags (image_id, tag, source, confidence)
            SELECT ?, tag, source, confidence FROM image_tags WHERE image_id = ?
            """,
            (winner_id, loser_id),
        )
        if loser_row["notes"]:
            cur.execute(
                "UPDATE images SET notes = CASE WHEN notes = '' OR notes IS NULL THEN ? ELSE notes END WHERE id = ?",
                (loser_row["notes"], winner_id),
            )

        cur.execute("DELETE FROM image_tags WHERE image_id = ?", (loser_id,))
        cur.execute("DELETE FROM images WHERE id = ?", (loser_id,))

        if req.delete_from_disk:
            fpath = Path(loser_row["path"])
            if fpath.is_file():
                try:
                    fsize = fpath.stat().st_size
                    fpath.unlink()
                    freed_bytes += fsize
                except Exception:
                    pass
        deleted_count += 1

    conn.commit()
    conn.close()
    freed_mb = round(freed_bytes / (1024 * 1024), 1)
    return {"status": "ok", "deleted": deleted_count, "freed_mb": freed_mb}


@app.post("/api/dismiss_ai_banner")
def api_dismiss_ai_banner():
    with open(AI_BANNER_DISMISSED_FLAG, "w", encoding="utf-8") as f:
        json.dump({"dismissed_at": time.time()}, f)
    return {"status": "ok"}


@app.get("/api/pick_folder")
def api_pick_folder():
    selected_dir = ""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected_dir = filedialog.askdirectory(title="Select folder")
        root.destroy()
    except Exception:
        if sys.platform.startswith("win"):
            ps_cmd = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "if ($f.ShowDialog() -eq 'OK') { Write-Output $f.SelectedPath }"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                text=True,
                creationflags=WIN_NO_WINDOW,
            )
            selected_dir = out.strip()
    return {"path": selected_dir}


@app.post("/api/start_scan")
def api_start_scan(req: StartScanRequest):
    if JOBS.scan_running:
        return {"status": "already_running"}
    root = Path(req.path.strip()).expanduser()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Not found: {req.path}")

    t = threading.Thread(target=background_scan_worker, args=(str(root), req.category, req.force_rescan), daemon=True)
    t.start()
    return {"status": "started"}


@app.post("/api/cancel_scan")
def api_cancel_scan():
    if JOBS.scan_running:
        JOBS.scan_cancel_requested = True
        JOBS.scan_op_stage = "Stopping..."
        return {"status": "cancelling"}
    return {"status": "not_running"}


@app.get("/api/scan_progress")
def api_scan_progress():
    pct = round((JOBS.scan_current / JOBS.scan_total) * 100.0, 1) if JOBS.scan_total > 0 else 0.0
    return {
        "running": JOBS.scan_running,
        "cancelling": JOBS.scan_cancel_requested,
        "folder": JOBS.scan_folder_path,
        "total": JOBS.scan_total,
        "current": JOBS.scan_current,
        "percent": pct,
        "new_added": JOBS.scan_new_added,
        "skipped_existing": JOBS.scan_skipped_existing,
        "current_file": JOBS.scan_current_file,
        "stage": JOBS.scan_op_stage,
        "eta_minutes": JOBS.scan_eta_minutes,
        "eta_seconds": JOBS.scan_eta_seconds,
        "speed_fps": JOBS.scan_speed_fps,
        "message": JOBS.scan_message,
    }


@app.post("/api/start_ai_download")
def api_start_ai_download(model: Optional[str] = None):
    if JOBS.ai_downloading:
        return {"status": "already_downloading"}
    t = threading.Thread(target=background_ai_download_worker, args=(model,), daemon=True)
    t.start()
    return {"status": "started"}


@app.post("/api/start_trans_model_download")
def api_start_trans_model_download():
    if JOBS.trans_downloading:
        return {"status": "already_downloading"}
    threading.Thread(target=background_trans_model_download_worker, daemon=True).start()
    return {"status": "started"}


@app.get("/api/trans_model_progress")
def api_trans_model_progress():
    return {
        "downloading": JOBS.trans_downloading,
        "percent": JOBS.trans_progress_pct,
        "step": JOBS.trans_step_text,
        "error": JOBS.trans_error,
        "installed": is_local_trans_model_installed(),
    }


@app.post("/api/delete_trans_model")
def api_delete_trans_model():
    global GLOBAL_NLLB_TRANSLATOR, GLOBAL_NLLB_SP
    with GLOBAL_NLLB_LOCK:
        GLOBAL_NLLB_TRANSLATOR = None
        GLOBAL_NLLB_SP = None
    if TRANS_MODEL_DIR.is_dir():
        for f in TRANS_MODEL_DIR.glob("*"):
            try:
                if f.is_file():
                    f.unlink()
            except Exception:
                pass
    return {"status": "ok"}


@app.get("/api/ai_download_progress")
def api_ai_download_progress():
    return {
        "downloading": JOBS.ai_downloading,
        "percent": JOBS.ai_progress_pct,
        "step": JOBS.ai_step_text,
        "downloaded_mb": JOBS.ai_downloaded_mb,
        "total_mb": JOBS.ai_total_mb,
        "error": JOBS.ai_error,
        "models_installed": AI_ENGINE.are_core_models_downloaded(),
    }


class BulkDeleteFilesRequest(BaseModel):
    image_ids: List[int]
    delete_from_disk: bool = True


@app.post("/api/bulk_delete_files")
def api_bulk_delete_files(req: BulkDeleteFilesRequest):
    if not req.image_ids:
        return {"status": "ok", "deleted": 0}
    conn = get_db()
    cur = conn.cursor()
    deleted_cnt = 0
    for iid in req.image_ids:
        cur.execute("SELECT path FROM images WHERE id=?", (iid,))
        row = cur.fetchone()
        if not row:
            continue
        fpath = Path(row["path"])
        cur.execute("DELETE FROM image_tags WHERE image_id=?", (iid,))
        cur.execute("DELETE FROM image_translations WHERE image_id=?", (iid,))
        cur.execute("DELETE FROM images WHERE id=?", (iid,))
        if req.delete_from_disk and fpath.is_file():
            try:
                fpath.unlink()
            except Exception:
                pass
        deleted_cnt += 1
    conn.commit()
    conn.close()
    return {"status": "ok", "deleted": deleted_cnt}


@app.post("/api/delete_duplicate")
def api_delete_duplicate(req: DeleteDuplicateRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT path FROM images WHERE id=?", (req.image_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Not found")

    fpath = Path(row["path"])
    cur.execute("DELETE FROM image_tags WHERE image_id=?", (req.image_id,))
    cur.execute("DELETE FROM images WHERE id=?", (req.image_id,))
    conn.commit()
    conn.close()

    if req.delete_from_disk and fpath.is_file():
        try:
            fpath.unlink()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    return {"status": "ok"}


@app.post("/api/update_tag")
def api_update_tag(req: TagUpdateRequest):
    tag_clean = req.tag.strip().lower().replace(" ", "_")
    if not tag_clean:
        raise HTTPException(status_code=400, detail="Empty tag")
    if ":" not in tag_clean:
        tag_clean = f"detail:{tag_clean}"

    conn = get_db()
    cur = conn.cursor()
    if req.action == "remove":
        cur.execute("DELETE FROM image_tags WHERE image_id=? AND tag=?", (req.image_id, tag_clean))
    else:
        cur.execute(
            "INSERT OR REPLACE INTO image_tags (image_id, tag, source, confidence) VALUES (?, ?, 'user', 1.0)",
            (req.image_id, tag_clean),
        )
    conn.commit()
    conn.close()
    return {"status": "ok", "tag": tag_clean}


@app.post("/api/update_flags")
def api_update_flags(req: ImageFlagsRequest):
    conn = get_db()
    cur = conn.cursor()
    if req.flip_h is not None:
        cur.execute("UPDATE images SET flip_h=? WHERE id=?", (int(req.flip_h), req.image_id))
    if req.grayscale is not None:
        cur.execute("UPDATE images SET grayscale=? WHERE id=?", (int(req.grayscale), req.image_id))
    if req.is_favorite is not None:
        cur.execute("UPDATE images SET is_favorite=? WHERE id=?", (int(req.is_favorite), req.image_id))
    if req.notes is not None:
        cur.execute("UPDATE images SET notes=? WHERE id=?", (req.notes, req.image_id))
    if req.folder_category is not None:
        cur.execute("UPDATE images SET folder_category=? WHERE id=?", (req.folder_category, req.image_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/inspire")
def api_inspire(media_filter: str = "all"):
    cfg = load_hub_settings()
    disable_videos = bool(cfg.get("disable_videos", False))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT image_id, tag FROM image_tags")
    tags_map: Dict[int, List[str]] = {}
    for r in cur.fetchall():
        tags_map.setdefault(r["image_id"], []).append(r["tag"])

    cur.execute("SELECT * FROM images")
    all_rows = cur.fetchall()
    conn.close()

    rows = []
    for r in all_rows:
        is_vid = is_video_row(r)
        if disable_videos and is_vid:
            continue
        if media_filter == "images" and is_vid:
            continue
        if media_filter == "videos" and not is_vid:
            continue
        rows.append(serialize_image_row(r, tags_map))

    if not rows:
        return {"cards": []}

    c1 = random.choice(rows)
    pool2 = [x for x in rows if x["id"] != c1["id"]] or rows
    c2 = random.choice(pool2)
    pool3 = [x for x in rows if x["id"] not in (c1["id"], c2["id"])] or pool2
    c3 = random.choice(pool3)

    return {
        "cards": [
            {"role": "1", "item": c1},
            {"role": "2", "item": c2},
            {"role": "3", "item": c3},
        ]
    }


@app.get("/api/thumb/{image_id}")
def api_get_thumb(image_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT path, art_type FROM images WHERE id=?", (image_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404)
    p = Path(row["path"])
    thumb_hash = hashlib.md5(str(p).encode("utf-8")).hexdigest()
    thumb_file = THUMBS_DIR / f"{thumb_hash}.jpg"
    if not thumb_file.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
        create_video_placeholder_thumb(p, thumb_file)
    if thumb_file.is_file():
        return FileResponse(str(thumb_file), media_type="image/jpeg")
    if p.is_file():
        return FileResponse(str(p))
    raise HTTPException(status_code=404)


@app.get("/api/full/{image_id}")
def api_get_full(image_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT path, art_type FROM images WHERE id=?", (image_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404)
    p = Path(row["path"])
    if is_video_row(row):
        thumb_hash = hashlib.md5(str(p).encode("utf-8")).hexdigest()
        thumb_file = THUMBS_DIR / f"{thumb_hash}.jpg"
        if thumb_file.is_file():
            return FileResponse(str(thumb_file), media_type="image/jpeg")
    if p.is_file():
        return FileResponse(str(p))
    raise HTTPException(status_code=404)

# =====================================================================
# 7. WEB UI (beta 0.1)
# =====================================================================

HTML_UI = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kokekbooru</title>
<style>
  :root {
    --bg-main: #101217;
    --bg-panel: #181b23;
    --bg-card: #212531;
    --bg-hover: #2c3142;
    --border: #2e3446;
    --text: #eceff4;
    --muted: #94a3b8;
    --accent: #7c5cff;
    --accent-hover: #957aff;
    --heart: #ef4444;
    --tag-angle: #3b82f6;
    --tag-pose: #ec4899;
    --tag-detail: #10b981;
    --tag-nsfw: #ef4444;
    --tag-char: #f59e0b;
    --danger: #ef4444;
    --success: #10b981;
    --canvas-bg: #08090d;
  }
  [data-theme="light"] {
    --bg-main: #f1f5f9;
    --bg-panel: #ffffff;
    --bg-card: #f8fafc;
    --bg-hover: #e2e8f0;
    --border: #cbd5e1;
    --text: #0f172a;
    --muted: #64748b;
    --accent: #6366f1;
    --accent-hover: #4f46e5;
    --canvas-bg: #e2e8f0;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg-main);
    color: var(--text);
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  #aiPromptBanner {
    background: linear-gradient(90deg, #312e81, #4c1d95);
    border-bottom: 1px solid #6366f1;
    padding: 8px 16px;
    display: none;
    align-items: center;
    justify-content: space-between;
    font-size: 13px;
    color: #fff;
  }
  header {
    background: var(--bg-panel);
    border-bottom: 1px solid var(--border);
    padding: 7px 10px;
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: nowrap;
    white-space: nowrap;
    position: relative;
    z-index: 60;
  }
  header button, header select {
    padding: 5px 8px;
    font-size: 11.5px;
    white-space: nowrap;
    flex-shrink: 0;
  }
  input[type="text"], input[type="password"], textarea, select {
    background: var(--bg-main);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 5px 8px;
    border-radius: 7px;
    font-size: 12px;
    outline: none;
  }
  input[type="text"]:focus, input[type="password"]:focus, textarea:focus, select:focus { border-color: var(--accent); }

  .search-wrap {
    position: relative;
    display: inline-flex;
    align-items: center;
    flex: 0 1 185px;
    min-width: 110px;
  }
  .search-wrap input[type="text"] {
    width: 100%;
    padding-right: 46px;
  }
  .search-faq-btn {
    position: absolute;
    right: 5px;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--muted);
    font-size: 11px;
    font-weight: 700;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    line-height: 1;
  }
  .search-faq-btn:hover {
    color: #fff;
    background: var(--accent);
    border-color: var(--accent);
  }
  /* Выпадающий список автодополнения тегов */
  .search-autocomplete-dropdown {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    min-width: 280px;
    max-width: 380px;
    max-height: 340px;
    overflow-y: auto;
    background: var(--bg-panel);
    border: 1.5px solid var(--accent);
    border-radius: 7px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.72);
    display: none;
    flex-direction: column;
    z-index: 999;
    padding: 3px 0;
  }
  .search-autocomplete-dropdown.open {
    display: flex;
  }
  .ac-item {
    padding: 5px 11px;
    font-size: 12.5px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    cursor: pointer;
    border-bottom: 1px solid rgba(148, 163, 184, 0.10);
    user-select: none;
  }
  .ac-item:last-child { border-bottom: none; }
  .ac-item:hover, .ac-item.active {
    background: var(--bg-hover);
  }
  .ac-hl {
    background: #facc15;
    color: #000000;
    font-weight: 700;
    padding: 0 2px;
    border-radius: 2px;
  }
  .ac-label-ns-angle { color: #60a5fa; font-weight: 600; }
  .ac-label-ns-pose { color: #f472b6; font-weight: 600; }
  .ac-label-ns-detail { color: #34d399; font-weight: 600; }
  .ac-label-ns-nsfw { color: #f87171; font-weight: 600; }
  .ac-label-ns-char { color: #fbbf24; font-weight: 600; }
  .ac-tag-left {
    display: flex;
    align-items: center;
    gap: 6px;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .ac-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .ac-dot.ns-angle { background: var(--tag-angle); }
  .ac-dot.ns-pose { background: var(--tag-pose); }
  .ac-dot.ns-detail { background: var(--tag-detail); }
  .ac-dot.ns-nsfw { background: var(--tag-nsfw); }
  .ac-dot.ns-char { background: var(--tag-char); }
  .ac-sub {
    color: var(--muted);
    font-size: 10.5px;
  }
  .ac-cnt {
    color: var(--muted);
    font-size: 11px;
    font-weight: 600;
    flex-shrink: 0;
  }
  .trans-icon-badge {
    font-weight: 800;
    font-size: 12px;
    letter-spacing: -0.5px;
    font-family: "Segoe UI", Arial, sans-serif;
  }
  .notes-list-box {
    display: flex;
    flex-direction: column;
    gap: 5px;
    max-height: 185px;
    overflow-y: auto;
    padding-right: 2px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 8px;
  }
  .note-card-item {
    padding: 6px 9px;
    border-radius: 7px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
    transition: 0.15s;
  }
  .note-card-item:hover { background: var(--bg-hover); }
  .note-card-item.active {
    border-color: var(--accent);
    background: rgba(124, 92, 255, 0.16);
  }
  .note-card-info {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    flex: 1;
  }
  .note-card-title {
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .note-card-preview {
    font-size: 10.5px;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .settings-layout {
    display: flex;
    flex-direction: row-reverse;
    flex: 1;
    min-height: 0;
    gap: 14px;
    overflow: hidden;
  }
  .settings-content-area {
    flex: 1;
    overflow-y: auto;
    padding-right: 4px;
    display: flex;
    flex-direction: column;
    gap: 11px;
  }
  .set-tabs-bar {
    width: 178px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    gap: 5px;
    border-right: 1px solid var(--border);
    padding-right: 12px;
    overflow-y: auto;
  }
  .set-tab-btn {
    width: 100%;
    padding: 8px 10px;
    font-size: 12px;
    border-radius: 7px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--muted);
    cursor: pointer;
    font-weight: 600;
    justify-content: flex-start;
    text-align: left;
  }
  .set-tab-btn:hover { background: var(--bg-hover); color: var(--text); }
  .set-tab-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .set-tab-btn.nsfw-tab { color: #ef4444; border-color: rgba(239, 68, 68, 0.45); }
  .set-tab-btn.nsfw-tab.active { background: #dc2626; border-color: #ef4444; color: #fff; }
  .set-tab-pane {
    display: none;
    flex-direction: column;
    gap: 11px;
  }
  .set-tab-pane.active { display: flex; }
  .set-tab-desc {
    background: rgba(124, 92, 255, 0.10);
    border-left: 3px solid var(--accent);
    padding: 8px 11px;
    border-radius: 6px;
    font-size: 12px;
    color: var(--muted);
    line-height: 1.45;
  }
  .set-tab-desc.nsfw-desc {
    background: rgba(239, 68, 68, 0.10);
    border-left-color: #ef4444;
    color: #ef4444;
  }
  .faq-box {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 12px;
    line-height: 1.5;
  }
  .faq-box summary {
    font-weight: 700;
    font-size: 13px;
    cursor: pointer;
    user-select: none;
    color: var(--accent);
  }
  .faq-content {
    margin-top: 8px;
    display: flex;
    flex-direction: column;
    gap: 7px;
    color: var(--text);
  }
  .faq-code {
    background: var(--bg-main);
    padding: 1px 6px;
    border-radius: 4px;
    border: 1px solid var(--border);
    font-family: monospace;
    font-size: 11.5px;
  }
  .search-clear-btn {
    position: absolute;
    right: 26px;
    background: transparent;
    border: none;
    color: var(--muted);
    font-size: 13px;
    padding: 2px 5px;
    cursor: pointer;
    display: none;
    border-radius: 4px;
    line-height: 1;
  }
  .search-clear-btn:hover {
    color: var(--text);
    background: var(--bg-hover);
  }

  button {
    background: var(--bg-card);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 6px 11px;
    border-radius: 7px;
    font-size: 12px;
    cursor: pointer;
    transition: 0.15s;
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  button:hover { background: var(--bg-hover); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
  button.primary:hover { background: var(--accent-hover); }
  button.danger-btn { background: #991b1b; border-color: #ef4444; color: #fff; font-weight: 600; }
  button.danger-btn:hover { background: #b91c1c; }
  button.active-toggle { background: #2563eb; border-color: #60a5fa; color: #fff; font-weight: 600; }
  button.fav-toggle { background: rgba(239, 68, 68, 0.2); border-color: #ef4444; color: #ef4444; font-weight: 600; }

  .media-segmented {
    display: inline-flex;
    background: var(--bg-main);
    border: 1px solid var(--border);
    border-radius: 7px;
    padding: 2px;
    gap: 2px;
  }
  .media-segmented button {
    border: none;
    background: transparent;
    padding: 4px 9px;
    font-size: 11.5px;
    border-radius: 5px;
    color: var(--muted);
  }
  .media-segmented button.active {
    background: var(--accent);
    color: #fff;
    font-weight: 600;
  }

  .header-right {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .artist-only { display: none !important; }
  .artist-only-block { display: none !important; }
  body.artist-mode .artist-only { display: inline-flex !important; }
  body.artist-mode .artist-only-block { display: flex !important; }

  .progress-drawer {
    background: var(--bg-panel);
    border-bottom: 1px solid var(--accent);
    padding: 10px 16px;
    display: none;
    flex-direction: column;
    gap: 6px;
  }
  .progress-drawer.active { display: flex; }
  .prog-track {
    width: 100%;
    height: 10px;
    background: var(--bg-main);
    border-radius: 5px;
    overflow: hidden;
    border: 1px solid var(--border);
  }
  .prog-fill {
    height: 100%;
    background: linear-gradient(90deg, #7c5cff, #ec4899);
    width: 0%;
    transition: width 0.2s;
  }
  .prog-meta {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    color: var(--muted);
  }

  #bulkActionBar {
    background: var(--bg-panel);
    color: var(--text);
    border-bottom: 2px solid var(--accent);
    padding: 8px 16px;
    display: none;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    z-index: 20;
  }
  #bulkActionBar.active { display: flex; }
  .bulk-count-pill {
    background: var(--accent);
    color: #fff;
    font-weight: 700;
    font-size: 12px;
    padding: 4px 10px;
    border-radius: 6px;
  }
  .bulk-group {
    display: flex;
    align-items: center;
    gap: 6px;
    background: var(--bg-card);
    padding: 4px 8px;
    border-radius: 7px;
    border: 1px solid var(--border);
  }

  .workspace {
    display: flex;
    flex: 1;
    overflow: hidden;
  }
  aside.left-sidebar {
    width: 305px;
    background: var(--bg-panel);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    padding: 12px;
    gap: 12px;
  }
  .side-section {
    border-bottom: 1px solid var(--border);
    padding-bottom: 10px;
  }
  .side-section:last-child { border-bottom: none; }
  .side-section h4 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: var(--muted);
    margin-bottom: 7px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    cursor: pointer;
    user-select: none;
  }
  .side-section.collapsed h4 { margin-bottom: 0; }
  .side-section.collapsed .side-section-body { display: none; }
  .collapse-btn {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 1px 6px;
    font-size: 11px;
    border-radius: 4px;
    line-height: 1.3;
  }
  .collapse-btn:hover { color: var(--text); background: var(--bg-hover); }

  .folder-cat-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .folder-cat-item {
    padding: 5px 8px;
    border-radius: 6px;
    font-size: 12.5px;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--bg-card);
    border: 1px solid transparent;
    gap: 6px;
  }
  .folder-cat-item:hover { background: var(--bg-hover); }
  .folder-cat-item.active { background: var(--accent); color: #fff; font-weight: 600; }
  .folder-cat-item.drag-over { border: 1px dashed #38bdf8; background: rgba(56, 189, 248, 0.18); }
  .cat-actions { display: none; gap: 3px; }
  .folder-cat-item:hover .cat-actions { display: inline-flex; }
  .cat-actions span {
    font-size: 11px;
    padding: 1px 4px;
    border-radius: 4px;
    background: rgba(0,0,0,0.28);
  }

  .tag-cloud {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }
  .tag-pill {
    font-size: 11.5px;
    padding: 3.5px 8px;
    border-radius: 6px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    cursor: pointer;
    user-select: none;
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  .tag-pill.ns-angle { border-left: 3px solid var(--tag-angle); }
  .tag-pill.ns-pose { border-left: 3px solid var(--tag-pose); }
  .tag-pill.ns-detail { border-left: 3px solid var(--tag-detail); }
  .tag-pill.ns-nsfw { border-left: 3px solid var(--tag-nsfw); }
  .tag-pill.ns-char { border-left: 3px solid var(--tag-char); }
  .tag-pill.included { background: var(--accent); border-color: #fff; color: #fff; font-weight: 600; }
  .tag-pill.excluded { background: #7f1d1d; border-color: var(--danger); color: #ef4444; text-decoration: line-through; }
  .tag-pill span.cnt { opacity: 0.65; font-size: 10px; }

  main {
    flex: 1;
    overflow-y: auto;
    padding: 14px;
  }
  .status-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
    font-size: 12px;
    color: var(--muted);
    gap: 10px;
    flex-wrap: wrap;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
    gap: 13px;
  }
  .empty-state {
    grid-column: 1 / -1;
    text-align: center;
    padding: 60px 20px;
    color: var(--muted);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
  }
  .empty-state .chicken-icon { font-size: 48px; }
  .empty-state h3 { font-size: 18px; color: var(--text); }

  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: transform 0.15s, border-color 0.15s, box-shadow 0.15s;
    position: relative;
  }
  .card:hover { border-color: var(--accent); transform: translateY(-2px); }
  .card.is-fav { border-color: rgba(239, 68, 68, 0.7); }
  .card.bulk-selected {
    border: 2px solid #38bdf8;
    box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.35);
  }
  .bulk-check-badge {
    position: absolute;
    top: 8px;
    right: 8px;
    width: 24px;
    height: 24px;
    border-radius: 6px;
    background: rgba(16, 18, 23, 0.85);
    border: 2px solid #94a3b8;
    color: #fff;
    font-size: 14px;
    font-weight: 700;
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 10;
    pointer-events: none;
  }
  body.bulk-mode .bulk-check-badge { display: flex; }
  body.bulk-mode .card-fav-heart { display: none; }
  body.bulk-mode .thumb-wrap { cursor: pointer; }
  .card.bulk-selected .bulk-check-badge {
    background: #0284c7;
    border-color: #38bdf8;
  }

  .card-fav-heart {
    position: absolute;
    top: 7px;
    right: 7px;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: rgba(16, 18, 23, 0.72);
    border: 1px solid rgba(255, 255, 255, 0.18);
    color: #cbd5e1;
    font-size: 14px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    z-index: 8;
    transition: 0.15s;
  }
  .card-fav-heart:hover { transform: scale(1.12); border-color: #ef4444; }
  .card-fav-heart.active {
    background: rgba(239, 68, 68, 0.25);
    border-color: #ef4444;
    color: #ef4444;
  }

  .thumb-wrap {
    position: relative;
    height: 215px;
    background: var(--canvas-bg);
    overflow: hidden;
    cursor: zoom-in;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .thumb-wrap.video-thumb { cursor: pointer; }
  .thumb-wrap img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    transition: 0.2s;
  }
  .thumb-wrap img.flipped { transform: scaleX(-1); }
  .thumb-wrap img.grayscale { filter: grayscale(100%); }
  .video-play-overlay {
    position: absolute;
    width: 54px;
    height: 54px;
    border-radius: 50%;
    background: rgba(124, 92, 255, 0.9);
    border: 2px solid #fff;
    color: #fff;
    font-size: 22px;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.55);
    pointer-events: none;
    transition: transform 0.15s;
  }
  .thumb-wrap:hover .video-play-overlay { transform: scale(1.1); background: #957aff; }

  .badge-top-left {
    position: absolute;
    top: 7px;
    left: 7px;
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    z-index: 5;
  }
  .mini-badge {
    font-size: 10px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 4px;
    color: #fff;
    cursor: pointer;
  }
  .mini-badge.video-badge { background: rgba(124, 92, 255, 0.95); }
  .mini-badge.dup-best { background: rgba(16, 185, 129, 0.95); }
  .mini-badge.dup-worse { background: rgba(239, 68, 68, 0.95); }

  .card-tools {
    display: none;
    gap: 4px;
    padding: 6px;
    background: rgba(0,0,0,0.18);
    border-top: 1px solid var(--border);
  }
  body.artist-mode .card-tools.has-artist-tools,
  .card-tools.has-dup-tools,
  .card-tools.has-vid-tools { display: flex; }
  .card-tools button {
    padding: 4px 6px;
    font-size: 11px;
    flex: 1;
    justify-content: center;
  }
  .card-body {
    padding: 8px 10px;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .card-title {
    font-size: 11.5px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--muted);
  }
  .card-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    max-height: 48px;
    overflow: hidden;
  }
  .card-tags span {
    font-size: 10px;
    background: var(--bg-main);
    padding: 2px 6px;
    border-radius: 4px;
    color: var(--text);
    opacity: 0.88;
  }
  .card-tags span.card-tag-chip {
    cursor: pointer;
    border: 1px solid var(--border);
    transition: 0.15s;
  }
  .card-tags span.card-tag-chip:hover {
    border-color: var(--accent);
    background: var(--bg-hover);
  }
  .card-tags span.card-tag-chip.nsfw-chip {
    border-color: rgba(239, 68, 68, 0.55);
    color: #ef4444;
  }
  .card-tags span.card-tag-chip.active {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
    opacity: 1;
  }

  aside.notes-drawer {
    width: 340px;
    background: var(--bg-panel);
    border-left: 1px solid var(--border);
    display: none;
    flex-direction: column;
    padding: 14px;
    gap: 10px;
    z-index: 120;
  }
  aside.notes-drawer.open { display: flex; }

  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(8, 10, 15, 0.86);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 100;
    padding: 20px;
  }
  .modal-backdrop.open { display: flex; }
  .modal-box {
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    width: 96%;
    max-width: 1260px;
    height: 88vh;
    display: flex;
    overflow: hidden;
  }

  /* Полноэкранный режим просмотра картинки в инспекторе */
  #inspectorModal.fullscreen-view {
    padding: 0;
    background: #000;
  }
  #inspectorModal.fullscreen-view .modal-box {
    width: 100vw;
    max-width: 100vw;
    height: 100vh;
    border-radius: 0;
    border: none;
  }
  #inspectorModal.fullscreen-view .inspector-side {
    display: none !important;
  }
  #inspectorModal.fullscreen-view .inspector-canvas {
    background: #000;
  }
  #inspectorModal.fullscreen-view .insp-transform-wrap img {
    width: 100vw;
    height: 100vh;
    max-width: 100vw;
    max-height: 100vh;
    object-fit: contain;
  }
  #inspectorModal.fullscreen-view.stretch-fill .insp-transform-wrap img {
    object-fit: fill;
  }

  .inspector-canvas {
    flex: 1;
    background: var(--canvas-bg);
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    user-select: none;
    cursor: grab;
  }
  .inspector-canvas.dragging { cursor: grabbing; }

  /* Обёртка трансформации для картинки + единого оверлея перевода текста */
  .insp-transform-wrap {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    max-width: 100%;
    max-height: 100%;
    transform-origin: center center;
    will-change: transform;
  }
  .insp-transform-wrap img {
    max-width: 100%;
    max-height: calc(88vh - 80px);
    width: auto;
    height: auto;
    object-fit: contain;
    pointer-events: none;
    display: block;
  }
  .insp-transform-wrap img.grayscale { filter: grayscale(100%); }

  /* Навигация по галерее в инспекторе */
  .insp-nav-arrow {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    width: 42px;
    height: 64px;
    border-radius: 10px;
    background: rgba(16, 18, 24, 0.72);
    border: 1px solid rgba(255, 255, 255, 0.18);
    color: #fff;
    font-size: 20px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    z-index: 22;
    transition: 0.15s;
    user-select: none;
  }
  .insp-nav-arrow:hover {
    background: var(--accent);
    border-color: #fff;
  }
  .insp-nav-arrow.prev { left: 12px; }
  .insp-nav-arrow.next { right: 12px; }

  /* Поблочное наложение перевода текста поверх каждого текстового блока на картинке */
  .insp-ocr-overlay {
    position: absolute;
    inset: 0;
    pointer-events: none;
    display: none;
    z-index: 14;
  }
  .insp-ocr-overlay.active {
    display: block;
  }
  .ocr-trans-box {
    position: absolute;
    background: #ffffff;
    color: #111827;
    border: none;
    border-radius: 6px;
    padding: 3px 5px;
    font-weight: 700;
    line-height: 1.18;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    text-align: center;
    box-shadow: 0 1px 6px rgba(0, 0, 0, 0.25);
    pointer-events: auto;
    cursor: help;
    word-break: normal;
    overflow-wrap: break-word;
    white-space: normal;
  }
  .ocr-trans-box:hover {
    outline: 1.5px solid var(--accent);
    z-index: 30;
  }

  .zoom-controls {
    position: absolute;
    bottom: 14px;
    left: 50%;
    transform: translateX(-50%);
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 5px 8px;
    display: flex;
    align-items: center;
    gap: 6px;
    z-index: 20;
    cursor: default;
  }
  .zoom-controls span {
    font-size: 12px;
    min-width: 48px;
    text-align: center;
    color: var(--text);
    font-weight: 600;
  }
  .inspector-side {
    width: 365px;
    padding: 14px;
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 10px;
    overflow-y: auto;
  }
  .toast {
    position: fixed;
    bottom: 20px;
    right: 20px;
    background: var(--accent);
    color: #fff;
    padding: 10px 16px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    display: none;
    z-index: 250;
  }
  #shutdownOverlay {
    position: fixed;
    inset: 0;
    background: rgba(10, 12, 18, 0.96);
    color: #fff;
    display: none;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    z-index: 999;
    text-align: center;
    padding: 20px;
  }
</style>
</head>
<body>

<div id="shutdownOverlay">
  <h2 id="shutdownTitle" style="font-size:22px;"></h2>
  <p id="shutdownSub" style="color:var(--muted);font-size:14px;"></p>
</div>



<div id="aiDownloadDrawer" class="progress-drawer">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <strong id="aiDlStep" style="font-size:13px;">⏳ ...</strong>
    <span id="aiDlPercent" style="font-weight:700;color:var(--accent);">0%</span>
  </div>
  <div class="prog-track"><div id="aiDlBar" class="prog-fill"></div></div>
  <div class="prog-meta">
    <span id="aiDlBytes">...</span>
    <span id="aiDlWarn">...</span>
  </div>
</div>

<div id="scanProgressDrawer" class="progress-drawer">
  <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
    <strong id="scanStageTitle" style="font-size:13px;">📂 ...</strong>
    <div style="display:flex;align-items:center;gap:12px;">
      <span id="scanEtaText" style="font-weight:700;color:#38bdf8;">...</span>
      <button class="danger-btn" onclick="cancelScan()" id="btnCancelScan"></button>
    </div>
  </div>
  <div class="prog-track"><div id="scanProgBar" class="prog-fill"></div></div>
  <div class="prog-meta">
    <span id="scanFileText">—</span>
    <span id="scanCountsText">0 / 0</span>
  </div>
</div>

<header>
  <div class="search-wrap">
    <input type="text" id="searchInput" autocomplete="off" oninput="onSearchInput()" onkeydown="onSearchKeyDown(event)" onfocus="onSearchInput()" onblur="hideSearchAutocompleteDelayed()">
    <button type="button" id="searchClearBtn" class="search-clear-btn" onclick="clearSearchInput()" title="Очистить поиск">✖</button>
    <button type="button" id="searchFaqBtn" class="search-faq-btn" onclick="document.getElementById('searchFaqModal').classList.add('open')" title="Как пользоваться поиском (FAQ)">?</button>
    <div id="searchAutocompleteDropdown" class="search-autocomplete-dropdown"></div>
  </div>

  <input type="text" id="folderInput" style="flex:0 1 130px;min-width:85px;">
  <button onclick="pickFolderNative()" id="btnPickFolder" title="Выбрать папку"></button>
  <button class="primary" onclick="startScan(false)" id="btnScanNew" title="Сканировать новые файлы"></button>
  <button onclick="startScan(true)" id="btnRescan" title="Пересканировать заново"></button>

  <div class="media-segmented" id="mediaSegmented" style="flex-shrink:0;">
    <button data-media="images" onclick="setMediaFilter('images')" id="segImages"></button>
    <button data-media="all" onclick="setMediaFilter('all')" class="active" id="segAll"></button>
    <button data-media="videos" onclick="setMediaFilter('videos')" id="segVideos"></button>
  </div>

  <select id="sortSelect" style="max-width:132px;" onchange="onSortChange()">
    <option value="newest" id="optSortNewest"></option>
    <option value="oldest" id="optSortOldest"></option>
    <option value="name_asc" id="optSortNameAsc"></option>
    <option value="name_desc" id="optSortNameDesc"></option>
    <option value="res_desc" id="optSortResDesc"></option>
    <option value="res_asc" id="optSortResAsc"></option>
    <option value="size_desc" id="optSortSizeDesc"></option>
    <option value="random" id="optSortRandom"></option>
  </select>
  <button id="shuffleBtn" onclick="shuffleSort()" title="Перемешать"></button>

  <select id="aspectSelect" class="artist-only" style="max-width:112px;" onchange="loadState()">
    <option value="all" id="optAspAll"></option>
    <option value="portrait" id="optAspPort"></option>
    <option value="landscape" id="optAspLand"></option>
    <option value="square" id="optAspSq"></option>
  </select>

  <button id="favOnlyBtn" onclick="toggleFavoritesOnly()">❤️ <span id="lblFavBtn"></span> (<span id="favCountBadge">0</span>)</button>
  <button id="bulkModeBtn" onclick="toggleBulkMode()">☑️ <span id="lblBulkBtn"></span></button>
  <button onclick="openInspireModal()" id="btnRoulette" class="artist-only"></button>

  <button id="clearFiltersBtn" onclick="resetFilters()" style="display:none;"><span id="lblResetBtn"></span></button>

  <div class="header-right">
    <button onclick="openSettingsModal()" id="btnSettings"></button>
    <button class="danger-btn" onclick="shutdownApp()" id="btnExit"></button>
  </div>
</header>

<div id="bulkActionBar">
  <span class="bulk-count-pill"><span id="lblBulkSelected"></span>: <span id="bulkSelectedCount">0</span></span>
  <button onclick="bulkSelectAllVisible()"><span id="lblBulkAll"></span> (<span id="bulkVisibleTotal">0</span>)</button>
  <button onclick="bulkClearSelection()" id="btnBulkClear"></button>

  <div class="bulk-group">
    <button onclick="bulkSetFavorite(true)" id="btnBulkFavAdd"></button>
    <button onclick="bulkSetFavorite(false)" id="btnBulkFavRem"></button>
  </div>

  <div class="bulk-group">
    <select id="bulkFolderSelect"></select>
    <button class="primary" onclick="bulkMoveToFolder()" id="btnBulkMove"></button>
  </div>

  <div class="bulk-group">
    <input type="text" id="bulkTagInput" style="width:160px;">
    <button class="primary" onclick="bulkAddTag()" id="btnBulkTag"></button>
  </div>

  <button class="danger-btn" onclick="bulkDeleteSelectedFiles()" id="btnBulkDelete">🗑</button>
  <button onclick="toggleBulkMode()" style="margin-left:auto;" id="btnBulkExit"></button>
</div>
<input type="file" id="backupFileInput" accept=".json" style="display:none;" onchange="handleImportBackupFile(event)">

<div class="workspace">
  <aside class="left-sidebar">
    <!-- 1. Папки в приложении (по умолчанию «🗂️ Все файлы» + созданные пользователем папки) -->
    <div class="side-section" data-section="folders">
      <h4 onclick="toggleSectionCollapse(event, 'folders')">
        <span id="lblSecFolders"></span>
        <span style="display:flex;align-items:center;gap:4px;">
          <button onclick="event.stopPropagation();createNewCategoryPrompt()" style="padding:2px 7px;font-size:11px;" id="btnNewCat"></button>
          <button class="collapse-btn">▾</button>
        </span>
      </h4>
      <div class="side-section-body">
        <div id="folderCatList" class="folder-cat-list"></div>
      </div>
    </div>

    <!-- 2. Папки на диске -->
    <div class="side-section" id="subfoldersSection" data-section="subfolders" style="display:none;">
      <h4 onclick="toggleSectionCollapse(event, 'subfolders')">
        <span id="lblSecSubfolders"></span>
        <button class="collapse-btn">▾</button>
      </h4>
      <div class="side-section-body">
        <div id="diskFolderList" class="folder-cat-list"></div>
      </div>
    </div>

    <div class="side-section" data-section="angle">
      <h4 onclick="toggleSectionCollapse(event, 'angle')">
        <span id="lblSecAngle"></span>
        <button class="collapse-btn">▾</button>
      </h4>
      <div class="side-section-body"><div id="tagsAngle" class="tag-cloud"></div></div>
    </div>

    <div class="side-section" data-section="pose">
      <h4 onclick="toggleSectionCollapse(event, 'pose')">
        <span id="lblSecPose"></span>
        <button class="collapse-btn">▾</button>
      </h4>
      <div class="side-section-body"><div id="tagsPose" class="tag-cloud"></div></div>
    </div>

    <!-- Переименовано из «Теги ИИ / Детали» в «🏷️ Общие теги» -->
    <div class="side-section" data-section="detail">
      <h4 onclick="toggleSectionCollapse(event, 'detail')">
        <span id="lblSecDetail"></span>
        <button class="collapse-btn">▾</button>
      </h4>
      <div class="side-section-body"><div id="tagsDetail" class="tag-cloud"></div></div>
    </div>

    <div class="side-section" id="nsfwTagsSection" data-section="nsfw">
      <h4 onclick="toggleSectionCollapse(event, 'nsfw')">
        <span id="lblSecNsfw"></span>
        <button class="collapse-btn">▾</button>
      </h4>
      <div class="side-section-body"><div id="tagsNsfw" class="tag-cloud"></div></div>
    </div>

    <div class="side-section" id="charTagsSection" data-section="char" style="display:none;">
      <h4 onclick="toggleSectionCollapse(event, 'char')">
        <span id="lblSecChar"></span>
        <button class="collapse-btn">▾</button>
      </h4>
      <div class="side-section-body"><div id="tagsChar" class="tag-cloud"></div></div>
    </div>
  </aside>

  <main>
    <div class="status-bar">
      <span id="statusText"></span>
    </div>
    <div id="imageGrid" class="grid"></div>
  </main>


</div>

<!-- Инспектор с кнопкой полноэкранного просмотра и переводчиком текста с картинки -->
<div id="inspectorModal" class="modal-backdrop" onclick="if(event.target===this)closeInspector()">
  <div class="modal-box">
    <div class="inspector-canvas" id="inspCanvas">
      <button type="button" class="insp-nav-arrow prev" onclick="event.stopPropagation();navigateInspector(-1)" title="Предыдущий (←)">◀</button>
      <div class="insp-transform-wrap" id="inspTransformWrap">
        <img id="inspImg" src="" alt="">
        <div id="inspOcrOverlay" class="insp-ocr-overlay"></div>
      </div>
      <button type="button" class="insp-nav-arrow next" onclick="event.stopPropagation();navigateInspector(1)" title="Следующий (→)">▶</button>
      <div class="zoom-controls" onclick="event.stopPropagation()" onmousedown="event.stopPropagation()" ondblclick="event.stopPropagation()">
        <button onclick="changeInspZoom(1 / 1.25)">➖</button>
        <span id="inspZoomLabel">100%</span>
        <button onclick="changeInspZoom(1.25)">➕</button>
        <button onclick="resetInspZoom()">1:1</button>
        <button onclick="toggleInspFullscreen()" id="btnInspFullscreen" title="На весь экран"></button>
        <button onclick="toggleInspStretchFill()" id="btnInspStretch" style="display:none;" title="Растянуть на весь экран без полей">↔↕</button>
        <button class="artist-only" onclick="toggleInspFlip()">↔️ Flip</button>
        <button class="artist-only" onclick="toggleInspGray()">◐</button>
      </div>
    </div>
    <div class="inspector-side">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <h3 id="inspTitle" style="font-size:13.5px;word-break:break-all;"></h3>
        <div style="display:flex;gap:5px;">
          <button onclick="closeInspector()">✖</button>
        </div>
      </div>

      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
        <button id="inspFavBtn" onclick="toggleInspFavorite()" title="Избранное"></button>
        <button id="btnInspTranslateSide" onclick="toggleImageTranslationOverlay(false)" title="Перевести текст на картинке"><span class="trans-icon-badge">文A</span></button>
        <button id="btnInspRetranslate" onclick="toggleImageTranslationOverlay(true)" title="Перевести заново" style="display:none;">🔄</button>
        <button onclick="openInExplorer(currentInspItem.id, event)" id="btnInspExplorer"></button>
        <button id="inspExtPlayerBtn" style="display:none;" class="primary" onclick="openExternalPlayer(currentInspItem.id, event)">▶ Плеер</button>
        <button class="artist-only" onclick="toggleInspFlip()">↔️ Flip H</button>
        <button class="artist-only" onclick="toggleInspGray()" id="btnInspGray"></button>
        <button class="danger-btn" onclick="deleteCurrentFileFromInspector()" id="btnInspDeleteFile" title="Удалить файл">🗑</button>
      </div>

      <div id="inspTranslatedTextBox" style="display:none;font-size:11.5px;color:var(--text);max-height:130px;overflow-y:auto;white-space:pre-wrap;background:var(--bg-main);padding:6px;border-radius:6px;border:1px solid var(--border);"></div>

      

      <div class="side-section collapsed" data-section="insp_folder">
        <h4 onclick="toggleSectionCollapse(event, 'insp_folder')">
          <span id="lblInspAppFolder">📂 Папка в приложении</span>
          <button class="collapse-btn">▸</button>
        </h4>
        <div class="side-section-body">
          <select id="inspFolderCat" style="width:100%;" onchange="updateInspMeta()"></select>
        </div>
      </div>

      <div class="side-section collapsed" data-section="insp_tags">
        <h4 onclick="toggleSectionCollapse(event, 'insp_tags')">
          <span id="lblInspTags"></span>
          <button class="collapse-btn">▸</button>
        </h4>
        <div class="side-section-body">
          <div id="inspTags" class="tag-cloud" style="margin-bottom:7px;"></div>
          <div style="display:flex;gap:6px;">
            <input type="text" id="newTagInput" style="flex:1;">
            <button onclick="addCustomTag()" id="btnAddTag"></button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Рулетка идей -->
<div id="inspireModal" class="modal-backdrop" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="modal-box" style="flex-direction:column;height:auto;max-height:86vh;padding:20px;gap:14px;">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <h3 id="lblRouletteTitle"></h3>
      <div style="display:flex;gap:8px;">
        <button class="primary" onclick="openInspireModal()" id="btnReroll"></button>
        <button onclick="document.getElementById('inspireModal').classList.remove('open')" id="btnCloseRoulette"></button>
      </div>
    </div>
    <div id="inspireGrid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;overflow-y:auto;"></div>
  </div>
</div>

<!-- Настройки -->
<div id="settingsModal" class="modal-backdrop" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="modal-box" style="flex-direction:column;height:78vh;max-height:90vh;max-width:840px;padding:20px;gap:12px;">
    <div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:10px;flex-shrink:0;">
      <h3 id="lblSetTitle"></h3>
      <div style="display:flex;gap:8px;">
        <button class="primary" onclick="saveSettingsFromModal()" id="btnSaveSet"></button>
        <button onclick="document.getElementById('settingsModal').classList.remove('open')" id="btnCloseSet"></button>
      </div>
    </div>

    <div class="settings-layout">
      <!-- Левая колонка: содержимое активной вкладки настроек -->
      <div class="settings-content-area">

    <!-- Вкладка 1: Основные -->
    <div class="set-tab-pane active" id="setPane-general">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
        <div style="display:flex;flex-direction:column;gap:4px;">
          <label style="font-size:12px;font-weight:600;" id="lblSetLang"></label>
          <select id="setLangSelect" onchange="onUiLanguageSelectChange()">
            <option value="ru">🇷🇺 Русский</option>
            <option value="en">🇬🇧 English</option>
          </select>
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;">
          <label style="font-size:12px;font-weight:600;" id="lblSetTheme"></label>
          <select id="setThemeSelect" onchange="markSettingsDirty()">
            <option value="system" id="optThemeSys"></option>
            <option value="dark" id="optThemeDark"></option>
            <option value="light" id="optThemeLight"></option>
          </select>
        </div>
      </div>

      <div style="display:flex;flex-direction:column;gap:8px;background:var(--bg-card);padding:10px;border-radius:8px;border:1px solid var(--border);font-size:12.5px;">
        <label id="lblWrapTagsRu" style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:600;">
          <input type="checkbox" id="setTagsRuCheck" onchange="markSettingsDirty()"> <span id="lblSetTagsRu"></span>
        </label>
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;">
          <input type="checkbox" id="setArtistModeCheck" onchange="markSettingsDirty()"> <span id="lblSetArtist"></span>
        </label>
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;">
          <input type="checkbox" id="setDisableVideosCheck" onchange="markSettingsDirty()"> <span id="lblSetNoVid"></span>
        </label>
      </div>

      <div style="display:flex;flex-direction:column;gap:5px;">
        <label style="font-size:12px;font-weight:600;" id="lblSetPlayer"></label>
        <input type="text" id="setExternalPlayerInput" oninput="markSettingsDirty()" placeholder="PotPlayer / VLC / MPC-HC (.exe)">
      </div>
    </div>

    <!-- Вкладка 2: ИИ и теги -->
    <div class="set-tab-pane" id="setPane-ai">
      <div class="set-tab-desc" id="descTabAi">Управление нейросетью авто-разметки фото и видео, аппаратным ускорением на видеокарте (GPU) и видимостью отдельных категорий тегов в боковой панели.</div>
      <div style="display:flex;flex-direction:column;gap:6px;background:var(--bg-card);padding:10px;border-radius:8px;border:1px solid var(--border);">
        <label style="font-size:12px;font-weight:600;" id="lblSetAiModel"></label>
        <div style="display:flex;gap:8px;align-items:center;">
          <select id="setAiModelSelect" style="flex:1;" onchange="markSettingsDirty();updateAiModelActionButtons()">
            <option value="eva02" id="optAiSmart"></option>
            <option value="vit" id="optAiFast"></option>
          </select>
          <button class="primary" onclick="downloadSelectedModelFromSettings()" id="btnSetDlModel"></button>
          <button class="danger-btn" onclick="deleteSelectedModelFromSettings()" id="btnSetDelModel"></button>
        </div>
        <span id="setAiModelStatusLine" style="font-size:11px;color:var(--muted);"></span>
      </div>

      <div style="display:flex;flex-direction:column;gap:7px;background:var(--bg-card);padding:10px;border-radius:8px;border:1px solid var(--border);font-size:12.5px;">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;">
          <input type="checkbox" id="setAiEnabledCheck" onchange="markSettingsDirty()"> <span id="lblSetAiEn"></span>
        </label>
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;">
          <input type="checkbox" id="setGpuEnabledCheck" onchange="markSettingsDirty()"> <span id="lblSetGpuEn"></span>
        </label>
      </div>

      <div style="display:flex;flex-direction:column;gap:7px;background:var(--bg-card);padding:10px;border-radius:8px;border:1px solid var(--border);font-size:12.5px;">
        <div style="font-weight:700;font-size:12.5px;" id="lblSetCatFilterHeader">🏷️ Активные категории тегов (можно отключить ненужные):</div>
        <div style="display:flex;flex-wrap:wrap;gap:12px;">
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
            <input type="checkbox" id="setCatAngleCheck" onchange="markSettingsDirty()" checked> <span id="lblCatAngle">📐 Ракурс</span>
          </label>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
            <input type="checkbox" id="setCatPoseCheck" onchange="markSettingsDirty()" checked> <span id="lblCatPose">🧘 Поза</span>
          </label>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
            <input type="checkbox" id="setCatDetailCheck" onchange="markSettingsDirty()" checked> <span id="lblCatDetail">🏷️ Общие теги</span>
          </label>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
            <input type="checkbox" id="setCatCharCheck" onchange="markSettingsDirty()" checked> <span id="lblCatChar">👤 Персонажи</span>
          </label>
        </div>
      </div>
    </div>

    <!-- Вкладка 3: Переводчик -->
    <div class="set-tab-pane" id="setPane-translator">
      <div class="set-tab-desc" id="descTabTrans">Настройка распознавания и перевода текста прямо поверх изображений: выбор онлайн-сервиса или скачивание лёгкой локальной нейросети NLLB-200 (работает офлайн на 200 языках).</div>
      <div style="display:flex;flex-direction:column;gap:8px;background:var(--bg-card);padding:11px;border-radius:8px;border:1px solid var(--border);font-size:12.5px;">
        <div style="font-weight:700;font-size:12.5px;" id="lblSetImgTransHeader"></div>
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;">
          <input type="checkbox" id="setImgTransEnabledCheck" onchange="updateImgTransDependentState();markSettingsDirty()"> <span id="lblSetImgTransEn"></span>
        </label>
        <label id="lblWrapImgTransAuto" style="display:flex;align-items:center;gap:8px;cursor:pointer;transition:opacity 0.15s;">
          <input type="checkbox" id="setImgTransAutoCheck" onchange="markSettingsDirty()"> <span id="lblSetImgTransAuto"></span>
        </label>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
          <div style="display:flex;flex-direction:column;gap:3px;">
            <label style="font-size:11.5px;color:var(--muted);" id="lblSetTransProv"></label>
            <select id="setTransProviderSelect" onchange="markSettingsDirty()">
              <option value="google">Google Translate API (бесплатно)</option>
              <option value="local_nllb">🧠 Локальная нейросеть (NLLB-200 INT8, 200 языков)</option>
              <option value="mymemory">MyMemory API (бесплатно)</option>
              <option value="libre">LibreTranslate / DeepLX API</option>
              <option value="openai">OpenAI / DeepSeek / Gemini API</option>
            </select>
          </div>
          <div style="display:flex;flex-direction:column;gap:3px;">
            <label style="font-size:11.5px;color:var(--muted);" id="lblSetTransTarget"></label>
            <select id="setTransTargetLangSelect" onchange="markSettingsDirty()">
              <option value="ru">🇷🇺 На русский (ru)</option>
              <option value="en">🇬🇧 На английский (en)</option>
              <option value="uk">🇺🇦 На украинский (uk)</option>
              <option value="de">🇩🇪 На немецкий (de)</option>
              <option value="es">🇪🇸 На испанский (es)</option>
              <option value="ja">🇯🇵 На японский (ja)</option>
              <option value="zh-CN">🇨🇳 На китайский (zh-CN)</option>
            </select>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
          <input type="text" id="setTransApiUrlInput" oninput="markSettingsDirty()" placeholder="URL API (опционально, для Libre/OpenAI)">
          <input type="password" id="setTransApiKeyInput" oninput="markSettingsDirty()" placeholder="API ключ переводчика (опционально)">
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:2px;">
          <button class="primary" onclick="downloadLocalTransModel()" id="btnDlLocalTrans">⬇️ Скачать локальный переводчик (NLLB-200, ~600 МБ)</button>
          <button class="danger-btn" onclick="deleteLocalTransModel()" id="btnDelLocalTrans" style="display:none;">🗑 Удалить</button>
          <span id="localTransStatusLine" style="font-size:11px;color:var(--muted);"></span>
        </div>
      </div>
    </div>

    <!-- Вкладка 4: Режим 18+ -->
    <div class="set-tab-pane" id="setPane-nsfw">
      <div class="set-tab-desc nsfw-desc" id="descTabNsfw">Отдельное управление контентом 18+: когда режим выключен, все 18+ теги скрываются из боковой панели, карточек и не присваиваются новым файлам при сканировании.</div>
      <div style="display:flex;flex-direction:column;gap:10px;background:var(--bg-card);padding:14px;border-radius:8px;border:1px solid rgba(239, 68, 68, 0.55);font-size:13px;">
        <label style="display:flex;align-items:center;gap:10px;cursor:pointer;color:#ef4444;font-weight:700;">
          <input type="checkbox" id="setNsfwEnabledCheck" onchange="markSettingsDirty()"> <span id="lblSetNsfwEn"></span>
        </label>
        <div id="nsfwRescanTip" style="font-size:11.5px;color:var(--muted);line-height:1.45;">При включении этого режима на уже просканированной базе рекомендуется нажать кнопку <b>«🔄»</b> (пересканировать заново), чтобы ИИ-модель проставила 18+ теги старым файлам.</div>
      </div>
    </div>

    <!-- Вкладка 6 (предпоследняя): Сохранение и загрузка настроек и избранного -->
    <div class="set-tab-pane" id="setPane-backup">
      <div class="set-tab-desc" id="descTabBackup">Экспорт всех ваших настроек, избранных файлов, созданных в приложении папок и пользовательских тегов в один JSON-файл для переноса на другой ПК или восстановления после сброса.</div>
      <div style="background:var(--bg-card);padding:12px;border-radius:8px;border:1px solid var(--border);display:flex;flex-direction:column;gap:10px;">
        <div style="font-size:12.5px;font-weight:700;" id="lblSetBackupHeader"></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button class="primary" onclick="exportHubBackup()" id="btnExportBackup"></button>
          <button onclick="document.getElementById('backupFileInput').click()" id="btnImportBackup"></button>
        </div>
      </div>
    </div>

    <!-- Вкладка 7 (последняя): Сервис, дубликаты и сброс -->
    <div class="set-tab-pane" id="setPane-service">
      <div class="set-tab-desc" id="descTabService">Поиск и очистка дубликатов (повторных файлов), статистика базы и кэша, отладочный запуск мастера настройки и полный сброс программы.</div>
      <div style="background:var(--bg-card);padding:11px;border-radius:8px;border:1px solid var(--border);display:flex;flex-direction:column;gap:8px;">
        <div style="font-size:12.5px;">
          <b id="lblSetDups"></b> <span id="setDupCount">0</span> (<span id="lblSetWorse"></span>: <span id="setWorseDupCount">0</span>)
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button onclick="showDuplicatesFromSettings()" id="btnShowDups"></button>
          <button class="danger-btn" onclick="autoCleanAllDuplicates()" id="btnCleanDups"></button>
        </div>
      </div>

      <div style="background:var(--bg-card);padding:11px;border-radius:8px;border:1px solid var(--border);font-size:12px;display:flex;flex-direction:column;gap:6px;">
        <div style="font-weight:700;font-size:12.5px;" id="lblStatsHeader">📊 Информация о библиотеке и кэше:</div>
        <div style="color:var(--muted);"><span id="lblStatsDir">📁 Папка базы и настроек:</span> <b style="color:var(--text);" id="setAppDir">—</b></div>
        <div style="color:var(--muted);"><span id="lblStatsItems">🖼 Всего файлов в базе:</span> <b style="color:var(--text);" id="setTotalItems">0</b> (<span id="lblStatsFolders">подключено папок:</span> <b style="color:var(--text);" id="setTotalFolders">0</b>)</div>
        <div style="color:var(--muted);"><span id="lblStatsCache">💾 Размер кэша миниатюр и базы:</span> <b style="color:var(--text);"><span id="setCacheMb">0</span> <span id="lblStatsMb">МБ</span></b></div>
      </div>

      <div style="background:rgba(153, 27, 27, 0.16);border:1px solid #ef4444;border-radius:9px;padding:11px;display:flex;flex-direction:column;gap:8px;">
        <button class="danger-btn" style="width:100%;justify-content:center;padding:8px;" onclick="fullResetHub()" id="btnFullReset"></button>
      </div>
    </div>

      </div>

      <!-- Правая колонка: пункты меню настроек -->
      <div class="set-tabs-bar">
        <button type="button" class="set-tab-btn active" data-tab="general" id="tabBtnGeneral" onclick="switchSettingsTab('general')">⚙️ Основные</button>
        <button type="button" class="set-tab-btn" data-tab="ai" id="tabBtnAi" onclick="switchSettingsTab('ai')">🧠 ИИ и теги</button>
        <button type="button" class="set-tab-btn" data-tab="translator" id="tabBtnTrans" onclick="switchSettingsTab('translator')">文A Переводчик</button>
        <button type="button" class="set-tab-btn nsfw-tab" data-tab="nsfw" id="tabBtnNsfw" onclick="switchSettingsTab('nsfw')">🔞 Режим 18+</button>
        <button type="button" class="set-tab-btn" data-tab="backup" id="tabBtnBackup" onclick="switchSettingsTab('backup')">💾 Бэкап</button>
        <button type="button" class="set-tab-btn" data-tab="service" id="tabBtnService" onclick="switchSettingsTab('service')">🛠 Сервис</button>
      </div>
    </div>

    <div id="settingsDirtyWarning" style="display:none;color:#f59e0b;font-weight:700;font-size:12.5px;text-align:center;flex-shrink:0;"></div>
    <div id="settingsVersionFooter" style="font-size:10.5px;color:var(--muted);text-align:center;opacity:0.75;flex-shrink:0;"></div>
  </div>
</div>

<!-- Справка по поиску (FAQ) -->
<div id="searchFaqModal" class="modal-backdrop" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="modal-box" style="flex-direction:column;height:auto;max-width:540px;padding:22px;gap:12px;">
    <div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:10px;">
      <h3 style="font-size:15px;" id="lblFaqModalTitle">📖 Как пользоваться поиском (FAQ)</h3>
      <button onclick="document.getElementById('searchFaqModal').classList.remove('open')">✖</button>
    </div>
    <div class="faq-content" id="faqBodyContent" style="margin-top:0;font-size:12.5px;line-height:1.55;">
      <div><b>1. Комбинированный поиск по тегам (И):</b> вводите теги через запятую или пробел (на русском или английском). Например: <span class="faq-code">улица, ночь</span> покажет работы, где есть оба тега одновременно.</div>
      <div><b>2. Исключение нежелательных тегов (НЕ):</b> поставьте восклицательный знак <span class="faq-code">!</span> или минус <span class="faq-code">-</span> перед тегом. Например: <span class="faq-code">портрет, !монохром</span> скроет все чёрно-белые работы.</div>
      <div><b>3. Быстрая фильтрация в боковой панели:</b> <b>ЛКМ</b> по любому тегу — добавить его в поиск; <b>ПКМ</b> по тегу — исключить его (<span class="faq-code">!тег</span>).</div>
      <div><b>4. Просмотр и горячие клавиши:</b> клавиши <span class="faq-code">←</span> и <span class="faq-code">→</span> листают картинки; колёсико мыши меняет масштаб; кнопка <span class="faq-code">文A</span> переводит текст прямо поверх картинки; <span class="faq-code">⛶</span> включает полноэкранный режим.</div>
    </div>
  </div>
</div>

<!-- Мастер первого запуска -->
<div id="firstRunModal" class="modal-backdrop">
  <div class="modal-box" style="flex-direction:column;height:auto;max-width:530px;padding:24px;gap:15px;">
    <h3 id="frTitle" style="font-size:18px;text-align:center;">🚀 Добро пожаловать в Kokekbooru</h3>

    <div style="display:flex;flex-direction:column;gap:6px;">
      <label style="font-size:12.5px;font-weight:600;">🌐 Язык интерфейса / Language:</label>
      <div style="display:flex;gap:10px;">
        <button id="frBtnRu" class="primary" style="flex:1;justify-content:center;padding:8px;" onclick="selectFirstRunLang('ru')">🇷🇺 Русский</button>
        <button id="frBtnEn" style="flex:1;justify-content:center;padding:8px;" onclick="selectFirstRunLang('en')">🇬🇧 English</button>
      </div>
    </div>

    <div style="background:var(--bg-card);padding:12px;border-radius:8px;border:1px solid var(--border);display:flex;flex-direction:column;gap:10px;">
      <div style="font-size:13px;font-weight:600;" id="frAiHeader">🧠 Скачать ИИ-модель для автотегов фото и видео?</div>
      <select id="frModelSelect" style="width:100%;">
        <option value="eva02" id="frOptSmart">Умная модель: WD-EVA02-Large v3, 1.26 ГБ</option>
        <option value="vit" id="frOptFast">Быстрая модель: WD-ViT v3, 360 МБ</option>
      </select>
      <label style="display:flex;align-items:center;gap:8px;font-size:12.5px;cursor:pointer;" id="frGpuRow">
        <input type="checkbox" id="frGpuCheck" checked>
        <span id="frGpuLabel">⚡ Включить GPU-ускорение видеокарты</span>
      </label>
      <label style="display:flex;align-items:center;gap:8px;font-size:12.5px;cursor:pointer;color:#ef4444;font-weight:600;">
        <input type="checkbox" id="frNsfwCheck">
        <span id="frNsfwLabel">🔞 Включить режим 18+ и показ 18+ тегов</span>
      </label>
    </div>

    <div style="background:var(--bg-card);padding:12px;border-radius:8px;border:1px solid var(--border);display:flex;flex-direction:column;gap:8px;">
      <label style="display:flex;align-items:center;gap:8px;font-size:12.5px;cursor:pointer;font-weight:600;">
        <input type="checkbox" id="frImgTransCheck" checked onchange="document.getElementById('frLocalTransRow').style.display=this.checked?'flex':'none'">
        <span id="frImgTransLabel">🌐 Включить переводчик текста на картинках</span>
      </label>
      <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;padding-left:18px;" id="frLocalTransRow">
        <input type="checkbox" id="frLocalTransCheck">
        <span id="frLocalTransLabel">🧠 Скачать локальную нейросеть перевода (NLLB-200, ~600 МБ, 200 языков) вместо онлайн-API</span>
      </label>
    </div>

    <div style="display:flex;gap:10px;flex-wrap:wrap;">
      <button class="primary" style="flex:2;justify-content:center;padding:10px;font-size:13px;" onclick="finishFirstRun(true)" id="frBtnDownload">⬇️ Скачать ИИ-модель и начать</button>
      <button style="flex:1;justify-content:center;padding:10px;font-size:12.5px;" onclick="finishFirstRun(false)" id="frBtnSkip">Пропустить скачивание</button>
    </div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
const IS_PUBLIC_EDITION = true;

const I18N = {
  ru: {
    searchPh: "🔍 Поиск...",
    folderPh: "Путь к папке",
    pickFolder: "📁",
    scanNew: "⚡ Скан",
    rescan: "🔄",
    segImages: "🖼 Фото",
    segAll: "📦 Все",
    segVideos: "🎬 Видео",
    sortNewest: "🕒 Сначала новые",
    sortOldest: "🕰 Сначала старые",
    sortNameAsc: "🔤 Имя от А до Я",
    sortNameDesc: "🔤 Имя от Я до А",
    sortResDesc: "📐 Большое разрешение",
    sortResAsc: "📐 Маленькое разрешение",
    sortSizeDesc: "💾 Большой размер файла",
    sortRandom: "🔀 Случайный порядок",
    shuffle: "🔀",
    aspAll: "📐 Все форматы",
    aspPort: "📱 Вертикальные",
    aspLand: "🖥 Горизонтальные",
    aspSq: "⏹ Квадратные",
    fav: "Избранное",
    bulk: "Выделить",
    roulette: "🎲 Рулетка",
    reset: "✖ Сброс",
    notes: "Заметки",
    settings: "⚙️ Настройки",
    exit: "⏻ Выход",
    dlAi: "⬇️ Скачать ИИ",
    aiBanner: "🧠 Нейросеть для авто-тегов ещё не скачана.",
    aiBannerDismiss: "Больше не предлагать",
    aiWarn: "Не закрывайте окно до завершения",
    cancelScan: "🛑 Отменить скан",
    bulkSelected: "Выбрано",
    bulkAll: "✅ Выделить все",
    bulkClear: "⬜ Снять",
    bulkFavAdd: "❤️ В избранное",
    bulkFavRem: "🤍 Убрать",
    bulkMove: "📁 Перенести",
    bulkTag: "🏷 Добавить тег",
    bulkExit: "✖ Закрыть",
    secFolders: "📂 Папки в приложении",
    secSubfolders: "📁 Папки на диске",
    secAngle: "📐 Ракурс",
    secPose: "🧘 Поза и действие",
    secDetail: "🏷️ Общие теги",
    secNsfw: "🔞 18+",
    secChar: "👤 Персонажи",
    newCat: "+ Создать",
    allDiskFolders: "🗂️ Все папки на диске",
    emptySub: "Попробуйте изменить запрос или сбросить фильтры.",
    videoTag: "Видео",
    noTags: "Без тегов",
    openExplorer: "📂 Открыть в проводнике",
    inspGray: "◐ Ч/Б",
    inspNote: "Личная заметка:",
    inspAppFolder: "📂 Папка в приложении",
    inspTags: "Теги:",
    addTag: "+ Тег",
    fullscreenBtn: "⛶ На весь экран",
    exitFullscreenBtn: "⛶ Обычный вид",
    translateImgBtn: "Перевести текст на картинке",
    hideTranslateImgBtn: "Скрыть перевод на картинке",
    translatingWait: "⏳ Распознавание и перевод текста...",
    noTextFound: "Текст на картинке не обнаружен",
    notesTitle: "📝 Блокнот заметок",
    newNote: "+ Новая",
    delNote: "🗑 Удалить",
    shownOf: "Показано",
    of: "из",
    setTitle: "⚙️ Настройки",
    setLang: "🌐 Язык интерфейса:",
    setTheme: "🎨 Тема оформления:",
    themeSys: "🌓 Как в системе",
    themeDark: "🌙 Тёмная",
    themeLight: "☀️ Светлая",
    setAiModel: "🧠 Модель ИИ:",
    aiSmart: "Умная: WD-EVA02-Large v3, 1.26 ГБ",
    aiFast: "Быстрая: WD-ViT v3, 360 МБ",
    dlModel: "⬇️ Скачать",
    delModel: "🗑 Удалить",
    modelInstalled: "✅ Модель скачана",
    modelMissing: "⚠️ Модель не скачана",
    setNsfwEn: "🔞 Режим 18+: включить показ и разметку 18+ тегов",
    setTagsRu: "🏷️ Теги на русском языке (переводить все теги на русский)",
    setArtist: "🎨 Режим артиста: отражение по горизонтали, Ч/Б фильтр, фильтр пропорций и рулетка идей",
    setAiEn: "🧠 Включить ИИ-автотеги для фото и видео при сканировании",
    setGpuEn: "⚡ Включить ускорение на видеокарте (DirectML / CUDA — в разы ускоряет ИИ-сканирование)",
    setNoVid: "🚫 Отключить видео в хабе (скрыть видеофайлы из ленты и пропускать их при сканировании)",
    setImgTransHeader: "Автоперевод текста с картинки:",
    setImgTransEn: "Включить функцию перевода текста на картинках",
    setImgTransAuto: "Автоматически переводить текст при открытии каждой картинки",
    setTransProv: "Онлайн API переводчик:",
    setTransTarget: "Язык перевода текста:",
    setBackupHeader: "💾 Сохранение и загрузка настроек, избранного, папок и тегов:",
    exportBackup: "💾 Сохранить в файл (.json)",
    importBackup: "📂 Загрузить из файла (.json)",
    setPlayer: "🎬 Путь к внешнему видеоплееру (если пусто — используется системный плеер по умолчанию):",
    setDups: "📑 Повторки:",
    setWorse: "худших",
    showDups: "📑 Показать повторки на экране",
    cleanDups: "🧹 Удалить все худшие повторки",
    dirtyWarn: "⚠️ Не забудьте сохранить настройки!",
    close: "✖ Закрыть",
    save: "💾 Сохранить",
    fullReset: "🗑️ Полный сброс настроек и сканированного контента",
    rouletteTitle: "🎲 Рулетка идей",
    reroll: "🔄 Перебросить",
    dupBanner: "📑 Показаны повторки",
    shutdownTitle: "⏻ Kokekbooru остановлен",
    shutdownSub: "Локальный сервер завершил работу. Эту вкладку можно закрыть.",
    frTitle: "🚀 Добро пожаловать в Kokekbooru",
    frAiHeader: "🧠 Скачать ИИ-модель для автотегов фото и видео?",
    frGpuLabel: "⚡ Включить GPU-ускорение видеокарты",
    frNsfwLabel: "🔞 Включить режим 18+ и показ 18+ тегов",
    frBtnDownload: "⬇️ Скачать ИИ-модель и начать",
    frBtnSkip: "Пропустить скачивание",
    aiVocabLabel: "Поддерживаемых общих тегов:",
    tabGeneral: "⚙️ Основные",
    tabAi: "🧠 ИИ и теги",
    tabTrans: "文A Переводчик",
    tabNsfw: "🔞 Режим 18+",
    tabBackup: "💾 Бэкап",
    tabService: "🛠 Сервис",
    descTabAi: "Управление нейросетью авто-разметки фото и видео, аппаратным ускорением на видеокарте (GPU) и видимостью отдельных категорий тегов в боковой панели.",
    descTabTrans: "Настройка распознавания и перевода текста прямо поверх изображений: выбор онлайн-сервиса или скачивание лёгкой локальной нейросети NLLB-200 (работает офлайн на 200 языках).",
    descTabNsfw: "Отдельное управление контентом 18+: когда режим выключен, все 18+ теги скрываются из боковой панели, карточек и не присваиваются новым файлам при сканировании.",
    descTabBackup: "Экспорт всех ваших настроек, избранных файлов, созданных в приложении папок и пользовательских тегов в один JSON-файл для переноса на другой ПК или восстановления после сброса.",
    descTabService: "Поиск и очистка дубликатов (повторных файлов), статистика базы и кэша, отладочный запуск мастера настройки и полный сброс программы.",
    catFilterHeader: "🏷️ Активные категории тегов (можно отключить ненужные):",
    catAngle: "📐 Ракурс",
    catPose: "🧘 Поза",
    catDetail: "🏷️ Общие теги",
    catChar: "👤 Персонажи",
    nsfwRescanTip: "При включении этого режима на уже просканированной базе рекомендуется нажать кнопку «🔄» (пересканировать заново), чтобы ИИ-модель проставила 18+ теги старым файлам.",
    dlLocalTrans: "⬇️ Скачать локальный переводчик (NLLB-200, ~600 МБ)",
    delLocalTrans: "🗑 Удалить",
    statsHeader: "📊 Информация о библиотеке и кэше:",
    statsDir: "📁 Папка базы и настроек:",
    statsItems: "🖼 Всего файлов в базе:",
    statsFolders: "подключено папок:",
    statsCache: "💾 Размер кэша миниатюр и базы:",
    statsMb: "МБ",
    debugFirstRun: "🧪 Debug: Имитировать первый запуск",
    faqModalTitle: "📖 Как пользоваться поиском (FAQ)",
    faqHtml: `<div><b>1. Комбинированный поиск по тегам (И):</b> вводите теги через запятую или пробел (на русском или английском). Например: <span class="faq-code">улица, ночь</span> покажет работы, где есть оба тега одновременно.</div><div><b>2. Исключение нежелательных тегов (НЕ):</b> поставьте восклицательный знак <span class="faq-code">!</span> или минус <span class="faq-code">-</span> перед тегом. Например: <span class="faq-code">портрет, !монохром</span> скроет все чёрно-белые работы.</div><div><b>3. Быстрая фильтрация в боковой панели:</b> <b>ЛКМ</b> по любому тегу — добавить его в поиск; <b>ПКМ</b> по тегу — исключить его (<span class="faq-code">!тег</span>).</div><div><b>4. Просмотр и горячие клавиши:</b> клавиши <span class="faq-code">←</span> и <span class="faq-code">→</span> листают картинки; колёсико мыши меняет масштаб; кнопка <span class="faq-code">文A</span> переводит текст прямо поверх картинки; <span class="faq-code">⛶</span> включает полноэкранный режим.</div>`,
    frImgTransLabel: "🌐 Включить переводчик текста на картинках",
    frLocalTransLabel: "🧠 Скачать локальную нейросеть перевода (NLLB-200, ~600 МБ, 200 языков) вместо онлайн-API",
  },
  en: {
    searchPh: "🔍 Search...",
    folderPh: "Folder path",
    pickFolder: "📁",
    scanNew: "⚡ Scan",
    rescan: "🔄",
    segImages: "🖼 Img",
    segAll: "📦 All",
    segVideos: "🎬 Videos",
    sortNewest: "🕒 Newest first",
    sortOldest: "🕰 Oldest first",
    sortNameAsc: "🔤 Name A → Z",
    sortNameDesc: "🔤 Name Z → A",
    sortResDesc: "📐 High Resolution",
    sortResAsc: "📐 Low Resolution",
    sortSizeDesc: "💾 Large File Size",
    sortRandom: "🔀 Random order",
    shuffle: "🔀",
    aspAll: "📐 All Ratios",
    aspPort: "📱 Portrait",
    aspLand: "🖥 Landscape",
    aspSq: "⏹ Square",
    fav: "Favorites",
    bulk: "Select",
    roulette: "🎲 Roulette",
    reset: "✖ Reset",
    notes: "Notes",
    settings: "⚙️ Settings",
    exit: "⏻ Exit",
    dlAi: "⬇️ Download AI",
    aiBanner: "🧠 AI auto-tagging model is not downloaded yet.",
    aiBannerDismiss: "Don't ask again",
    aiWarn: "Do not close this tab until finished",
    cancelScan: "🛑 Cancel Scan",
    bulkSelected: "Selected",
    bulkAll: "✅ Select All",
    bulkClear: "⬜ Clear",
    bulkFavAdd: "❤️ Favorite",
    bulkFavRem: "🤍 Unfavorite",
    bulkMove: "📁 Move",
    bulkTag: "🏷 Add Tag",
    bulkExit: "✖ Close",
    secFolders: "📂 App Folders",
    secSubfolders: "📁 Disk Folders",
    secAngle: "📐 Angle",
    secPose: "🧘 Pose & Action",
    secDetail: "🏷️ General Tags",
    secNsfw: "🔞 18+ / NSFW",
    secChar: "👤 Characters",
    newCat: "+ Create",
    allDiskFolders: "🗂️ All Disk Folders",
    emptySub: "Try changing your search query or resetting filters.",
    videoTag: "Video",
    noTags: "No tags",
    openExplorer: "📂 Show in Explorer",
    inspGray: "◐ B&W",
    inspNote: "Personal note:",
    inspAppFolder: "📂 App Folder",
    inspTags: "Tags:",
    addTag: "+ Tag",
    fullscreenBtn: "⛶ Fullscreen",
    exitFullscreenBtn: "⛶ Normal view",
    translateImgBtn: "Translate image text",
    hideTranslateImgBtn: "Hide image translation",
    translatingWait: "⏳ Detecting & translating text...",
    noTextFound: "No text detected on image",
    notesTitle: "📝 Notes",
    newNote: "+ New",
    delNote: "🗑 Delete",
    shownOf: "Showing",
    of: "of",
    setTitle: "⚙️ Settings",
    setLang: "🌐 Interface Language:",
    setTheme: "🎨 Theme:",
    themeSys: "🌓 System",
    themeDark: "🌙 Dark",
    themeLight: "☀️ Light",
    setAiModel: "🧠 AI Model:",
    aiSmart: "Smart: WD-EVA02-Large v3, 1.26 GB",
    aiFast: "Fast: WD-ViT v3, 360 MB",
    dlModel: "⬇️ Download",
    delModel: "🗑 Delete",
    modelInstalled: "✅ Model downloaded",
    modelMissing: "⚠️ Model not downloaded",
    setNsfwEn: "🔞 18+ Mode: enable showing and tagging 18+ / NSFW tags",
    setTagsRu: "🏷️ Tags in Russian (translate all tags to Russian)",
    setArtist: "🎨 Artist Mode: horizontal flip, B&W filter, aspect filter, and idea roulette",
    setAiEn: "🧠 Enable AI auto-tagging for images & videos on scan",
    setGpuEn: "⚡ Enable GPU acceleration (DirectML / CUDA — dramatically speeds up AI tagging)",
    setNoVid: "🚫 Disable videos in Kokekbooru (hide videos from grid and skip during scans)",
    setImgTransHeader: "Image Text Auto-Translation:",
    setImgTransEn: "Enable image text translation feature",
    setImgTransAuto: "Automatically translate text when opening an image",
    setTransProv: "Online Translator API:",
    setTransTarget: "Target Language:",
    setBackupHeader: "💾 Backup & Restore (settings, favorites, folders, tags):",
    exportBackup: "💾 Save to file (.json)",
    importBackup: "📂 Load from file (.json)",
    setPlayer: "🎬 External video player path (leave empty to use the system default player):",
    setDups: "📑 Duplicates:",
    setWorse: "worse",
    showDups: "📑 Show duplicates",
    cleanDups: "🧹 Delete all worse duplicates",
    dirtyWarn: "⚠️ Don't forget to save settings!",
    close: "✖ Close",
    save: "💾 Save",
    fullReset: "🗑️ Full reset of settings and scanned library",
    rouletteTitle: "🎲 Idea Roulette",
    reroll: "🔄 Reroll",
    dupBanner: "📑 Showing duplicates",
    shutdownTitle: "⏻ Kokekbooru stopped",
    shutdownSub: "Local server shut down. You can close this tab.",
    frTitle: "🚀 Welcome to Kokekbooru",
    frAiHeader: "🧠 Download AI model for image & video auto-tagging?",
    frGpuLabel: "⚡ Enable GPU hardware acceleration",
    frNsfwLabel: "🔞 Enable 18+ / NSFW mode and tags",
    frBtnDownload: "⬇️ Download AI Model & Start",
    frBtnSkip: "Skip Download",
    aiVocabLabel: "Supported general tags:",
    tabGeneral: "⚙️ General",
    tabAi: "🧠 AI & Tags",
    tabTrans: "文A Translator",
    tabNsfw: "🔞 18+ Mode",
    tabBackup: "💾 Backup",
    tabService: "🛠 Service",
    descTabAi: "Configure the AI auto-tagging model for images and videos, GPU hardware acceleration, and visibility of individual tag categories in the sidebar.",
    descTabTrans: "Configure OCR text detection and on-image translation: choose an online API or download the lightweight offline NLLB-200 neural translator (200 languages).",
    descTabNsfw: "Dedicated 18+ / NSFW content control: when disabled, all 18+ tags are hidden from the sidebar, cards, and skipped during scans.",
    descTabBackup: "Export all your settings, favorites, custom app folders, and user tags to a single JSON file for backup or transfer to another PC.",
    descTabService: "Find and clean duplicate files, view library and cache statistics, or perform a full reset.",
    catFilterHeader: "🏷️ Active tag categories (disable any you don't need):",
    catAngle: "📐 Angle",
    catPose: "🧘 Pose",
    catDetail: "🏷️ General Tags",
    catChar: "👤 Characters",
    nsfwRescanTip: "After enabling this mode on an already scanned library, click «🔄» (Rescan All) so the AI assigns 18+ tags to existing files.",
    dlLocalTrans: "⬇️ Download local translator (NLLB-200, ~600 MB)",
    delLocalTrans: "🗑 Delete",
    statsHeader: "📊 Library & Cache Statistics:",
    statsDir: "📁 Data & Settings Folder:",
    statsItems: "🖼 Total Indexed Files:",
    statsFolders: "folders:",
    statsCache: "💾 Thumbnails & DB Cache Size:",
    statsMb: "MB",
    debugFirstRun: "🧪 Debug: Simulate First Launch",
    faqModalTitle: "📖 Search & Usage Guide (FAQ)",
    faqHtml: `<div><b>1. Combined Tag Search (AND):</b> enter tags separated by commas or spaces. Example: <span class="faq-code">outdoors, night</span> shows items matching both tags.</div><div><b>2. Exclude Tags (NOT):</b> prefix any tag with <span class="faq-code">!</span> or <span class="faq-code">-</span>. Example: <span class="faq-code">portrait, !monochrome</span> hides monochrome items.</div><div><b>3. Quick Sidebar Filtering:</b> <b>Left-click</b> a tag to include it in search; <b>Right-click</b> to exclude it (<span class="faq-code">!tag</span>).</div><div><b>4. Viewer & Shortcuts:</b> <span class="faq-code">←</span> and <span class="faq-code">→</span> flip images; mouse wheel zooms; <span class="faq-code">文A</span> translates text over the image; <span class="faq-code">⛶</span> toggles fullscreen.</div>`,
    frImgTransLabel: "🌐 Enable image text translator",
    frLocalTransLabel: "🧠 Download local translation neural model (NLLB-200, ~600 MB, 200 languages) instead of online API",
  }
};

let currentLang = "ru";
let activeMediaFilter = "all";
let activeFolderCat = "general";
let activeSubfolder = "all";
let favoritesOnly = false;
let duplicatesOnly = false;
let artistMode = false;
let nsfwMode = false;
let tagsInRussian = true;
let imgTranslateEnabled = true;
let imgTranslateAuto = false;
let tagRuMap = {};
let pendingUnknownRuTags = new Set();
let randomSeed = Math.floor(Math.random() * 1000000) + 1;
let currentImages = [];
let currentCategories = [];
let currentInspItem = null;
let currentSettings = {};
let lastDuplicatesCount = 0;
let lastWorseDuplicatesCount = 0;
let settingsMetaInfo = {};
let debounceTimer = null;
let scanPollTimer = null;
let aiPollTimer = null;
let allNotes = [];
let currentNoteId = null;
let noteSaveTimer = null;
let isInspFullscreen = false;
let isInspStretchFill = false;
let isOcrOverlayVisible = false;
let currentOcrBlocks = [];

let bulkMode = false;
let selectedImageIds = new Set();
let lastBulkClickedIdx = null;

function t(key) {
  return (I18N[currentLang] && I18N[currentLang][key]) || I18N.ru[key] || key;
}

function formatTagDisplay(rawTag) {
  if (!rawTag) return "";
  if (rawTag === "detail:video" || rawTag === "video") return t("videoTag");
  const shortName = rawTag.includes(":") ? rawTag.split(":", 2)[1] : rawTag;
  if (!tagsInRussian || currentLang !== 'ru') {
    return shortName.replace(/_/g, " ");
  }
  if (tagRuMap[rawTag]) return tagRuMap[rawTag];
  if (tagRuMap[shortName]) return tagRuMap[shortName];
  if (tagRuMap[shortName.toLowerCase()]) return tagRuMap[shortName.toLowerCase()];
  // Если попался редкий тег с латиницей, ставим его в очередь на фоновый онлайн-доперевод
  if (/[a-zA-Z]{3,}/.test(shortName) && !rawTag.startsWith("char:")) {
    pendingUnknownRuTags.add(rawTag);
  }
  return shortName.replace(/_/g, " ");
}

let batchTransTimer = null;
function scheduleUnknownTagsOnlineTranslation() {
  if (!tagsInRussian || pendingUnknownRuTags.size === 0) return;
  if (batchTransTimer) return;
  batchTransTimer = setTimeout(async () => {
    batchTransTimer = null;
    const batch = Array.from(pendingUnknownRuTags).slice(0, 40);
    pendingUnknownRuTags.clear();
    try {
      const res = await fetch('/api/translate_tags_batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tags: batch})
      });
      if (res.ok) {
        const data = await res.json();
        if (data.translations) {
          Object.assign(tagRuMap, data.translations);
          renderGrid(currentImages);
          if (currentInspItem) renderInspTags();
        }
      }
    } catch (e) {}
  }, 400);
}

function applyLanguageUI(lang) {
  currentLang = (lang === "en") ? "en" : "ru";
  const d = I18N[currentLang];
  document.getElementById('searchInput').placeholder = d.searchPh;
  document.getElementById('folderInput').placeholder = d.folderPh;
  document.getElementById('btnPickFolder').textContent = d.pickFolder;
  document.getElementById('btnScanNew').textContent = d.scanNew;
  document.getElementById('btnRescan').textContent = d.rescan;
  document.getElementById('segImages').textContent = d.segImages;
  document.getElementById('segAll').textContent = d.segAll;
  document.getElementById('segVideos').textContent = d.segVideos;
  document.getElementById('optSortNewest').textContent = d.sortNewest;
  document.getElementById('optSortOldest').textContent = d.sortOldest;
  document.getElementById('optSortNameAsc').textContent = d.sortNameAsc;
  document.getElementById('optSortNameDesc').textContent = d.sortNameDesc;
  document.getElementById('optSortResDesc').textContent = d.sortResDesc;
  document.getElementById('optSortResAsc').textContent = d.sortResAsc;
  document.getElementById('optSortSizeDesc').textContent = d.sortSizeDesc;
  document.getElementById('optSortRandom').textContent = d.sortRandom;
  document.getElementById('shuffleBtn').textContent = d.shuffle;
  document.getElementById('optAspAll').textContent = d.aspAll;
  document.getElementById('optAspPort').textContent = d.aspPort;
  document.getElementById('optAspLand').textContent = d.aspLand;
  document.getElementById('optAspSq').textContent = d.aspSq;
  document.getElementById('lblFavBtn').textContent = d.fav;
  document.getElementById('lblBulkBtn').textContent = d.bulk;
  document.getElementById('btnRoulette').textContent = d.roulette;
  document.getElementById('lblResetBtn').textContent = d.reset;
  document.getElementById('btnSettings').textContent = d.settings;
  document.getElementById('btnExit').textContent = d.exit;
  document.getElementById('aiDlWarn').textContent = d.aiWarn;
  document.getElementById('btnCancelScan').textContent = d.cancelScan;

  document.getElementById('lblBulkSelected').textContent = d.bulkSelected;
  document.getElementById('lblBulkAll').textContent = d.bulkAll;
  document.getElementById('btnBulkClear').textContent = d.bulkClear;
  document.getElementById('btnBulkFavAdd').textContent = d.bulkFavAdd;
  document.getElementById('btnBulkFavRem').textContent = d.bulkFavRem;
  document.getElementById('btnBulkMove').textContent = d.bulkMove;
  document.getElementById('btnBulkTag').textContent = d.bulkTag;
  document.getElementById('btnBulkExit').textContent = d.bulkExit;

  document.getElementById('lblSecFolders').textContent = d.secFolders;
  document.getElementById('lblSecSubfolders').textContent = d.secSubfolders;
  document.getElementById('lblSecAngle').textContent = d.secAngle;
  document.getElementById('lblSecPose').textContent = d.secPose;
  document.getElementById('lblSecDetail').textContent = d.secDetail;
  document.getElementById('lblSecNsfw').textContent = d.secNsfw;
  document.getElementById('lblSecChar').textContent = d.secChar;
  document.getElementById('btnNewCat').textContent = d.newCat;

  document.getElementById('btnInspExplorer').textContent = d.openExplorer;
  document.getElementById('btnInspGray').textContent = d.inspGray;
  document.getElementById('lblInspTags').textContent = d.inspTags;
  document.getElementById('btnAddTag').textContent = d.addTag;
  document.getElementById('btnInspFullscreen').textContent = isInspFullscreen ? d.exitFullscreenBtn : d.fullscreenBtn;


  document.getElementById('lblSetTitle').textContent = d.setTitle;
  document.getElementById('lblSetLang').textContent = d.setLang;
  document.getElementById('lblSetTheme').textContent = d.setTheme;
  document.getElementById('optThemeSys').textContent = d.themeSys;
  document.getElementById('optThemeDark').textContent = d.themeDark;
  document.getElementById('optThemeLight').textContent = d.themeLight;
  document.getElementById('lblSetAiModel').textContent = d.setAiModel;
  document.getElementById('optAiSmart').textContent = d.aiSmart;
  document.getElementById('optAiFast').textContent = d.aiFast;
  document.getElementById('btnSetDlModel').textContent = d.dlModel;
  document.getElementById('btnSetDelModel').textContent = d.delModel;
  if (document.getElementById('lblSetNsfwEn')) document.getElementById('lblSetNsfwEn').textContent = d.setNsfwEn;
  document.getElementById('lblSetTagsRu').textContent = d.setTagsRu;
  document.getElementById('lblWrapTagsRu').style.display = (currentLang === 'ru') ? 'flex' : 'none';
  document.getElementById('lblSetArtist').textContent = d.setArtist;
  document.getElementById('lblSetAiEn').textContent = d.setAiEn;
  document.getElementById('lblSetGpuEn').textContent = d.setGpuEn;
  document.getElementById('lblSetNoVid').textContent = d.setNoVid;
  document.getElementById('lblSetImgTransHeader').textContent = d.setImgTransHeader;
  document.getElementById('lblSetImgTransEn').textContent = d.setImgTransEn;
  document.getElementById('lblSetImgTransAuto').textContent = d.setImgTransAuto;
  document.getElementById('lblSetTransProv').textContent = d.setTransProv;
  document.getElementById('lblSetTransTarget').textContent = d.setTransTarget;
  document.getElementById('lblSetBackupHeader').textContent = d.setBackupHeader;
  document.getElementById('btnExportBackup').textContent = d.exportBackup;
  document.getElementById('btnImportBackup').textContent = d.importBackup;
  document.getElementById('lblSetPlayer').textContent = d.setPlayer;
  document.getElementById('lblSetDups').textContent = d.setDups;
  document.getElementById('lblSetWorse').textContent = d.setWorse;
  document.getElementById('btnShowDups').textContent = d.showDups;
  document.getElementById('btnCleanDups').textContent = d.cleanDups;
  document.getElementById('settingsDirtyWarning').textContent = d.dirtyWarn;
  document.getElementById('btnCloseSet').textContent = d.close;
  document.getElementById('btnSaveSet').textContent = d.save;
  document.getElementById('btnFullReset').textContent = d.fullReset;
  document.getElementById('lblRouletteTitle').textContent = d.rouletteTitle;
  document.getElementById('btnReroll').textContent = d.reroll;
  document.getElementById('btnCloseRoulette').textContent = d.close;
  document.getElementById('shutdownTitle').textContent = d.shutdownTitle;
  document.getElementById('shutdownSub').textContent = d.shutdownSub;

  document.getElementById('frTitle').textContent = d.frTitle;
  document.getElementById('frAiHeader').textContent = d.frAiHeader;
  document.getElementById('frOptSmart').textContent = d.aiSmart;
  document.getElementById('frOptFast').textContent = d.aiFast;
  document.getElementById('frGpuLabel').textContent = d.frGpuLabel;
  if (document.getElementById('frNsfwLabel')) document.getElementById('frNsfwLabel').textContent = d.frNsfwLabel;
  document.getElementById('frBtnDownload').textContent = d.frBtnDownload;
  document.getElementById('frBtnSkip').textContent = d.frBtnSkip;
  if (document.getElementById('frImgTransLabel')) document.getElementById('frImgTransLabel').textContent = d.frImgTransLabel;
  if (document.getElementById('frLocalTransLabel')) document.getElementById('frLocalTransLabel').textContent = d.frLocalTransLabel;

  document.getElementById('tabBtnGeneral').textContent = d.tabGeneral;
  document.getElementById('tabBtnAi').textContent = d.tabAi;
  document.getElementById('tabBtnTrans').textContent = d.tabTrans;
  document.getElementById('tabBtnNsfw').textContent = d.tabNsfw;
  document.getElementById('tabBtnBackup').textContent = d.tabBackup;
  document.getElementById('tabBtnService').textContent = d.tabService;

  document.getElementById('descTabAi').textContent = d.descTabAi;
  document.getElementById('descTabTrans').textContent = d.descTabTrans;
  document.getElementById('descTabNsfw').textContent = d.descTabNsfw;
  document.getElementById('descTabBackup').textContent = d.descTabBackup;
  document.getElementById('descTabService').textContent = d.descTabService;

  document.getElementById('lblSetCatFilterHeader').textContent = d.catFilterHeader;
  document.getElementById('lblCatAngle').textContent = d.catAngle;
  document.getElementById('lblCatPose').textContent = d.catPose;
  document.getElementById('lblCatDetail').textContent = d.catDetail;
  document.getElementById('lblCatChar').textContent = d.catChar;
  document.getElementById('nsfwRescanTip').textContent = d.nsfwRescanTip;
  document.getElementById('btnDlLocalTrans').textContent = d.dlLocalTrans;
  document.getElementById('btnDelLocalTrans').textContent = d.delLocalTrans;
  document.getElementById('lblStatsHeader').textContent = d.statsHeader;
  document.getElementById('lblStatsDir').textContent = d.statsDir;
  document.getElementById('lblStatsItems').textContent = d.statsItems;
  document.getElementById('lblStatsFolders').textContent = d.statsFolders;
  document.getElementById('lblStatsCache').textContent = d.statsCache;
  document.getElementById('lblStatsMb').textContent = d.statsMb;
  document.getElementById('lblFaqModalTitle').textContent = d.faqModalTitle;
  if (document.getElementById('lblInspAppFolder')) document.getElementById('lblInspAppFolder').textContent = d.inspAppFolder;
  if (document.getElementById('searchClearBtn')) document.getElementById('searchClearBtn').title = (currentLang === 'en' ? 'Clear search' : 'Очистить поиск');
  if (document.getElementById('btnInspStretch')) document.getElementById('btnInspStretch').title = (currentLang === 'en' ? 'Stretch to fill screen' : 'Растянуть на весь экран без полей');
  if (document.getElementById('btnInspDeleteFile')) document.getElementById('btnInspDeleteFile').title = (currentLang === 'en' ? 'Delete file' : 'Удалить файл');
  if (document.getElementById('btnInspRetranslate')) document.getElementById('btnInspRetranslate').title = (currentLang === 'en' ? 'Re-translate' : 'Перевести заново');
  if (document.getElementById('inspExtPlayerBtn')) document.getElementById('inspExtPlayerBtn').textContent = (currentLang === 'en' ? '▶ Player' : '▶ Плеер');
  document.getElementById('faqBodyContent').innerHTML = d.faqHtml;
}

function selectFirstRunLang(lang) {
  applyLanguageUI(lang);
  const tgtSel = document.getElementById('setTransTargetLangSelect');
  if (tgtSel && (tgtSel.value === 'ru' || tgtSel.value === 'en')) tgtSel.value = lang;
  document.getElementById('frBtnRu').classList.toggle('primary', lang === 'ru');
  document.getElementById('frBtnEn').classList.toggle('primary', lang === 'en');
}

async function finishFirstRun(downloadAiNow) {
  const model = document.getElementById('frModelSelect').value;
  const gpuOn = document.getElementById('frGpuCheck').checked;
  const nsfwEl = document.getElementById('frNsfwCheck');
  const nsfwOn = nsfwEl ? nsfwEl.checked : false;
  const imgTransOn = document.getElementById('frImgTransCheck').checked;
  const localTransOn = imgTransOn && document.getElementById('frLocalTransCheck').checked;
  document.getElementById('firstRunModal').classList.remove('open');

  await fetch('/api/save_settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      language: currentLang,
      ai_model: model,
      gpu_enabled: gpuOn,
      nsfw_enabled: nsfwOn,
      img_translate_enabled: imgTransOn,
      translate_provider: localTransOn ? 'local_nllb' : 'google',
      tags_in_russian: currentLang === 'ru',
      ai_enabled: true,
      first_run_completed: true,
    })
  });

  if (downloadAiNow) {
    document.getElementById('aiDownloadDrawer').classList.add('active');
    await fetch(`/api/start_ai_download?model=${encodeURIComponent(model)}`, {method: 'POST'});
    if (aiPollTimer) clearInterval(aiPollTimer);
    aiPollTimer = setInterval(pollAIDownload, 500);
  }
  if (localTransOn) {
    await fetch('/api/start_trans_model_download', {method: 'POST'});
  }
  loadState();
}

let allSearchableTagsCatalog = [];
let acSelectedIndex = -1;
let acCurrentMatches = [];
let acBlurTimer = null;

function highlightSubstringHtml(text, query) {
  if (!text) return '';
  if (!query) return text;
  const lowText = text.toLowerCase();
  const qLow = query.toLowerCase();
  const idx = lowText.indexOf(qLow);
  if (idx === -1) return text;
  const before = text.slice(0, idx);
  const match = text.slice(idx, idx + query.length);
  const after = text.slice(idx + query.length);
  return `${before}<mark class="ac-hl">${match}</mark>${after}`;
}

function rebuildSearchableTagsCatalog(tags, presetNsfwTags, brawlChars) {
  const mapByShort = new Map();

  function addEntry(rawTag, cnt) {
    const [ns, shortName] = rawTag.includes(':') ? rawTag.split(':', 2) : ['detail', rawTag];
    if (!nsfwMode && ns === 'nsfw') return;
    const key = shortName.toLowerCase();
    const existing = mapByShort.get(key);
    if (!existing || cnt > existing.count) {
      mapByShort.set(key, {
        rawTag,
        ns,
        shortName,
        ruName: formatTagDisplay(rawTag),
        count: cnt,
        // Без префикса категории!
        searchToken: shortName,
      });
    }
  }

  (tags || []).forEach(tObj => addEntry(tObj.tag, tObj.count || 0));
  if (nsfwMode) {
    (presetNsfwTags || []).forEach(nt => addEntry(nt, 0));
  }
  (brawlChars || []).forEach(bc => addEntry(`char:${bc}`, 0));

  // Добавляем также известные переведённые теги из словаря
  Object.keys(tagRuMap || {}).forEach(k => {
    if (k.includes(':')) addEntry(k, 0);
  });

  const catalog = Array.from(mapByShort.values());
  catalog.sort((a, b) => (b.count - a.count) || a.shortName.localeCompare(b.shortName));
  allSearchableTagsCatalog = catalog;
}

function getActiveTypingSegment(inputVal) {
  // Определяем текущее вводимое слово после последней запятой или пробела
  const lastComma = inputVal.lastIndexOf(',');
  const lastSpace = inputVal.lastIndexOf(' ');
  const splitIdx = Math.max(lastComma, lastSpace);
  const prefixBefore = splitIdx >= 0 ? inputVal.slice(0, splitIdx + 1) : '';
  const rawToken = splitIdx >= 0 ? inputVal.slice(splitIdx + 1).trimStart() : inputVal.trimStart();
  let negPrefix = '';
  let cleanQuery = rawToken;
  if (cleanQuery.startsWith('!') || cleanQuery.startsWith('-')) {
    negPrefix = cleanQuery[0];
    cleanQuery = cleanQuery.slice(1).trim();
  }
  if (cleanQuery.includes(':')) {
    cleanQuery = cleanQuery.split(':').pop().trim();
  }
  return { prefixBefore, negPrefix, cleanQuery: cleanQuery.toLowerCase() };
}

function renderSearchAutocomplete() {
  const inp = document.getElementById('searchInput');
  const box = document.getElementById('searchAutocompleteDropdown');
  if (!inp || !box) return;

  const { cleanQuery } = getActiveTypingSegment(inp.value);
  if (!cleanQuery || cleanQuery.length < 1) {
    box.classList.remove('open');
    acCurrentMatches = [];
    acSelectedIndex = -1;
    return;
  }

  const startsMatches = [];
  const containsMatches = [];

  for (const item of allSearchableTagsCatalog) {
    const ruLow = (item.ruName || '').toLowerCase();
    const enLow = (item.shortName || '').toLowerCase();
    const cleanEnSpaces = enLow.replace(/_/g, ' ');
    if (ruLow.startsWith(cleanQuery) || enLow.startsWith(cleanQuery) || cleanEnSpaces.startsWith(cleanQuery)) {
      startsMatches.push(item);
    } else if (ruLow.includes(cleanQuery) || enLow.includes(cleanQuery) || cleanEnSpaces.includes(cleanQuery)) {
      containsMatches.push(item);
    }
  }

  acCurrentMatches = [...startsMatches, ...containsMatches].slice(0, 12);
  if (acCurrentMatches.length === 0) {
    box.classList.remove('open');
    acSelectedIndex = -1;
    return;
  }

  if (acSelectedIndex < 0 || acSelectedIndex >= acCurrentMatches.length) acSelectedIndex = 0;

  box.innerHTML = acCurrentMatches.map((m, idx) => {
    const isAct = (idx === acSelectedIndex);
    const primaryLabel = tagsInRussian ? m.ruName : m.shortName;
    const secondaryLabel = (tagsInRussian && m.ruName.toLowerCase() !== m.shortName.toLowerCase()) ? m.shortName : '';
    const hlPrimary = highlightSubstringHtml(primaryLabel, cleanQuery);
    const hlSecondary = secondaryLabel ? ` <span class="ac-sub">(${highlightSubstringHtml(secondaryLabel, cleanQuery)})</span>` : '';
    return `
      <div class="ac-item ${isAct ? 'active' : ''}" onmousedown="event.preventDefault();applyAutocompleteTag(${idx})">
        <div class="ac-tag-left">
          <span class="ac-dot ns-${m.ns}"></span>
          <span class="ac-label-ns-${m.ns}">${hlPrimary}</span>${hlSecondary}
        </div>
        <span class="ac-cnt">(${m.count})</span>
      </div>
    `;
  }).join('');
  box.classList.add('open');
}

function applyAutocompleteTag(idx) {
  const match = acCurrentMatches[idx];
  if (!match) return;
  const inp = document.getElementById('searchInput');
  const { prefixBefore, negPrefix, cleanQuery } = getActiveTypingSegment(inp.value);
  let sep = prefixBefore;
  if (sep && !sep.endsWith(' ')) {
    sep = sep + ' ';
  }
  // Если пользователь вводил на русском и включены русские теги — можем вставить чистый тег без категории
  const tokenToInsert = match.searchToken;
  inp.value = `${sep}${negPrefix}${tokenToInsert}, `;
  document.getElementById('searchClearBtn').style.display = 'inline-block';
  document.getElementById('searchAutocompleteDropdown').classList.remove('open');
  acCurrentMatches = [];
  acSelectedIndex = -1;
  inp.focus();
  loadState();
}

function onSearchKeyDown(e) {
  const box = document.getElementById('searchAutocompleteDropdown');
  if (!box || !box.classList.contains('open') || acCurrentMatches.length === 0) return;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    acSelectedIndex = (acSelectedIndex + 1) % acCurrentMatches.length;
    renderSearchAutocomplete();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    acSelectedIndex = (acSelectedIndex - 1 + acCurrentMatches.length) % acCurrentMatches.length;
    renderSearchAutocomplete();
  } else if (e.key === 'Enter' || e.key === 'Tab') {
    if (acSelectedIndex >= 0 && acSelectedIndex < acCurrentMatches.length) {
      e.preventDefault();
      applyAutocompleteTag(acSelectedIndex);
    }
  } else if (e.key === 'Escape') {
    box.classList.remove('open');
  }
}

function hideSearchAutocompleteDelayed() {
  clearTimeout(acBlurTimer);
  acBlurTimer = setTimeout(() => {
    const box = document.getElementById('searchAutocompleteDropdown');
    if (box) box.classList.remove('open');
  }, 180);
}

function onSearchInput() {
  const val = document.getElementById('searchInput').value;
  document.getElementById('searchClearBtn').style.display = val.length > 0 ? 'inline-block' : 'none';
  renderSearchAutocomplete();
  debounceLoad();
}

function clearSearchInput() {
  const inp = document.getElementById('searchInput');
  inp.value = '';
  document.getElementById('searchClearBtn').style.display = 'none';
  document.getElementById('searchAutocompleteDropdown').classList.remove('open');
  inp.focus();
  loadState();
}

function applyTheme(themePref) {
  const pref = themePref || "system";
  try { localStorage.setItem('kokek_theme_pref', pref); } catch (e) {}
  let effective = pref;
  if (effective === "system") {
    const isDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    effective = isDark ? "dark" : "light";
  }
  document.documentElement.setAttribute('data-theme', effective);
}
try { applyTheme(localStorage.getItem('kokek_theme_pref') || 'system'); } catch (e) {}

if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if ((currentSettings.theme || 'system') === 'system') applyTheme('system');
  });
}

function applyArtistMode(enabled) {
  artistMode = !!enabled;
  document.body.classList.toggle('artist-mode', artistMode);
}

function showToast(msg) {
  const tEl = document.getElementById('toast');
  tEl.textContent = msg;
  tEl.style.display = 'block';
  setTimeout(() => { tEl.style.display = 'none'; }, 3200);
}

function debounceLoad() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => loadState(), 200);
}

function formatEta(secs, mins) {
  if (!secs || secs <= 0) return currentLang === 'en' ? '< 1s' : '< 1 сек';
  if (secs < 60) return currentLang === 'en' ? `~${secs}s` : `~${secs} сек`;
  return currentLang === 'en' ? `~${mins}m` : `~${mins} мин`;
}

function loadCollapsedSections() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('kokek_collapsed_v31') || '{}'); } catch (e) {}
  document.querySelectorAll('.side-section').forEach(sec => {
    const key = sec.dataset.section;
    const isCollapsed = (saved[key] !== undefined) ? !!saved[key] : true;
    sec.classList.toggle('collapsed', isCollapsed);
    const btn = sec.querySelector('.collapse-btn');
    if (btn) btn.textContent = isCollapsed ? '▸' : '▾';
  });
}

function toggleSectionCollapse(e, key) {
  if (e) e.stopPropagation();
  const sec = document.querySelector(`.side-section[data-section="${key}"]`);
  if (!sec) return;
  const collapsed = sec.classList.toggle('collapsed');
  const btn = sec.querySelector('.collapse-btn');
  if (btn) btn.textContent = collapsed ? '▸' : '▾';
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('kokek_collapsed_v31') || '{}'); } catch (err) {}
  saved[key] = collapsed;
  localStorage.setItem('kokek_collapsed_v31', JSON.stringify(saved));
}

async function shutdownApp() {
  if (!confirm(currentLang === 'en' ? "Shut down Kokekbooru?" : "Завершить работу Kokekbooru?")) return;
  try { await fetch('/api/shutdown', {method: 'POST'}); } catch (e) {}
  document.getElementById('shutdownOverlay').style.display = 'flex';
  try { window.close(); } catch (e) {}
}

function setMediaFilter(mode) {
  activeMediaFilter = mode;
  document.querySelectorAll('#mediaSegmented button').forEach(b => {
    b.classList.toggle('active', b.dataset.media === mode);
  });
  loadState();
}

function getSearchTokens() {
  const raw = document.getElementById('searchInput').value.trim();
  if (!raw) return [];
  if (raw.includes(',')) return raw.split(',').map(s => s.trim()).filter(Boolean);
  return raw.split(/\s+/).map(s => s.trim()).filter(Boolean);
}

function setSearchTokens(tokens) {
  const inp = document.getElementById('searchInput');
  inp.value = tokens.join(', ');
  document.getElementById('searchClearBtn').style.display = inp.value.length > 0 ? 'inline-block' : 'none';
  loadState();
}

function toggleTagInSearch(tag, exclude = false) {
  let tokens = getSearchTokens();
  const norm = tag.toLowerCase();
  const neg = '!' + norm;
  const hasPos = tokens.some(x => x.toLowerCase() === norm);
  const hasNeg = tokens.some(x => x.toLowerCase() === neg);
  tokens = tokens.filter(x => x.toLowerCase() !== norm && x.toLowerCase() !== neg);
  if (exclude) {
    if (!hasNeg) tokens.push(neg);
  } else {
    if (!hasPos) tokens.push(norm);
  }
  setSearchTokens(tokens);
}

function toggleTagFromCard(e, tag) {
  e.stopPropagation();
  toggleTagInSearch(tag, false);
}

async function openInExplorer(id, e) {
  if (e) e.stopPropagation();
  const res = await fetch('/api/open_in_explorer', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: id})
  });
  if (!res.ok) {
    const err = await res.json();
    showToast('❌ ' + (err.detail || 'Error'));
  }
}

let initialSettingsSnapshot = null;

function getSettingsFormSnapshot() {
  return {
    external_player_path: document.getElementById('setExternalPlayerInput').value.trim(),
    tags_in_russian: document.getElementById('setTagsRuCheck').checked,
    cat_angle_enabled: document.getElementById('setCatAngleCheck').checked,
    cat_pose_enabled: document.getElementById('setCatPoseCheck').checked,
    cat_detail_enabled: document.getElementById('setCatDetailCheck').checked,
    cat_char_enabled: document.getElementById('setCatCharCheck').checked,
    artist_mode: document.getElementById('setArtistModeCheck').checked,
    ai_enabled: document.getElementById('setAiEnabledCheck').checked,
    gpu_enabled: document.getElementById('setGpuEnabledCheck').checked,
    disable_videos: document.getElementById('setDisableVideosCheck').checked,
    img_translate_enabled: document.getElementById('setImgTransEnabledCheck').checked,
    img_translate_auto: document.getElementById('setImgTransAutoCheck').checked,
    translate_provider: document.getElementById('setTransProviderSelect').value,
    translate_target_lang: document.getElementById('setTransTargetLangSelect').value,
    translate_api_url: document.getElementById('setTransApiUrlInput').value.trim(),
    translate_api_key: document.getElementById('setTransApiKeyInput').value.trim(),
    ai_model: document.getElementById('setAiModelSelect').value,
    theme: document.getElementById('setThemeSelect').value,
    language: document.getElementById('setLangSelect').value,
    nsfw_enabled: document.getElementById('setNsfwEnabledCheck') ? document.getElementById('setNsfwEnabledCheck').checked : false,
  };
}

function updateImgTransDependentState() {
  const en = document.getElementById('setImgTransEnabledCheck').checked;
  const autoChk = document.getElementById('setImgTransAutoCheck');
  const wrap = document.getElementById('lblWrapImgTransAuto');
  autoChk.disabled = !en;
  if (wrap) {
    wrap.style.opacity = en ? '1' : '0.45';
    wrap.style.pointerEvents = en ? 'auto' : 'none';
  }
}

function onUiLanguageSelectChange() {
  const uiLang = document.getElementById('setLangSelect').value;
  const tgtSel = document.getElementById('setTransTargetLangSelect');
  if (tgtSel) {
    if (uiLang === 'en' && tgtSel.value === 'ru') tgtSel.value = 'en';
    else if (uiLang === 'ru' && tgtSel.value === 'en') tgtSel.value = 'ru';
  }
  markSettingsDirty();
}

function markSettingsDirty() {
  if (!initialSettingsSnapshot) {
    document.getElementById('settingsDirtyWarning').style.display = 'block';
    return;
  }
  const curSnap = getSettingsFormSnapshot();
  const isDirty = JSON.stringify(curSnap) !== JSON.stringify(initialSettingsSnapshot);
  document.getElementById('settingsDirtyWarning').style.display = isDirty ? 'block' : 'none';
}

function updateAiModelActionButtons() {
  const sel = document.getElementById('setAiModelSelect').value;
  const isInstalled = (sel === 'vit') ? !!settingsMetaInfo.vit_installed : !!settingsMetaInfo.eva02_installed;
  document.getElementById('setAiModelStatusLine').textContent = isInstalled ? t('modelInstalled') : t('modelMissing');
  document.getElementById('btnSetDlModel').style.display = isInstalled ? 'none' : 'inline-flex';
  document.getElementById('btnSetDelModel').style.display = isInstalled ? 'inline-flex' : 'none';
}

let transPollTimer = null;
async function downloadLocalTransModel() {
  document.getElementById('settingsModal').classList.remove('open');
  document.getElementById('aiDownloadDrawer').classList.add('active');
  await fetch('/api/start_trans_model_download', {method: 'POST'});
  if (transPollTimer) clearInterval(transPollTimer);
  transPollTimer = setInterval(async () => {
    const res = await fetch('/api/trans_model_progress');
    const d = await res.json();
    document.getElementById('aiDlStep').textContent = d.step || '...';
    document.getElementById('aiDlPercent').textContent = `${d.percent}%`;
    document.getElementById('aiDlBar').style.width = `${d.percent}%`;
    if (!d.downloading) {
      clearInterval(transPollTimer);
      transPollTimer = null;
      setTimeout(() => document.getElementById('aiDownloadDrawer').classList.remove('active'), 1800);
      if (d.error) showToast('❌ ' + d.error);
      else showToast(currentLang === 'en' ? '✅ Local NLLB-200 translator ready!' : '✅ Локальный переводчик NLLB-200 готов!');
    }
  }, 500);
}

async function deleteLocalTransModel() {
  if (!confirm(currentLang === 'en' ? 'Delete local NLLB-200 translation model from disk?' : 'Удалить локальную модель перевода NLLB-200 с диска?')) return;
  await fetch('/api/delete_trans_model', {method: 'POST'});
  await openSettingsModal();
  showToast(currentLang === 'en' ? '🗑 Deleted' : '🗑 Удалено');
}

async function downloadSelectedModelFromSettings() {
  const model = document.getElementById('setAiModelSelect').value;
  document.getElementById('settingsModal').classList.remove('open');
  document.getElementById('aiDownloadDrawer').classList.add('active');
  await fetch(`/api/start_ai_download?model=${encodeURIComponent(model)}`, {method: 'POST'});
  if (aiPollTimer) clearInterval(aiPollTimer);
  aiPollTimer = setInterval(pollAIDownload, 500);
}

async function deleteSelectedModelFromSettings() {
  const model = document.getElementById('setAiModelSelect').value;
  if (!confirm(currentLang === 'en' ? `Delete ${model} model files?` : `Удалить файлы модели ${model}?`)) return;
  const res = await fetch('/api/delete_ai_model', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({model})
  });
  if (res.ok) {
    showToast('🗑 OK');
    await openSettingsModal();
    loadState();
  }
}

function switchSettingsTab(tabKey) {
  document.querySelectorAll('#settingsModal .set-tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabKey);
  });
  document.querySelectorAll('#settingsModal .set-tab-pane').forEach(pane => {
    pane.classList.toggle('active', pane.id === `setPane-${tabKey}`);
  });
}

function applySettingsDataToModal(data) {
  settingsMetaInfo = data || {};
  document.getElementById('setAppDir').textContent = data.app_dir || '—';
  document.getElementById('setTotalItems').textContent = data.total_items ?? currentImages.length;
  document.getElementById('setTotalFolders').textContent = data.total_folders ?? 0;
  document.getElementById('setCacheMb').textContent = data.cache_mb ?? '...';
  document.getElementById('setExternalPlayerInput').value = data.external_player_path || '';
  if (document.getElementById('setNsfwEnabledCheck')) {
    document.getElementById('setNsfwEnabledCheck').checked = !!data.nsfw_enabled;
  }
  document.getElementById('setTagsRuCheck').checked = data.tags_in_russian !== false;
  document.getElementById('setCatAngleCheck').checked = data.cat_angle_enabled !== false;
  document.getElementById('setCatPoseCheck').checked = data.cat_pose_enabled !== false;
  document.getElementById('setCatDetailCheck').checked = data.cat_detail_enabled !== false;
  document.getElementById('setCatCharCheck').checked = data.cat_char_enabled !== false;
  document.getElementById('setArtistModeCheck').checked = !!data.artist_mode;
  document.getElementById('setAiEnabledCheck').checked = data.ai_enabled !== false;
  document.getElementById('setGpuEnabledCheck').checked = data.gpu_enabled !== false;
  document.getElementById('setDisableVideosCheck').checked = !!data.disable_videos;
  document.getElementById('setImgTransEnabledCheck').checked = data.img_translate_enabled !== false;
  document.getElementById('setImgTransAutoCheck').checked = !!data.img_translate_auto;
  document.getElementById('setTransProviderSelect').value = data.translate_provider || 'google';
  document.getElementById('setTransTargetLangSelect').value = data.translate_target_lang || 'ru';
  document.getElementById('setTransApiUrlInput').value = data.translate_api_url || '';
  document.getElementById('setTransApiKeyInput').value = data.translate_api_key || '';
  document.getElementById('setAiModelSelect').value = data.ai_model || 'eva02';
  document.getElementById('setThemeSelect').value = data.theme || 'system';
  document.getElementById('setLangSelect').value = data.language || currentLang;
  document.getElementById('settingsVersionFooter').textContent = `Kokekbooru ${data.version || 'beta 0.1'}`;
  document.getElementById('settingsDirtyWarning').style.display = 'none';
  updateImgTransDependentState();
  initialSettingsSnapshot = getSettingsFormSnapshot();
  updateAiModelActionButtons();
  const ltInstalled = !!data.local_trans_installed;
  document.getElementById('localTransStatusLine').textContent = ltInstalled ? (currentLang === 'en' ? '✅ Local translator (200 languages) downloaded' : '✅ Локальный переводчик (200 языков) скачан') : (currentLang === 'en' ? '⚠️ Not downloaded (needed for offline translation)' : '⚠️ Не скачан');
  document.getElementById('btnDlLocalTrans').style.display = ltInstalled ? 'none' : 'inline-flex';
  document.getElementById('btnDelLocalTrans').style.display = ltInstalled ? 'inline-flex' : 'none';
  document.getElementById('setDupCount').textContent = lastDuplicatesCount;
  document.getElementById('setWorseDupCount').textContent = lastWorseDuplicatesCount;
}

async function openSettingsModal() {
  // Мгновенное открытие окна без ожидания сетевых запросов
  applySettingsDataToModal(Object.keys(settingsMetaInfo).length ? settingsMetaInfo : currentSettings);
  document.getElementById('settingsModal').classList.add('open');

  try {
    const res = await fetch('/api/settings');
    if (res.ok) {
      const data = await res.json();
      applySettingsDataToModal(data);
    }
  } catch (e) {}
}

async function saveSettingsFromModal() {
  const payload = {
    external_player_path: document.getElementById('setExternalPlayerInput').value.trim(),
    tags_in_russian: document.getElementById('setTagsRuCheck').checked,
    cat_angle_enabled: document.getElementById('setCatAngleCheck').checked,
    cat_pose_enabled: document.getElementById('setCatPoseCheck').checked,
    cat_detail_enabled: document.getElementById('setCatDetailCheck').checked,
    cat_char_enabled: document.getElementById('setCatCharCheck').checked,
    artist_mode: document.getElementById('setArtistModeCheck').checked,
    ai_enabled: document.getElementById('setAiEnabledCheck').checked,
    gpu_enabled: document.getElementById('setGpuEnabledCheck').checked,
    disable_videos: document.getElementById('setDisableVideosCheck').checked,
    img_translate_enabled: document.getElementById('setImgTransEnabledCheck').checked,
    img_translate_auto: document.getElementById('setImgTransAutoCheck').checked,
    translate_provider: document.getElementById('setTransProviderSelect').value,
    translate_target_lang: document.getElementById('setTransTargetLangSelect').value,
    translate_api_url: document.getElementById('setTransApiUrlInput').value.trim(),
    translate_api_key: document.getElementById('setTransApiKeyInput').value.trim(),
    ai_model: document.getElementById('setAiModelSelect').value,
    theme: document.getElementById('setThemeSelect').value,
    language: document.getElementById('setLangSelect').value,
  };
  if (document.getElementById('setNsfwEnabledCheck')) {
    payload.nsfw_enabled = document.getElementById('setNsfwEnabledCheck').checked;
  }
  const res = await fetch('/api/save_settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (res.ok) {
    document.getElementById('settingsDirtyWarning').style.display = 'none';
    initialSettingsSnapshot = getSettingsFormSnapshot();
    tagsInRussian = payload.tags_in_russian;
    imgTranslateEnabled = payload.img_translate_enabled;
    imgTranslateAuto = payload.img_translate_auto;
    applyTheme(payload.theme);
    applyLanguageUI(payload.language);
    applyArtistMode(payload.artist_mode);
    showToast('💾 OK');
    loadState();
  }
}

async function exportHubBackup() {
  try {
    const res = await fetch('/api/export_backup');
    const data = await res.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const dateStr = new Date().toISOString().slice(0, 10);
    a.href = url;
    a.download = `kokekbooru_backup_${dateStr}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showToast(currentLang === 'en' ? '💾 Backup file saved' : '💾 Файл резервной копии сохранён');
  } catch (e) {
    showToast(currentLang === 'en' ? '❌ Export error' : '❌ Ошибка экспорта');
  }
}

async function handleImportBackupFile(ev) {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const parsed = JSON.parse(text);
    const res = await fetch('/api/import_backup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({backup_data: parsed})
    });
    const out = await res.json();
    if (res.ok) {
      showToast(currentLang === 'en' ? `📂 Restored (favorites: ${out.restored_favorites || 0}, updated: ${out.restored_items || 0})` : `📂 Загружено (избранных: ${out.restored_favorites || 0}, обновлено: ${out.restored_items || 0})`);
      if (document.getElementById('settingsModal').classList.contains('open')) {
        await openSettingsModal();
      }
      loadState();
    } else {
      showToast('❌ ' + (out.detail || (currentLang === 'en' ? 'Import error' : 'Ошибка импорта')));
    }
  } catch (e) {
    showToast(currentLang === 'en' ? '❌ Invalid JSON file' : '❌ Некорректный JSON файл');
  } finally {
    ev.target.value = '';
  }
}

function showDuplicatesFromSettings() {
  document.getElementById('settingsModal').classList.remove('open');
  duplicatesOnly = true;
  loadState();
}

async function simulateFirstRun() {
  document.getElementById('settingsModal').classList.remove('open');
  await fetch('/api/save_settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ first_run_completed: false, language: '' })
  });
  const browserLang = (navigator.language || 'ru').toLowerCase().startsWith('en') ? 'en' : 'ru';
  selectFirstRunLang(browserLang);
  document.getElementById('frGpuCheck').checked = !!(currentSettings.gpu_enabled !== false);
  if (document.getElementById('frNsfwCheck')) document.getElementById('frNsfwCheck').checked = !!currentSettings.nsfw_enabled;
  if (document.getElementById('frImgTransCheck')) {
    document.getElementById('frImgTransCheck').checked = true;
    document.getElementById('frLocalTransRow').style.display = 'flex';
  }
  if (document.getElementById('frLocalTransCheck')) document.getElementById('frLocalTransCheck').checked = false;
  document.getElementById('firstRunModal').classList.add('open');
}

async function fullResetHub() {
  if (!confirm(currentLang === 'en'
    ? 'Full reset of settings and scanned library? Original files on disk will NOT be deleted.'
    : 'Выполнить полный сброс настроек и просканированной базы? Оригинальные файлы на диске НЕ удалятся.')) return;

  const delModels = confirm(currentLang === 'en'
    ? 'Also delete downloaded AI models (~1.3 GB) from disk?'
    : 'Удалить также скачанные ИИ-модели (~1.3 ГБ) с диска?');

  const res = await fetch('/api/full_reset', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({delete_ai_models: delModels})
  });
  if (res.ok) {
    localStorage.removeItem('kokek_collapsed_v31');
    document.getElementById('folderInput').value = '';
    document.getElementById('settingsModal').classList.remove('open');
    resetFilters();
    loadCollapsedSections();
    showToast('♻️ OK');
  }
}

async function openExternalPlayer(id, e) {
  if (e) e.stopPropagation();
  const res = await fetch('/api/open_external_player', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: id})
  });
  if (!res.ok) {
    const err = await res.json();
    showToast('❌ ' + (err.detail || 'Error'));
  }
}

function toggleFavoritesOnly() {
  favoritesOnly = !favoritesOnly;
  document.getElementById('favOnlyBtn').classList.toggle('fav-toggle', favoritesOnly);
  loadState();
}

async function toggleCardFavorite(e, id) {
  if (e) e.stopPropagation();
  const item = currentImages.find(x => x.id === id);
  if (!item) return;
  item.is_favorite = !item.is_favorite;
  await fetch('/api/update_flags', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: id, is_favorite: item.is_favorite})
  });
  loadState();
}

async function toggleInspFavorite() {
  if (!currentInspItem) return;
  currentInspItem.is_favorite = !currentInspItem.is_favorite;
  updateInspFavButton();
  await fetch('/api/update_flags', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: currentInspItem.id, is_favorite: currentInspItem.is_favorite})
  });
  loadState();
}

function updateInspFavButton() {
  const btn = document.getElementById('inspFavBtn');
  if (!btn || !currentInspItem) return;
  btn.textContent = currentInspItem.is_favorite ? '❤️' : '🤍';
  btn.classList.toggle('fav-toggle', !!currentInspItem.is_favorite);
}

function onSortChange() {
  if (document.getElementById('sortSelect').value === 'random') {
    randomSeed = Math.floor(Math.random() * 1000000) + 1;
  }
  loadState();
}

function shuffleSort() {
  document.getElementById('sortSelect').value = 'random';
  randomSeed = Math.floor(Math.random() * 1000000) + 1;
  loadState();
}

function toggleBulkMode() {
  bulkMode = !bulkMode;
  document.body.classList.toggle('bulk-mode', bulkMode);
  document.getElementById('bulkModeBtn').classList.toggle('active-toggle', bulkMode);
  document.getElementById('bulkActionBar').classList.toggle('active', bulkMode);
  if (!bulkMode) {
    selectedImageIds.clear();
    lastBulkClickedIdx = null;
  }
  updateBulkUI();
  renderGrid(currentImages);
}

function updateBulkUI() {
  document.getElementById('bulkSelectedCount').textContent = selectedImageIds.size;
  document.getElementById('bulkVisibleTotal').textContent = currentImages.length;
}

function toggleBulkSelect(id, idx, shiftKey) {
  if (shiftKey && lastBulkClickedIdx !== null && currentImages.length > 0) {
    const s = Math.min(lastBulkClickedIdx, idx);
    const e = Math.max(lastBulkClickedIdx, idx);
    for (let i = s; i <= e; i++) {
      if (currentImages[i]) selectedImageIds.add(currentImages[i].id);
    }
  } else {
    if (selectedImageIds.has(id)) selectedImageIds.delete(id);
    else selectedImageIds.add(id);
    lastBulkClickedIdx = idx;
  }
  updateBulkUI();
  renderGrid(currentImages);
}

function bulkSelectAllVisible() {
  currentImages.forEach(x => selectedImageIds.add(x.id));
  updateBulkUI();
  renderGrid(currentImages);
}

function bulkClearSelection() {
  selectedImageIds.clear();
  lastBulkClickedIdx = null;
  updateBulkUI();
  renderGrid(currentImages);
}

async function bulkSetFavorite(favState) {
  if (selectedImageIds.size === 0) return;
  await fetch('/api/bulk_favorite', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_ids: Array.from(selectedImageIds), is_favorite: favState})
  });
  loadState();
}

async function bulkMoveToFolder() {
  if (selectedImageIds.size === 0) return;
  const targetCat = document.getElementById('bulkFolderSelect').value;
  if (!targetCat) return;
  await fetch('/api/bulk_move_category', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_ids: Array.from(selectedImageIds), folder_category: targetCat})
  });
  loadState();
}

async function bulkAddTag() {
  if (selectedImageIds.size === 0) return;
  const inp = document.getElementById('bulkTagInput');
  const tagVal = inp.value.trim();
  if (!tagVal) return;
  const res = await fetch('/api/bulk_add_tag', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_ids: Array.from(selectedImageIds), tag: tagVal})
  });
  if (res.ok) {
    inp.value = '';
    loadState();
  }
}

async function dismissAIBannerForever() {
  await fetch('/api/dismiss_ai_banner', {method: 'POST'});
}

async function pickFolderNative() {
  const res = await fetch('/api/pick_folder');
  const data = await res.json();
  if (data.path) document.getElementById('folderInput').value = data.path;
}

async function startScan(forceRescan) {
  const path = document.getElementById('folderInput').value.trim();
  const category = (activeFolderCat && activeFolderCat !== 'all') ? activeFolderCat : 'general';
  if (!path) return;
  const res = await fetch('/api/start_scan', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path, category, force_rescan: !!forceRescan})
  });
  if (!res.ok) {
    const err = await res.json();
    showToast('❌ ' + (err.detail || 'Error'));
    return;
  }
  document.getElementById('scanProgressDrawer').classList.add('active');
  if (scanPollTimer) clearInterval(scanPollTimer);
  scanPollTimer = setInterval(pollScanProgress, 400);
}

async function cancelScan() {
  await fetch('/api/cancel_scan', {method: 'POST'});
}

async function pollScanProgress() {
  const res = await fetch('/api/scan_progress');
  const p = await res.json();
  document.getElementById('scanProgBar').style.width = `${p.percent}%`;
  document.getElementById('scanStageTitle').textContent = `📂 ${p.stage || '...'} (${p.percent}%)`;
  document.getElementById('scanEtaText').textContent = p.running
    ? `${formatEta(p.eta_seconds, p.eta_minutes)} (${p.speed_fps || 0} ${currentLang === 'en' ? 'fps' : 'ф/с'})`
    : 'OK';
  document.getElementById('scanFileText').textContent = p.current_file || '—';
  document.getElementById('scanCountsText').textContent = `${p.current} / ${p.total} (+${p.new_added})`;

  if (!p.running) {
    clearInterval(scanPollTimer);
    scanPollTimer = null;
    setTimeout(() => document.getElementById('scanProgressDrawer').classList.remove('active'), 2000);
    if (p.message) showToast(p.message);
    loadState();
  }
}

async function startAIDownload() {
  document.getElementById('aiDownloadDrawer').classList.add('active');
  await fetch('/api/start_ai_download', {method: 'POST'});
  if (aiPollTimer) clearInterval(aiPollTimer);
  aiPollTimer = setInterval(pollAIDownload, 500);
}

async function pollAIDownload() {
  const res = await fetch('/api/ai_download_progress');
  const d = await res.json();
  document.getElementById('aiDlStep').textContent = d.step || '...';
  document.getElementById('aiDlPercent').textContent = `${d.percent}%`;
  document.getElementById('aiDlBar').style.width = `${d.percent}%`;
  if (d.total_mb > 0) document.getElementById('aiDlBytes').textContent = `${d.downloaded_mb} / ${d.total_mb} ${currentLang === 'en' ? 'MB' : 'МБ'}`;
  if (!d.downloading) {
    clearInterval(aiPollTimer);
    aiPollTimer = null;
    setTimeout(() => document.getElementById('aiDownloadDrawer').classList.remove('active'), 1800);
    if (d.error) showToast('❌ ' + d.error);
    loadState();
  }
}

async function createNewCategoryPrompt() {
  const name = prompt(currentLang === 'en' ? 'New app folder name:' : 'Название новой папки в приложении:');
  if (!name || !name.trim()) return;
  const res = await fetch('/api/manage_category', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'create', label: name.trim()})
  });
  if (res.ok) loadState();
}

async function renameCategoryPrompt(e, key, oldLabel) {
  e.stopPropagation();
  const newName = prompt(currentLang === 'en' ? 'New folder name:' : 'Новое название папки:', oldLabel);
  if (!newName || !newName.trim() || newName.trim() === oldLabel) return;
  await fetch('/api/manage_category', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'rename', key, label: newName.trim()})
  });
  loadState();
}

async function deleteCategoryPrompt(e, key, label) {
  e.stopPropagation();
  if (!confirm(currentLang === 'en' ? `Delete folder «${label}»?` : `Удалить папку «${label}»?`)) return;
  const res = await fetch('/api/manage_category', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'delete', key})
  });
  if (res.ok) {
    if (activeFolderCat === key) activeFolderCat = 'general';
    loadState();
  }
}

async function autoCleanAllDuplicates() {
  if (!confirm(currentLang === 'en' ? 'Delete all worse duplicate copies from disk?' : 'Удалить все худшие копии с диска?')) return;
  const res = await fetch('/api/auto_clean_duplicates', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({delete_from_disk: true})
  });
  const data = await res.json();
  if (res.ok) {
    showToast(`🧹 -${data.deleted} (${data.freed_mb} ${currentLang === 'en' ? 'MB' : 'МБ'})`);
    if (document.getElementById('settingsModal').classList.contains('open')) {
      openSettingsModal();
    }
    loadState();
  }
}

function selectFolderCat(catKey) {
  activeFolderCat = catKey;
  loadState();
}

function selectDiskSubfolder(subName) {
  activeSubfolder = subName;
  loadState();
}

function resetFilters() {
  activeFolderCat = "general";
  activeSubfolder = "all";
  activeMediaFilter = "all";
  favoritesOnly = false;
  duplicatesOnly = false;
  document.getElementById('searchInput').value = "";
  document.getElementById('searchClearBtn').style.display = 'none';
  document.getElementById('aspectSelect').value = "all";
  document.getElementById('sortSelect').value = "newest";
  document.getElementById('favOnlyBtn').classList.remove('fav-toggle');
  document.querySelectorAll('#mediaSegmented button').forEach(b => {
    b.classList.toggle('active', b.dataset.media === 'all');
  });
  loadState();
}

async function loadState() {
  const q = document.getElementById('searchInput').value;
  document.getElementById('searchClearBtn').style.display = q.length > 0 ? 'inline-block' : 'none';
  const aspect = document.getElementById('aspectSelect').value;
  const sort = document.getElementById('sortSelect').value;

  const hasFilters = q || aspect !== 'all' || activeSubfolder !== 'all' || activeFolderCat !== 'general' || activeMediaFilter !== 'all' || favoritesOnly || duplicatesOnly || sort !== 'newest';
  document.getElementById('clearFiltersBtn').style.display = hasFilters ? 'inline-flex' : 'none';

  const url = `/api/state?q=${encodeURIComponent(q)}&aspect=${aspect}&folder_cat=${activeFolderCat}&subfolder=${encodeURIComponent(activeSubfolder)}&media_filter=${activeMediaFilter}&favorites_only=${favoritesOnly}&duplicates_only=${duplicatesOnly}&sort=${encodeURIComponent(sort)}&seed=${randomSeed}`;
  const res = await fetch(url);
  const data = await res.json();

  currentSettings = data.settings || {};
  lastDuplicatesCount = data.duplicates_count || 0;
  lastWorseDuplicatesCount = data.worse_duplicates_count || 0;
  tagRuMap = data.tag_ru_map || {};
  tagsInRussian = currentSettings.tags_in_russian !== false;
  imgTranslateEnabled = currentSettings.img_translate_enabled !== false;
  imgTranslateAuto = !!currentSettings.img_translate_auto;

  applyTheme(currentSettings.theme || 'system');
  applyArtistMode(!!currentSettings.artist_mode);

  nsfwMode = (currentSettings.nsfw_enabled !== undefined) ? !!currentSettings.nsfw_enabled : !IS_PUBLIC_EDITION;
  document.querySelector('.side-section[data-section="angle"]').style.display = (currentSettings.cat_angle_enabled !== false) ? 'block' : 'none';
  document.querySelector('.side-section[data-section="pose"]').style.display = (currentSettings.cat_pose_enabled !== false) ? 'block' : 'none';
  document.querySelector('.side-section[data-section="detail"]').style.display = (currentSettings.cat_detail_enabled !== false) ? 'block' : 'none';
  document.getElementById('nsfwTagsSection').style.display = nsfwMode ? 'block' : 'none';

  document.getElementById('btnInspTranslateSide').style.display = imgTranslateEnabled ? 'inline-flex' : 'none';

  if (!currentSettings.first_run_completed && !currentSettings.language) {
    const browserLang = (navigator.language || 'ru').toLowerCase().startsWith('en') ? 'en' : 'ru';
    selectFirstRunLang(browserLang);
    document.getElementById('frGpuCheck').checked = !!data.gpu_hardware_available;
    document.getElementById('frGpuRow').style.display = data.gpu_hardware_available ? 'flex' : 'none';
    if (document.getElementById('frNsfwCheck')) document.getElementById('frNsfwCheck').checked = false;
    document.getElementById('firstRunModal').classList.add('open');
  } else {
    applyLanguageUI(currentSettings.language || 'ru');
  }

  const noVid = !!currentSettings.disable_videos;
  document.getElementById('mediaSegmented').style.display = noVid ? 'none' : 'inline-flex';
  if (noVid && activeMediaFilter !== 'images') activeMediaFilter = 'images';



  document.getElementById('favCountBadge').textContent = data.favorites_count || 0;

  if (data.folders.length > 0 && !document.getElementById('folderInput').value) {
    document.getElementById('folderInput').value = data.folders[0].path;
  }

  document.getElementById('statusText').textContent = duplicatesOnly
    ? `${t('dupBanner')}: ${data.images.length}`
    : `${t('shownOf')}: ${data.images.length} ${t('of')} ${data.total_indexed}`;



  currentCategories = data.folder_categories;
  renderFolderCategories(data.folder_categories);
  renderSubfolders(data.subfolders, data.total_indexed);
  renderTagsSidebar(data.tags, data.preset_nsfw_tags || [], data.brawl_stars_chars || []);
  currentImages = data.images;
  updateBulkUI();
  renderGrid(data.images);
  scheduleUnknownTagsOnlineTranslation();
}

function renderFolderCategories(cats) {
  const box = document.getElementById('folderCatList');
  box.innerHTML = '';



  const bulkSel = document.getElementById('bulkFolderSelect');
  if (bulkSel) {
    const prevBulkVal = bulkSel.value || (cats[0] ? cats[0].key : 'general');
    bulkSel.innerHTML = cats.map(c => `<option value="${c.key}" ${c.key === prevBulkVal ? 'selected' : ''}>${(c.key === 'general' && currentLang === 'en') ? '🗂️ All Files' : c.label}</option>`).join('');
  }

  const inspSel = document.getElementById('inspFolderCat');
  if (inspSel) inspSel.innerHTML = cats.map(c => `<option value="${c.key}">${(c.key === 'general' && currentLang === 'en') ? '🗂️ All Files' : c.label}</option>`).join('');

  cats.forEach(c => {
    const item = document.createElement('div');
    const isAct = (activeFolderCat === c.key) || (c.key === 'general' && activeFolderCat === 'all');
    item.className = `folder-cat-item ${isAct ? 'active' : ''}`;
    const dispLabel = (c.key === 'general' && currentLang === 'en') ? '🗂️ All Files' : c.label;
    item.innerHTML = `
      <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${dispLabel}</span>
      <div style="display:flex;align-items:center;gap:5px;">
        ${c.key !== 'general' ? `
        <div class="cat-actions">
          <span onclick="renameCategoryPrompt(event, '${c.key}', '${c.label.replace(/'/g, "\\'")}')">✏️</span>
          <span onclick="deleteCategoryPrompt(event, '${c.key}', '${c.label.replace(/'/g, "\\'")}')">🗑</span>
        </div>` : ''}
        <span style="opacity:0.7;font-size:11px;">${c.count}</span>
      </div>
    `;
    item.onclick = () => selectFolderCat(c.key);
    item.ondragover = (e) => { e.preventDefault(); item.classList.add('drag-over'); };
    item.ondragleave = () => item.classList.remove('drag-over');
    item.ondrop = async (e) => {
      e.preventDefault();
      item.classList.remove('drag-over');
      const imgId = parseInt(e.dataTransfer.getData('text/plain'));
      if (!imgId) return;
      await fetch('/api/update_flags', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({image_id: imgId, folder_category: c.key})
      });
      loadState();
    };
    box.appendChild(item);
  });
}

function renderSubfolders(subfolders, totalCount) {
  const subSec = document.getElementById('subfoldersSection');
  const box = document.getElementById('diskFolderList');
  box.innerHTML = '';

  if (subfolders && subfolders.length > 0) {
    subSec.style.display = 'block';
    const allItem = document.createElement('div');
    allItem.className = `folder-cat-item ${activeSubfolder === 'all' ? 'active' : ''}`;
    allItem.innerHTML = `<span>${t('allDiskFolders')}</span><span style="opacity:0.7;font-size:11px;">${totalCount}</span>`;
    allItem.onclick = () => selectDiskSubfolder('all');
    box.appendChild(allItem);

    subfolders.forEach(s => {
      const item = document.createElement('div');
      item.className = `folder-cat-item ${activeSubfolder === s.name ? 'active' : ''}`;
      item.innerHTML = `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">📁 ${s.name}</span><span style="opacity:0.7;font-size:11px;">${s.count}</span>`;
      item.onclick = () => selectDiskSubfolder(s.name);
      box.appendChild(item);
    });
  } else {
    subSec.style.display = 'none';
  }
}

function renderTagsSidebar(tags, presetNsfwTags, brawlChars) {
  const containers = {
    angle: document.getElementById('tagsAngle'),
    pose: document.getElementById('tagsPose'),
    detail: document.getElementById('tagsDetail'),
    nsfw: document.getElementById('tagsNsfw'),
    char: document.getElementById('tagsChar'),
  };
  Object.values(containers).forEach(c => c.innerHTML = '');
  let hasChars = false;

  const activeTokens = new Set(getSearchTokens().map(x => x.toLowerCase()));
  const existingTagSet = new Set(tags.map(t => t.tag));
  const mergedTags = [...tags];
  if (nsfwMode) {
    presetNsfwTags.forEach(nt => {
      if (!existingTagSet.has(nt)) mergedTags.push({tag: nt, count: 0});
    });
  }



  rebuildSearchableTagsCatalog(tags, presetNsfwTags, brawlChars);

  mergedTags.forEach(({tag, count}) => {
    const [ns, label] = tag.includes(':') ? tag.split(':', 2) : ['detail', tag];
    if (ns === 'light') return;
    if (!nsfwMode && ns === 'nsfw') return;
    if (ns === 'char') hasChars = true;
    const target = containers[ns] || containers.detail;
    const pill = document.createElement('div');
    pill.className = `tag-pill ns-${ns}`;

    const low = tag.toLowerCase();
    const shortLow = label.toLowerCase();
    if (activeTokens.has(low) || activeTokens.has(shortLow)) pill.classList.add('included');
    if (activeTokens.has('!' + low) || activeTokens.has('!' + shortLow)) pill.classList.add('excluded');
    if (count === 0) pill.style.opacity = '0.68';

    const displayLabel = formatTagDisplay(tag);
    pill.title = label;
    pill.innerHTML = `<span>${displayLabel}</span><span class="cnt">${count}</span>`;

    const tokenToAdd = label;
    pill.onclick = () => toggleTagInSearch(tokenToAdd, false);
    pill.oncontextmenu = (e) => {
      e.preventDefault();
      toggleTagInSearch(tokenToAdd, true);
    };
    target.appendChild(pill);
  });

  document.getElementById('charTagsSection').style.display = (hasChars && currentSettings.cat_char_enabled !== false) ? 'block' : 'none';
}

function renderGrid(images) {
  const grid = document.getElementById('imageGrid');
  grid.innerHTML = '';

  if (!images || images.length === 0) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="chicken-icon">🐔</div>
        <h3>Nobody here but us chickens!</h3>
        <p>${t('emptySub')}</p>
      </div>
    `;
    return;
  }

  const activeTokens = new Set(getSearchTokens().map(x => x.toLowerCase()));

  images.forEach((item, idx) => {
    const isSelected = selectedImageIds.has(item.id);
    const isFav = !!item.is_favorite;
    const isVid = !!item.is_video;

    const card = document.createElement('div');
    card.className = `card ${isSelected ? 'bulk-selected' : ''} ${isFav ? 'is-fav' : ''}`;
    card.draggable = !bulkMode;
    card.ondragstart = (e) => {
      if (bulkMode) { e.preventDefault(); return; }
      e.dataTransfer.setData('text/plain', String(item.id));
    };

    const imgCls = `${item.flip_h ? 'flipped' : ''} ${item.grayscale ? 'grayscale' : ''}`;
    const shortTags = (item.tags || []).slice(0, 8).map(tg => {
      const safeT = tg.replace(/'/g, "\\'");
      const shortName = tg.split(':').pop();
      const isInc = activeTokens.has(tg.toLowerCase()) || activeTokens.has(shortName.toLowerCase());
      const isNsfw = tg.startsWith('nsfw:');
      const disp = formatTagDisplay(tg);
      return `<span class="card-tag-chip ${isInc ? 'active' : ''} ${isNsfw ? 'nsfw-chip' : ''}" title="${tg}" onclick="toggleTagFromCard(event, '${tg.startsWith('char:') ? shortName.replace(/'/g, "\\'") : safeT}')">${disp}</span>`;
    }).join('');
    const dupCls = item.is_worse_dup ? 'dup-worse' : 'dup-best';
    const toolsCls = `card-tools ${!isVid ? 'has-artist-tools' : 'has-vid-tools'} ${duplicatesOnly ? 'has-dup-tools' : ''}`;

    card.innerHTML = `
      <div class="bulk-check-badge">${isSelected ? '✓' : ''}</div>
      <div class="card-fav-heart ${isFav ? 'active' : ''}" onclick="toggleCardFavorite(event, ${item.id})">${isFav ? '❤️' : '🤍'}</div>
      <div class="thumb-wrap ${isVid ? 'video-thumb' : ''}" onclick="onThumbClick(event, ${item.id}, ${idx}, ${isVid})">
        <img class="${imgCls}" src="/api/thumb/${item.id}" loading="lazy">
        <div class="badge-top-left">
          ${isVid ? `<span class="mini-badge video-badge">🎬 ${t('videoTag')}</span>` : ''}
          ${item.dup_group ? `<span class="mini-badge ${dupCls}" title="${item.dup_group}">${item.is_worse_dup ? '🗑' : '👑'}</span>` : ''}
        </div>
      </div>
      <div class="${toolsCls}">
        ${isVid
          ? `<button class="primary" onclick="openExternalPlayer(${item.id}, event)">▶</button>
             <button onclick="openInspector(${item.id})">🏷</button>`
          : `<button class="artist-only" onclick="quickFlip(${item.id})" title="Flip H">↔️</button>
             <button class="artist-only" onclick="quickGray(${item.id})" title="${currentLang === 'en' ? 'B/W' : 'Ч/Б'}">◐</button>`
        }
        ${duplicatesOnly
          ? `<button onclick="deleteDuplicate(${item.id}, true)" style="color:#ef4444;">🗑</button>`
          : ''
        }
      </div>
      <div class="card-body">
        <div class="card-title" title="${item.path}">${item.width}×${item.height} • ${item.filename}</div>
        <div class="card-tags">${shortTags || `<span style="opacity:0.45">${t('noTags')}</span>`}</div>
      </div>
    `;
    grid.appendChild(card);
  });
}

function onThumbClick(e, id, idx, isVid) {
  if (bulkMode) {
    e.stopPropagation();
    toggleBulkSelect(id, idx, e.shiftKey);
    return;
  }
  if (isVid) {
    openExternalPlayer(id, e);
    return;
  }
  openInspector(id);
}

async function deleteDuplicate(id, fromDisk) {
  if (fromDisk && !confirm(currentLang === 'en' ? 'Delete file from disk?' : 'Удалить файл с диска?')) return;
  const res = await fetch('/api/delete_duplicate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: id, delete_from_disk: fromDisk})
  });
  if (res.ok) loadState();
}

async function deleteCurrentFileFromInspector() {
  if (!currentInspItem) return;
  const msg = currentLang === 'en'
    ? `Delete file «${currentInspItem.filename}» from disk and library?`
    : `Удалить файл «${currentInspItem.filename}» с диска и из базы?`;
  if (!confirm(msg)) return;
  const delId = currentInspItem.id;
  const curIdx = currentImages.findIndex(x => x.id === delId);
  const res = await fetch('/api/delete_duplicate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: delId, delete_from_disk: true})
  });
  if (res.ok) {
    currentImages = currentImages.filter(x => x.id !== delId);
    if (currentImages.length > 0) {
      const nextIdx = Math.min(curIdx, currentImages.length - 1);
      openInspector(currentImages[nextIdx].id);
    } else {
      closeInspector();
    }
    loadState();
  }
}

async function bulkDeleteSelectedFiles() {
  if (selectedImageIds.size === 0) return;
  const msg = currentLang === 'en'
    ? `Delete ${selectedImageIds.size} selected file(s) from disk and library?`
    : `Удалить выбранные файлы (${selectedImageIds.size} шт.) с диска и из базы?`;
  if (!confirm(msg)) return;
  const res = await fetch('/api/bulk_delete_files', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_ids: Array.from(selectedImageIds), delete_from_disk: true})
  });
  if (res.ok) {
    selectedImageIds.clear();
    updateBulkUI();
    loadState();
  }
}

async function quickFlip(id) {
  const item = currentImages.find(x => x.id === id);
  if (!item) return;
  item.flip_h = !item.flip_h;
  await fetch('/api/update_flags', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: id, flip_h: item.flip_h})
  });
  loadState();
}

async function quickGray(id) {
  const item = currentImages.find(x => x.id === id);
  if (!item) return;
  item.grayscale = !item.grayscale;
  await fetch('/api/update_flags', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: id, grayscale: item.grayscale})
  });
  loadState();
}


let inspZoom = 1.0, inspPanX = 0, inspPanY = 0, isPanning = false, panStartX = 0, panStartY = 0;

function applyInspTransform() {
  const wrap = document.getElementById('inspTransformWrap');
  const img = document.getElementById('inspImg');
  if (!wrap || !img || !currentInspItem) return;
  const flipScale = currentInspItem.flip_h ? -1 : 1;
  wrap.style.transform = `translate(${inspPanX}px, ${inspPanY}px) scale(${inspZoom * flipScale}, ${inspZoom})`;
  img.classList.toggle('grayscale', !!currentInspItem.grayscale);
  const lbl = document.getElementById('inspZoomLabel');
  if (lbl) lbl.textContent = `${Math.round(inspZoom * 100)}%`;
}

function changeInspZoom(mult) {
  inspZoom = Math.min(8.0, Math.max(0.2, +(inspZoom * mult).toFixed(3)));
  if (Math.abs(inspZoom - 1.0) < 0.04) {
    inspZoom = 1.0;
    inspPanX = 0;
    inspPanY = 0;
  }
  applyInspTransform();
}

function resetInspZoom() {
  inspZoom = 1.0; inspPanX = 0; inspPanY = 0;
  applyInspTransform();
}

function toggleInspFullscreen() {
  const modal = document.getElementById('inspectorModal');
  isInspFullscreen = !isInspFullscreen;
  modal.classList.toggle('fullscreen-view', isInspFullscreen);
  document.getElementById('btnInspFullscreen').textContent = isInspFullscreen ? t('exitFullscreenBtn') : t('fullscreenBtn');
  document.getElementById('btnInspStretch').style.display = isInspFullscreen ? 'inline-flex' : 'none';

  fitInspectorImageToCanvas();
  if (isInspFullscreen) {
    if (document.documentElement.requestFullscreen) {
      document.documentElement.requestFullscreen().catch(() => {});
    }
  } else {
    isInspStretchFill = false;
    modal.classList.remove('stretch-fill');
    if (document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(() => {});
    }
  }
  resetInspZoom();
}

function toggleInspStretchFill() {
  const modal = document.getElementById('inspectorModal');
  isInspStretchFill = !isInspStretchFill;
  modal.classList.toggle('stretch-fill', isInspStretchFill);
  document.getElementById('btnInspStretch').classList.toggle('active-toggle', isInspStretchFill);
}

// Отрисовка переведённого блока над каждым текстовым блоком картинки
function renderOcrOverlayBoxes(blocks) {
  const ov = document.getElementById('inspOcrOverlay');
  const txtBox = document.getElementById('inspTranslatedTextBox');
  ov.innerHTML = '';

  if (!blocks || blocks.length === 0) {
    ov.classList.remove('active');
    txtBox.style.display = 'block';
    txtBox.textContent = t('noTextFound');
    return;
  }

  const imgEl = document.getElementById('inspImg');
  const imgW = (imgEl && imgEl.clientWidth) ? imgEl.clientWidth : 800;
  const imgH = (imgEl && imgEl.clientHeight) ? imgEl.clientHeight : 600;

  ov.classList.add('active');
  const placedRects = [];

  blocks.forEach(b => {
    const div = document.createElement('div');
    div.className = 'ocr-trans-box';

    const padX = 4;
    const padY = 4;
    const leftPx = Math.max(0, ((b.x / 100.0) * imgW) - padX);
    const topPx = Math.max(0, ((b.y / 100.0) * imgH) - padY);
    const boxPxW = Math.min(imgW - leftPx, Math.max(16, ((b.w / 100.0) * imgW) + padX * 2));
    const boxPxH = Math.min(imgH - topPx, Math.max(12, ((b.h / 100.0) * imgH) + padY * 2));

    div.style.left = `${leftPx.toFixed(1)}px`;
    div.style.top = `${topPx.toFixed(1)}px`;
    div.style.width = `${boxPxW.toFixed(1)}px`;
    div.style.height = `${boxPxH.toFixed(1)}px`;
    if (b.bg) div.style.background = b.bg;
    if (b.fg) div.style.color = b.fg;
    div.title = `${b.orig} → ${b.trans}`;
    div.textContent = b.trans;
    ov.appendChild(div);

    // Точный автоподбор размера шрифта, чтобы весь переведённый текст без обрезки помещался внутри баббла
    const estLines = Math.max(1, b.lines || 1);
    let fontPx = Math.min(26, Math.max(7, (boxPxH / estLines) * 0.72));
    div.style.fontSize = `${fontPx.toFixed(1)}px`;
    while (fontPx > 6.0 && (div.scrollHeight > div.clientHeight + 1 || div.scrollWidth > div.clientWidth + 1)) {
      fontPx -= 0.6;
      div.style.fontSize = `${fontPx.toFixed(1)}px`;
    }
  });

  ov.classList.add('active');
  txtBox.style.display = 'block';
  txtBox.textContent = blocks.map(b => `${b.orig} → ${b.trans}`).join('\n');
}

function clearOcrOverlay() {
  isOcrOverlayVisible = false;
  currentOcrBlocks = [];
  const ov = document.getElementById('inspOcrOverlay');
  if (ov) {
    ov.innerHTML = '';
    ov.classList.remove('active');
  }
  const txtBox = document.getElementById('inspTranslatedTextBox');
  if (txtBox) {
    txtBox.style.display = 'none';
    txtBox.textContent = '';
  }
  document.getElementById('btnInspTranslateSide').classList.remove('active-toggle');
  document.getElementById('btnInspRetranslate').style.display = 'none';
}

async function toggleImageTranslationOverlay(forceRefresh = false) {
  if (!currentInspItem || !imgTranslateEnabled) return;
  if (!forceRefresh && isOcrOverlayVisible) {
    clearOcrOverlay();
    return;
  }

  const txtBox = document.getElementById('inspTranslatedTextBox');
  txtBox.style.display = 'block';
  txtBox.textContent = t('translatingWait');

  try {
    const res = await fetch('/api/translate_image_text', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({image_id: currentInspItem.id, force_refresh: !!forceRefresh})
    });
    const data = await res.json();
    if (!res.ok) {
      txtBox.textContent = '❌ ' + (data.detail || 'Error');
      return;
    }
    currentOcrBlocks = data.blocks || [];
    isOcrOverlayVisible = true;
    document.getElementById('btnInspTranslateSide').classList.add('active-toggle');
    document.getElementById('btnInspRetranslate').style.display = 'inline-flex';
    renderOcrOverlayBoxes(currentOcrBlocks);
  } catch (e) {
    txtBox.textContent = currentLang === 'en' ? '❌ Translator connection error' : '❌ Ошибка соединения с переводчиком';
  }
}

function fitInspectorImageToCanvas() {
  const canvas = document.getElementById('inspCanvas');
  const img = document.getElementById('inspImg');
  if (!canvas || !img) return;
  if (isInspFullscreen) {
    img.style.maxWidth = '100vw';
    img.style.maxHeight = '100vh';
  } else {
    const cw = Math.max(200, canvas.clientWidth - 110);
    const ch = Math.max(200, canvas.clientHeight - 82);
    img.style.maxWidth = cw + 'px';
    img.style.maxHeight = ch + 'px';
  }
}

window.addEventListener('resize', () => {
  if (document.getElementById('inspectorModal').classList.contains('open')) {
    fitInspectorImageToCanvas();
  }
});

function navigateInspector(delta) {
  if (!currentInspItem || !currentImages || currentImages.length === 0) return;
  const curIdx = currentImages.findIndex(x => x.id === currentInspItem.id);
  if (curIdx === -1) return;
  const nextIdx = (curIdx + delta + currentImages.length) % currentImages.length;
  const nextItem = currentImages[nextIdx];
  if (nextItem) {
    openInspector(nextItem.id);
  }
}

window.addEventListener('keydown', (e) => {
  const modalOpen = document.getElementById('inspectorModal').classList.contains('open');
  if (!modalOpen) return;
  const activeTag = (document.activeElement && document.activeElement.tagName) ? document.activeElement.tagName.toLowerCase() : '';
  if (activeTag === 'input' || activeTag === 'textarea' || activeTag === 'select') return;

  if (e.key === 'Escape' && isInspFullscreen) {
    e.preventDefault();
    toggleInspFullscreen();
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault();
    navigateInspector(-1);
  } else if (e.key === 'ArrowRight') {
    e.preventDefault();
    navigateInspector(1);
  }
});

(function initInspZoomPan() {
  const canvas = document.getElementById('inspCanvas');
  if (!canvas) return;
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    changeInspZoom(e.deltaY < 0 ? 1.15 : (1 / 1.15));
  }, {passive: false});
  canvas.addEventListener('mousedown', (e) => {
    if (e.button !== 0 || e.target.closest('.zoom-controls') || e.target.closest('.ocr-trans-box')) return;
    isPanning = true; panStartX = e.clientX - inspPanX; panStartY = e.clientY - inspPanY;
    canvas.classList.add('dragging');
  });
  window.addEventListener('mousemove', (e) => {
    if (!isPanning) return;
    inspPanX = e.clientX - panStartX; inspPanY = e.clientY - panStartY;
    applyInspTransform();
  });
  window.addEventListener('mouseup', () => {
    if (!isPanning) return;
    isPanning = false; canvas.classList.remove('dragging');
  });
  canvas.addEventListener('dblclick', (e) => {
    if (e.target.closest('.zoom-controls')) return;
    if (inspZoom > 1.05) resetInspZoom();
    else { inspZoom = 2.5; applyInspTransform(); }
  });
})();

function openInspector(id) {
  const item = currentImages.find(x => x.id === id);
  if (!item) return;
  currentInspItem = item;
  clearOcrOverlay();
  document.getElementById('inspTitle').textContent = `${item.filename} (${item.width}×${item.height})`;

  const img = document.getElementById('inspImg');
  img.style.opacity = '0';
  img.removeAttribute('src');
  img.onload = () => {
    fitInspectorImageToCanvas();
    resetInspZoom();
    img.style.opacity = '1';
    if (imgTranslateEnabled && imgTranslateAuto && !item.is_video) {
      toggleImageTranslationOverlay(false);
    }
  };
  img.src = `/api/full/${item.id}`;

  document.getElementById('inspFolderCat').value = item.folder_category || 'general';
  document.getElementById('inspExtPlayerBtn').style.display = item.is_video ? 'inline-flex' : 'none';
  document.getElementById('btnInspTranslateSide').style.display = (imgTranslateEnabled && !item.is_video) ? 'inline-flex' : 'none';
  updateInspFavButton();
  document.getElementById('inspectorModal').classList.add('open');
  fitInspectorImageToCanvas();
  resetInspZoom();
  renderInspTags();
}

function closeInspector() {
  clearOcrOverlay();
  if (isInspFullscreen) {
    isInspFullscreen = false;
    isInspStretchFill = false;
    const modal = document.getElementById('inspectorModal');
    modal.classList.remove('fullscreen-view', 'stretch-fill');
    document.getElementById('btnInspStretch').style.display = 'none';
    if (document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(() => {});
    }
  }
  const img = document.getElementById('inspImg');
  img.style.opacity = '0';
  img.removeAttribute('src');
  document.getElementById('inspectorModal').classList.remove('open');
}

async function updateInspMeta() {
  if (!currentInspItem) return;
  const folder_category = document.getElementById('inspFolderCat').value;
  currentInspItem.folder_category = folder_category;
  await fetch('/api/update_flags', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: currentInspItem.id, folder_category})
  });
  loadState();
}


async function toggleInspFlip() {
  if (!currentInspItem) return;
  currentInspItem.flip_h = !currentInspItem.flip_h;
  applyInspTransform();
  await fetch('/api/update_flags', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: currentInspItem.id, flip_h: currentInspItem.flip_h})
  });
  renderGrid(currentImages);
}

async function toggleInspGray() {
  if (!currentInspItem) return;
  currentInspItem.grayscale = !currentInspItem.grayscale;
  applyInspTransform();
  await fetch('/api/update_flags', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: currentInspItem.id, grayscale: currentInspItem.grayscale})
  });
  renderGrid(currentImages);
}

function renderInspTags() {
  const box = document.getElementById('inspTags');
  box.innerHTML = '';
  (currentInspItem.tags || []).forEach(tg => {
    const pill = document.createElement('div');
    pill.className = 'tag-pill';
    pill.title = tg;
    pill.innerHTML = `<span>${formatTagDisplay(tg)}</span><span style="color:#ef4444;margin-left:4px;">✖</span>`;
    pill.onclick = async () => {
      await fetch('/api/update_tag', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({image_id: currentInspItem.id, tag: tg, action: 'remove'})
      });
      currentInspItem.tags = currentInspItem.tags.filter(x => x !== tg);
      renderInspTags();
      loadState();
    };
    box.appendChild(pill);
  });
}

async function addCustomTag() {
  const inp = document.getElementById('newTagInput');
  const val = inp.value.trim();
  if (!val || !currentInspItem) return;
  const res = await fetch('/api/update_tag', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({image_id: currentInspItem.id, tag: val, action: 'add'})
  });
  const data = await res.json();
  if (res.ok) {
    currentInspItem.tags.push(data.tag);
    inp.value = '';
    renderInspTags();
    loadState();
  }
}

async function openInspireModal() {
  const res = await fetch(`/api/inspire?media_filter=${activeMediaFilter}`);
  const data = await res.json();
  const grid = document.getElementById('inspireGrid');
  grid.innerHTML = '';
  if (!data.cards || data.cards.length === 0) {
    showToast('🐔 Nobody here but us chickens!');
    return;
  }
  data.cards.forEach(({role, item}) => {
    const col = document.createElement('div');
    col.className = 'card';
    const isVid = !!item.is_video;
    col.innerHTML = `
      <div class="thumb-wrap ${isVid ? 'video-thumb' : ''}" style="height:250px;" onclick="${isVid ? `openExternalPlayer(${item.id}, event)` : `openInspector(${item.id})`}">
        <img src="/api/thumb/${item.id}">
      </div>
      <div class="card-body">
        <div class="card-title">#${role} • ${item.filename}</div>
      </div>
    `;
    grid.appendChild(col);
  });
  document.getElementById('inspireModal').classList.add('open');
}

applyLanguageUI(currentLang);
loadCollapsedSections();
loadState();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index_page():
    return HTML_UI


def open_browser():
    time.sleep(0.8)
    webbrowser.open("http://127.0.0.1:8765")


if __name__ == "__main__":
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")