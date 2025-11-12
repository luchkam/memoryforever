# bot.py — Memory Forever v0.4
# Шаги: Сюжет(ы) → Формат → Фон → Музыка → Фото(1/2) → Runway → постобработка (wm+audio+титр) → отправка
import os, io, time, uuid, base64, requests, subprocess, shutil, json
from datetime import datetime, timezone
from typing import List
from PIL import Image, ImageDraw, ImageFont
import re, textwrap
import threading
import numpy as np
import math
from uuid import uuid4
from telebot.types import LabeledPrice
from PIL import ImageFilter

# rembg: где лежат модели и сессии вырезки
os.environ.setdefault("U2NET_HOME", os.path.join(os.getcwd(), "models"))
from rembg import remove, new_session
RMBG_SESSION = new_session("u2net")
# Дополнительные модели для портретов (если используешь внутри smart_cutout)
RMBG_HUMAN = new_session("u2net_human_seg")
RMBG_ISNET  = new_session("isnet-general-use")

import telebot

# ---------- КЛЮЧИ ----------
# --- Tochka acquiring ---
TOCHKA_JWT           = os.environ.get("TOCHKA_JWT", "")
TOCHKA_CUSTOMER_CODE = os.environ.get("TOCHKA_CUSTOMER_CODE", "")     # ← из поддержки
TOCHKA_MERCHANT_ID   = os.environ.get("TOCHKA_MERCHANT_ID",   "")
TOCHKA_OK_URL        = os.environ.get("TOCHKA_OK_URL",        "https://api.memoryforever.ru/ok")
TOCHKA_API           = "https://enter.tochka.com/uapi/acquiring/v1.0"
TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
RUNWAY_KEY = os.environ.get("RUNWAY_API_KEY", "")
if not TG_TOKEN or not RUNWAY_KEY:
    print("⚠️ Задай TELEGRAM_BOT_TOKEN и RUNWAY_API_KEY в Secrets.")
bot = telebot.TeleBot(TG_TOKEN, parse_mode="HTML")

# ---------- РЕЖИМЫ/ОТЛАДКА (без OpenAI Assistants) ----------
# Этот флаг оставим как общий «расширенный лог», он НЕ связан больше с OpenAI.
OAI_DEBUG = os.environ.get("OAI_DEBUG", "1") == "1"   # просто флаг подробного лога
# Визуальное превью старт-кадра и промпта (перед Runway)
PREVIEW_START_FRAME = os.environ.get("PREVIEW_START_FRAME", "0") == "1"  # 1 — отправлять пользователю
DEBUG_TO_ADMIN      = os.environ.get("DEBUG_TO_ADMIN", "1") == "1"       # 1 — слать превью админу (если ADMIN_CHAT_ID задан)
RUNWAY_SEND_JPEG    = os.environ.get("RUNWAY_SEND_JPEG", "1") == "1"     # конвертировать старт-кадр в JPEG перед отправкой
START_OVERLAY_DEBUG = os.environ.get("START_OVERLAY_DEBUG", "0") == "1"  # рисовать диагностические рамки на старте
MF_DEBUG            = OAI_DEBUG or (os.environ.get("MF_DEBUG", "0") == "1")
CROSSFADE_SEC = float(os.environ.get("CROSSFADE_SEC", "0.7"))  # длительность кроссфейда между сценами
SINGLE_ALBUM_REJECTED = set()

# Титры / свеча / текст
CANDLE_WIDTH_FRAC = float(os.environ.get("CANDLE_WIDTH_FRAC", "0.32"))  # было 0.26 → больше
MEM_TOP_FRAC      = float(os.environ.get("MEM_TOP_FRAC", "0.48"))       # где начинается памятный текст

# --- Безопасная зона под угловой логотип (для титров) ---
WM_CORNER_WIDTH_PX = int(os.environ.get("WM_CORNER_WIDTH_PX", "120"))  # как в ffmpeg scale=120:-1
WM_CORNER_MARGIN_PX = int(os.environ.get("WM_CORNER_MARGIN_PX", "24")) # как в overlay ... :24

def _wm_safe_top_px() -> int:
    try:
        from PIL import Image
        im = Image.open(WATERMARK_PATH)
        w, h = im.size
        scaled_h = int(round(WM_CORNER_WIDTH_PX * (h / max(1, w))))
        return WM_CORNER_MARGIN_PX + scaled_h + 12  # +12 небольшой запас
    except Exception:
        return 160  # дефолт, если не смогли прочитать файл

# Полностью отключаем любые «ворота/проверки» ассистента (и ниже не используем их нигде)
ASSISTANT_GATE_ENABLED = False  # жёстко OFF
START_OVERLAY_DEBUG = False
# --- Отладка/превью (Assistant OpenAI удалён) ---

def _safe_send_photo(chat_id: int, path: str, caption: str = ""):
    try:
        with open(path, "rb") as ph:
            bot.send_photo(chat_id, ph, caption=caption[:1024])
    except Exception as e:
        print(f"[DBG] send_photo error: {e}")

def _send_debug_preview(uid: int, scene_key: str, start_path: str, prompt: str, gate: dict | None = None):
    """
    Превью старт-кадра и текста промпта.
    Параметр gate оставлен для совместимости с существующими вызовами,
    но игнорируется (ассистент выключен).
    """
    cap = (
        f"🎯 PREVIEW → {scene_key}\n"
        f"prompt[{len(prompt)}]: {prompt}\n"
        f"gate: disabled"
    )
    if PREVIEW_START_FRAME:
        _safe_send_photo(uid, start_path, cap)
    if DEBUG_TO_ADMIN and ADMIN_CHAT_ID:
        try:
            _safe_send_photo(int(ADMIN_CHAT_ID), start_path, f"[uid {uid}] {cap}")
        except Exception as e:
            print(f"[DBG] admin preview err: {e}")

def _is_admin(uid: int) -> bool:
    try:
        return ADMIN_CHAT_ID and str(uid) == str(int(ADMIN_CHAT_ID))
    except Exception:
        return False

# --- Админ для техподдержки (ID чата/пользователя/группы) ---
# Пример: "123456789" для юзера, "-1001234567890" для супергруппы.
_raw_admin = os.environ.get("ADMIN_CHAT_ID", "").strip()
ADMIN_CHAT_ID = int(_raw_admin) if _raw_admin.lstrip("-").isdigit() else None  # None, если не задано корректно

# --- Тексты кнопок главного меню ---
BTN_MENU_MAIN    = "📋 Главное меню"
BTN_MENU_START   = "🎬 Сделать видео"
BTN_MENU_PRICE   = "💲 Стоимость"
BTN_MENU_SUPPORT = "🛟 Техподдержка"
BTN_MENU_GUIDE   = "📘 Инструкция по созданию видео"
BTN_MENU_DEMO    = "🎞 Пример работ"
BTN_MENU_OFFER   = "📄 Договор-оферта"
BTN_MENU_POLICY  = "🔐 Политика данных"

# Кнопка «домой» для всех шагов мастера
BTN_GO_HOME = "🏠 В главное меню"

def kb_main_menu() -> telebot.types.ReplyKeyboardMarkup:
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2, selective=True)
    kb.add(
        telebot.types.KeyboardButton(BTN_MENU_MAIN),
        telebot.types.KeyboardButton(BTN_MENU_START),
    )
    kb.add(
        telebot.types.KeyboardButton(BTN_MENU_PRICE),
        telebot.types.KeyboardButton(BTN_MENU_SUPPORT),
    )
    kb.add(
        telebot.types.KeyboardButton(BTN_MENU_GUIDE),
        telebot.types.KeyboardButton(BTN_MENU_DEMO),
    )
    kb.add(
        telebot.types.KeyboardButton(BTN_MENU_OFFER),
        telebot.types.KeyboardButton(BTN_MENU_POLICY),
    )
    return kb

def show_main_menu(uid: int, text: str | None = None) -> None:
    """Показывает главное меню пользователю."""
    text = text or "Выберите пункт меню или перейдите к созданию видео, нажав «Сделать видео»."
    try:
        bot.send_message(uid, text, reply_markup=kb_main_menu())
    except Exception as e:
        # не падаем из-за телеграм-ошибок (например, пользователь отключил бота)
        print(f"[WARN] show_main_menu({uid}) failed: {e}")

# ---------- ПАПКИ ----------
os.makedirs("uploads",  exist_ok=True)
os.makedirs("renders",  exist_ok=True)

def cleanup_uploads_folder():
    """Очистка папки uploads: оставляем не больше 10 файлов каждого типа"""
    import glob
    import os

    # Очистка входящих фото (паттерн: цифры_цифры_hex.jpg)
    user_photos = glob.glob("uploads/*_*_*.jpg")
    user_photos = [f for f in user_photos if not f.startswith("uploads/start")]
    if len(user_photos) > 20:
        # Сортируем по времени модификации (новые первыми)
        user_photos.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        # Удаляем старые файлы (оставляем только 10 новейших)
        for old_file in user_photos[10:]:
            try:
                os.remove(old_file)
                print(f"[CLEANUP] Удален старый входящий файл: {old_file}")
            except Exception as e:
                print(f"[CLEANUP] Ошибка удаления {old_file}: {e}")

    # Очистка стартовых кадров (паттерн: start_дата_время_hex.png/jpg)
    start_frames = glob.glob("uploads/start_*.png") + glob.glob("uploads/start_*.jpg") + glob.glob("uploads/startframe_*.jpg")
    if len(start_frames) > 20:
        # Сортируем по времени модификации (новые первыми)
        start_frames.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        # Удаляем старые файлы (оставляем только 10 новейших)
        for old_file in start_frames[10:]:
            try:
                os.remove(old_file)
                print(f"[CLEANUP] Удален старый стартовый кадр: {old_file}")
            except Exception as e:
                print(f"[CLEANUP] Ошибка удаления {old_file}: {e}")
os.makedirs("assets",   exist_ok=True)
os.makedirs("audio",    exist_ok=True)
os.makedirs("assets/guide", exist_ok=True)
GUIDE_VIDEO_PATH = os.environ.get("GUIDE_VIDEO_PATH", "assets/guide/guide.mov")
WATERMARK_PATH = "assets/watermark_black.jpg"
# PNG с прозрачностью: «Свеча с двумя гвоздиками»
# положи файл сюда: assets/overlays/candle_flowers.png  (можно переопределить через ENV)
CANDLE_PATH = os.environ.get("CANDLE_PATH", "assets/overlays/candle_flowers.png")

# === FULLFRAME (free hugs) watermark ===
FREE_HUGS_SCENE = "👫 Объятия 5с - БЕСПЛАТНО"

# какой файл брать для полноэкранного ВЗ
FULL_WATERMARK_PATH = os.environ.get("FULL_WATERMARK_PATH", None)
if not FULL_WATERMARK_PATH:
    for _cand in ("assets/watermark_full.png", "assets/watermark.png", WATERMARK_PATH):
        if os.path.isfile(_cand):
            FULL_WATERMARK_PATH = _cand
            break
print(f"[WM] full watermark file: {FULL_WATERMARK_PATH}")

# настройки (можно переопределить через ENV)
FREE_HUGS_WM_MODE   = os.environ.get("FREE_HUGS_WM_MODE", "single")  # 'single' | 'grid'
FREE_HUGS_WM_ALPHA  = float(os.environ.get("FREE_HUGS_WM_ALPHA", "0.25"))  # 0..1
FREE_HUGS_WM_SCALE  = float(os.environ.get("FREE_HUGS_WM_SCALE", "0.90"))  # ширина логотипа как доля ширины кадра (single)
FREE_HUGS_WM_ROTATE = float(os.environ.get("FREE_HUGS_WM_ROTATE", "0"))   # поворот в градусах (single)

FREE_HUGS_WM_GRID_COLS   = int(os.environ.get("FREE_HUGS_WM_GRID_COLS", "3"))
FREE_HUGS_WM_GRID_ROWS   = int(os.environ.get("FREE_HUGS_WM_GRID_ROWS", "6"))
FREE_HUGS_WM_GRID_MARGIN = int(os.environ.get("FREE_HUGS_WM_GRID_MARGIN", "16"))

# === КВОТЫ БЕСПЛАТНОГО СЮЖЕТА (2 генерации на аккаунт) ===
FREE_HUGS_LIMIT = int(os.environ.get("FREE_HUGS_LIMIT", "2"))
# === ЦЕНЫ (можно переопределять через ENV) ===
SCENE_PRICE_10S        = int(os.environ.get("SCENE_PRICE_10S", "100"))  # ₽/сюжет 10 сек
OPT_PRICE_CUSTOM_BG    = int(os.environ.get("OPT_PRICE_CUSTOM_BG", "50"))   # ₽ за свой фон
OPT_PRICE_CUSTOM_MUSIC = int(os.environ.get("OPT_PRICE_CUSTOM_MUSIC", "50"))# ₽ за свой трек
OPT_PRICE_TITLES       = int(os.environ.get("OPT_PRICE_TITLES", "50"))      # ₽ за кастомные титры

# Включить «шлагбаум» оплаты перед запуском рендера
PAYMENT_GATE_ENABLED   = os.environ.get("PAYMENT_GATE_ENABLED", "1") == "1"

QUOTA_DIR = "quota"
FREE_HUGS_QUOTA_FILE = os.path.join(QUOTA_DIR, "free_hugs_usage.json")
# Белый список тестеров (через ENV: FREE_HUGS_WHITELIST="123,456", плюс админ)
FREE_HUGS_WHITELIST = {
    s.strip() for s in os.environ.get("FREE_HUGS_WHITELIST", "").split(",") if s.strip()
}
def is_free_hugs_whitelisted(uid: int) -> bool:
    try:
        if _is_admin(uid):  # админ тоже без лимита
            return True
    except Exception:
        pass
    return str(uid) in FREE_HUGS_WHITELIST

def _quota_load() -> dict:
    os.makedirs(QUOTA_DIR, exist_ok=True)
    try:
        with open(FREE_HUGS_QUOTA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _quota_save(data: dict):
    os.makedirs(QUOTA_DIR, exist_ok=True)
    tmp = FREE_HUGS_QUOTA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FREE_HUGS_QUOTA_FILE)

def get_free_hugs_count(uid: int) -> int:
    data = _quota_load()
    try:
        return int(data.get(str(uid), 0))
    except Exception:
        return 0

def inc_free_hugs_count(uid: int, delta: int = 1):
    data = _quota_load()
    key = str(uid)
    data[key] = int(data.get(key, 0)) + delta
    _quota_save(data)

# --- Robust-определение бесплатной сцены "Объятия 5с" ---
FREE_HUGS_SCENE_KEYS = {
    "👫 Объятия 5с - БЕСПЛАТНО",
    "🫂 Объятия 5с - БЕСПЛАТНО",
}
def _is_free_hugs(scene_key: str) -> bool:
    meta = SCENES.get(scene_key, {})
    return (
        scene_key in FREE_HUGS_SCENE_KEYS
        or (meta.get("kind") == "hug" and meta.get("duration") == 5 and "БЕСПЛАТНО" in scene_key)
    )

# --- Полные тексты оферты/политики (файлы для отправки) ---
LEGAL_DIR = "assets/legal"
OFFER_FULL_BASENAME = "offer_full"     # будем искать assets/legal/offer_full.* 
POLICY_FULL_BASENAME = "policy_full"   # будем искать assets/legal/policy_full.*

# Порядок расширений, которые попробуем найти
LEGAL_EXTS = [".pdf", ".docx", ".doc", ".txt", ".md", ".html"]

def _find_legal_file(basename: str) -> str | None:
    os.makedirs(LEGAL_DIR, exist_ok=True)
    for ext in LEGAL_EXTS:
        p = os.path.join(LEGAL_DIR, basename + ext)
        if os.path.isfile(p):
            return p
    return None

# ---------- СЦЕНЫ / ФОРМАТЫ / ФОНЫ / МУЗЫКА ----------
SCENES = {
    "👫 Объятия 5с - БЕСПЛАТНО":      {"duration": 5,  "kind": "hug",         "people": 2},
    "🫂 Объятия 10с - 100 рублей":    {"duration": 10, "kind": "hug",         "people": 2},
    "💏 Поцелуй 10с - 100 рублей":    {"duration": 10, "kind": "kiss_cheek",  "people": 2},
    "👋 Прощание 10с - 100 рублей":   {"duration": 10, "kind": "wave",        "people": 1},
    "🕊️ Уходит в небеса 10с - 100 рублей": {"duration": 10, "kind": "stairs", "people": 1},
}

FORMATS = {
    "🧍 В рост":   "full-body shot",
    "👨‍💼 По пояс": "waist-up shot",
    "👨‍💼 По грудь": "chest-up shot",
}

# Единый источник истины: фон → путь к картинке
BG_FILES = {
    "☁️ Облака": "assets/backgrounds/bg_stairs.jpg",
    "🔆 Врата света":            "assets/backgrounds/bg_gates.jpg",
    "🪽 Ангелы и крылья":        "assets/backgrounds/bg_angels.jpg",
}

# Для совместимости со старым кодом используем то же имя (кнопки смотрят на ключи BACKGROUNDS)
BACKGROUNDS = BG_FILES  # алиас: те же ключи и те же пути

# ----- Пользовательский фон -----
# Чистое имя без эмодзи для callback-данных
BG_BY_CLEAN = {(name.split(" ", 1)[1] if " " in name else name): name for name in BG_FILES.keys()}
CUSTOM_BG_KEY = "__CUSTOM__"  # маркер в стейте

def _bg_orig_from_clean(clean: str) -> str | None:
    return BG_BY_CLEAN.get(clean)

def cleanup_user_custom_bg(uid: int):
    import glob, os
    for p in glob.glob(f"uploads/custombg_{uid}_*.*"):
        try:
            os.remove(p)
        except Exception:
            pass

MUSIC = {
    "🎵 Спокойная": "audio/soft_pad.mp3",
    "🎵 Церковная": "audio/gentle_arpeggio.mp3",
    "🎵 Лиричная":  "audio/strings_hymn.mp3",
}

MUSIC_BY_CLEAN = { name.replace("🎵 ", ""): path for name, path in MUSIC.items() }
# --- Пользовательский трек ---
CUSTOM_MUSIC_KEY = "🎵 Свой трек"
ALLOWED_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".oga", ".opus"}

# --- RAW PROMPTS (без склейки; ровно как написали) ---
SCENE_PROMPTS = {
    "hug":         """Медленный равномерный dolly-in на людей, без резких зумов, стабильный кадр. Люди из стартового кадра начинают плавное сближение, поворачиваются друг к другу лицом, обнимаются, объятие длится, они покачиваются, руки меняют положение, головы касаются, но лица полностью не закрываются от камеры, мимика тёплая, движения сохраняются весь ролик. Фон оживает на протяжении всего видео. """,
    "kiss_cheek":  """Люди из стартового кадра начинают плавное сближение, поворачиваются друг к другу лицом, обнимаются, готовясь к медленному и очень нежному поцелую — щека к щеке, они чуть покачиваются, слегка прижимаются, позы и взгляды плавно меняются на протяжении всего видео, лица никогда полностью не перекрываются. Фон оживает на протяжении всего видео. Медленный равномерный dolly-in на людей, без резких зумов, стабильный кадр. """,
    "wave":        """Человек из стартового кадра дружелюбно машет рукой, меняя амплитуду и темп; корпус слегка разворачивается, вес перекатывается с ноги на ногу, возможен маленький шаг на месте; рука опускается и снова поднимается — движение непрерывное. Фон оживает на протяжении всего видео. Медленный равномерный dolly-in на персонажа, без резких зумов, стабильный кадр. """,
    "stairs":      """Человек медленно машет рукой около трех секунд, разворачивается спиной и уходит вверх по лестнице. Камера плавно следует, без резких зумов. В конце фигура мягко растворяется в светлой дымке. """,
}

# Ресэмплер под Pillow 10+
RESAMPLE = getattr(Image, "Resampling", Image)

# Зазоры и центры
MIN_GAP_PX       = 5     # было 20 — чуть безопаснее от «слипания»
IDEAL_GAP_FRAC   = 0.005   # было 0.05 — целевой зазор ~7% ширины
CENTER_BIAS_FRAC = 0.40   # было 0.42 — в старой раскладке уводит людей чуть к краям
# --- предупреждение о сильной разнице ширины пары (для всех форматов) ---
PAIR_WIDTH_WARN_RATIO = float(os.environ.get("PAIR_WIDTH_WARN_RATIO", "1.40"))  # 1.40 = +40% шире

# Максимальный допустимый апскейл
MAX_UPSCALE = float(os.environ.get("MAX_UPSCALE", "1.8"))

# Минимальные «видимые» высоты (анти-карлик), доля от высоты кадра H
MIN_VISIBLE_FRAC = {
    ("🧍 В рост", 1): 0.66,  # было 0.70
    ("🧍 В рост", 2): 0.64,  # было 0.70
    ("👨‍💼 По пояс", 1): 0.56,  # было 0.60
    ("👨‍💼 По пояс", 2): 0.54,  # было 0.60
    ("👨‍💼 По грудь", 1): 0.48,  # было 0.50
    ("👨‍💼 По грудь", 2): 0.46,  # было 0.50
}
def _min_frac_for(format_key: str, count_people: int) -> float:
    return MIN_VISIBLE_FRAC.get((format_key, count_people), 0.56)

# Целевые стартовые высоты (ещё чуть меньше, чем раньше)
TH_FULL_SINGLE   = 0.66   # было 0.70
TH_FULL_DOUBLE   = 0.66   # было 0.70
TH_WAIST_SINGLE  = 0.60   # было 0.60
TH_WAIST_DOUBLE  = 0.60   # было 0.60
TH_CHEST_SINGLE  = 0.50   # было 0.50
TH_CHEST_DOUBLE  = 0.50   # было 0.50

# Параметры LEAN-раскладки пары (можно через ENV переопределять)
LEAN_TARGET_VISIBLE_FRAC = float(os.environ.get("LEAN_TARGET_VISIBLE_FRAC", "0.76"))  # ↓ на 2 п.п.
LEAN_MAX_VISIBLE_FRAC    = float(os.environ.get("LEAN_MAX_VISIBLE_FRAC", "0.82"))     # ↓ лимит на рост
LEAN_MIN_GAP_FRAC = float(os.environ.get("LEAN_MIN_GAP_FRAC", "0.01"))  # минимум ~1% ширины
LEAN_CX_LEFT             = float(os.environ.get("LEAN_CX_LEFT", "0.34"))              # ← левее
LEAN_CX_RIGHT            = float(os.environ.get("LEAN_CX_RIGHT", "0.66"))             # → правее

# --- CHEST-UP (формат «По грудь») — виртуальный «пол» и туман ---
CHEST_VIRTUAL_FLOOR_FRAC = float(os.environ.get("CHEST_VIRTUAL_FLOOR_FRAC", "0.74"))  # где стоит «низ» фигур (доля H)
CHEST_FOG_START_FRAC     = float(os.environ.get("CHEST_FOG_START_FRAC", "0.62"))      # откуда начинается туман (доля H)
CHEST_FOG_MAX_ALPHA      = int(os.environ.get("CHEST_FOG_MAX_ALPHA", "210"))          # 0..255, плотность у самого низа
# мягкий тёплый туман (RGBA смешивание); можно поменять через env CHEST_FOG_COLOR="R,G,B"
CHEST_FOG_COLOR          = tuple(map(int, os.environ.get("CHEST_FOG_COLOR", "255,224,170").split(",")))

# --- WAIST-UP («По пояс») — виртуальный «пол» и туман ---
WAIST_VIRTUAL_FLOOR_FRAC = float(os.environ.get("WAIST_VIRTUAL_FLOOR_FRAC", "0.88"))
WAIST_FOG_START_FRAC     = float(os.environ.get("WAIST_FOG_START_FRAC", "0.80"))
WAIST_FOG_MAX_ALPHA      = int(os.environ.get("WAIST_FOG_MAX_ALPHA", "180"))

# Минимальная доля высоты группы (для «подростить», если совсем мелко)
MIN_SINGLE_FRAC = {
    "В рост":  0.66,
    "По пояс": 0.56,
    "По грудь":0.48,
}
MIN_PAIR_FRAC = {
    "В рост":  0.64,
    "По пояс": 0.54,
    "По грудь":0.46,
}

# Мягкий предел апскейла при доводке (чтобы внезапно не «раздуть»)
PAIR_UPSCALE_CAP   = 1.10   # было 1.22
SINGLE_UPSCALE_CAP = 1.12   # было 1.25

def _bg_layout_presets(bg_path: str):
    name = os.path.basename(str(bg_path)).lower()
    # по умолчанию – широкая полоса
    presets = dict(center_frac=0.50, band_frac=0.68, top_headroom_min=0.05, top_headroom_max=0.13)

    if "stairs" in name:
        presets["band_frac"] = 0.72
    elif "gates" in name:
        presets["band_frac"] = 0.80   # было 0.44 → из-за этого всех сжимало
    return presets

# ------------------------- PROMPT BUILDER (per scene) -------------------------

def _people_count_by_kind(kind: str) -> int:
    """
    Кол-во людей по типу сцены.
    Все одиночные сюжеты перечисляем явно.
    """
    k = (kind or "").lower()
    SINGLE_KINDS = {"wave", "stairs"}   # ← тут ключевая правка
    return 1 if k in SINGLE_KINDS else 2

# ---------- СТЕЙТ ----------
def new_state():
    return {
        "scenes": [],          # список выбранных названий сцен (как раньше)
        "format": None,
        "bg": None,
        "music": None,

        # --- новое для мультисцен ---
        "scene_idx": 0,        # индекс текущего сюжета, для которого собираем фото/рендерим
        "scene_jobs": [],      # список dict по каждому сюжету: {scene_key, people, photos[], start_frame, duration, prompt, video_path}

        # обратная совместимость (не используем в новом потоке, но пусть будет)
        "photos": [],

        "ready": False,
        "support": False,
        "await_approval": None,  # сюда кладём данные ТОЛЬКО по текущему сюжету (включая scene_idx)
        "await_custom_bg": False,   # ждём загрузку пользовательского фона
        "bg_custom_path": None,     # путь к пользовательскому фону текущей сессии
        "await_custom_music": False,   # ждём загрузку своего трека?
        "custom_music_path": None,     # путь к пользовательскому треку (на диске)
        # --- ЛЕГАЛ ---
        "offer_accepted": False,
        "offer_accepted_ver": None,
        # --- титры ---
        "titles_mode": "none",        # 'none' | 'custom'
        "titles_fio": None,
        "titles_dates": None,
        "titles_text": None,
        "await_titles_field": None,   # 'fio' | 'dates' | 'mem' или None
        "await_payment": False,   # ждём оплату перед стартом генерации
        "payment_confirmed": False,  # оплата прошла (заполним позже, когда подключим оплату)
    }

users = {}  # uid -> state
IN_RENDER = set()  # юзеры, у кого идет отправка в рендер (защита от двойного клика)
# Буфер для альбомов (несколько фото, пришедших одним медиа-группой)
PENDING_ALBUMS = {}  # media_group_id -> {"uid": int, "scene_idx": int, "need": int, "paths": list[str]}  # важно: теперь на конкретный сюжет

# ---------- КЛАВИАТУРЫ ----------
def available_scene_keys(format_key: str | None) -> list[str]:
    # если формат не "В рост" — убираем все сцены с kind == "stairs"
    keys = []
    for name, meta in SCENES.items():
        if format_key and "В рост" not in format_key and meta.get("kind") == "stairs":
            continue
        keys.append(name)
    return keys

def _is_paid_scene(scene_key: str) -> bool:
    """Платный ли сюжет? (сейчас: всё, что не 'Объятия 5с', и с длительностью 10 сек)"""
    if _is_free_hugs(scene_key):
        return False
    meta = SCENES.get(scene_key, {})
    return int(meta.get("duration", 0)) >= 10

def calc_order_price(st: dict) -> tuple[int, dict]:
    """
    Возвращает (total_rub, breakdown) где breakdown:
    {
      "scenes": [(name, price_rub), ...],
      "options": [("Свой фон", price), ("Своя музыка", price), ("Титры", price)]
    }
    """
    total = 0
    br = {"scenes": [], "options": []}

    # Сцены
    for name in st.get("scenes", []):
        if _is_paid_scene(name):
            p = SCENE_PRICE_10S
        else:
            p = 0
        br["scenes"].append((name, p))
        total += p

    # Опции (добавляются ко всему ролику)
    # свой фон
    if st.get("bg") == CUSTOM_BG_KEY and st.get("bg_custom_path"):
        br["options"].append(("Свой фон", OPT_PRICE_CUSTOM_BG))
        total += OPT_PRICE_CUSTOM_BG
    # своя музыка
    if st.get("music") == CUSTOM_MUSIC_KEY and st.get("custom_music_path"):
        br["options"].append(("Своя музыка", OPT_PRICE_CUSTOM_MUSIC))
        total += OPT_PRICE_CUSTOM_MUSIC
    # кастомные титры
    if st.get("titles_mode") == "custom":
        br["options"].append(("Финальные титры", OPT_PRICE_TITLES))
        total += OPT_PRICE_TITLES

    return total, br

def stars_amount_for_state(st: dict) -> tuple[int, int]:
    k = float(os.environ.get("STARS_PER_RUB", "0.5"))
    total_rub, _ = calc_order_price(st)
    if total_rub <= 0:
        return 0, 0
    stars = int(math.ceil(total_rub * k))
    return stars, total_rub

def format_quote_text(total: int, br: dict) -> str:
    lines = []
    lines.append("💳 <b>Итог к оплате</b>\n")
    if br["scenes"]:
        lines.append("<b>Сюжеты:</b>")
        for name, price in br["scenes"]:
            price_str = f"{price} ₽" if price > 0 else "бесплатно"
            lines.append(f"• {name} — <b>{price_str}</b>")
    else:
        lines.append("• Сюжеты: не выбраны")

    if br["options"]:
        lines.append("\n<b>Опции:</b>")
        for label, price in br["options"]:
            lines.append(f"• {label} — +{price} ₽")
    else:
        lines.append("\nОпции: нет")

    lines.append(f"\n<b>Итого: {total} ₽</b>")
    # Пояснение для бесплатного сюжета
    lines.append("\n<i>Примечание: опции добавляются к итоговой цене даже при бесплатном сюжете 5 сек.</i>")
    return "\n".join(lines)

def kb_payment():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("💳 Оплатить", callback_data="pay_now"),
        telebot.types.InlineKeyboardButton("🏠 В главное меню", callback_data="go_home"),
    )
    return kb

def send_payment_quote(uid: int, st: dict):
    total, br = calc_order_price(st)
    text = format_quote_text(total, br)

    if total <= 0:
        # Просто показываем итог и сразу продолжаем
        bot.send_message(uid, text)  # без кнопок
        bot.send_message(uid, "Стоимость 0 ₽ — оплата не требуется. Продолжаем ✅")
        st["await_payment"] = False
        st["payment_confirmed"] = True
        _after_payment_continue(uid, st)   # тот же путь, что и после успешной оплаты
        return

    try:
        bot.send_message(uid, text, reply_markup=kb_payment())
    except Exception as e:
        print(f"[PAY] send quote error: {e}")

def kb_payment_methods():
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton("⭐️ Оплата Stars Telegram", callback_data="pay_stars"),
        telebot.types.InlineKeyboardButton("💳 Оплата картой / СБП",    callback_data="pay_tochka"),
    )
    kb.add(telebot.types.InlineKeyboardButton("🏠 В главное меню", callback_data="go_home"))
    return kb

def kb_tochka_link(op_id: str, url: str):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(telebot.types.InlineKeyboardButton("🔗 Открыть платёж", url=url))
    kb.add(telebot.types.InlineKeyboardButton("🔁 Я оплатил(а) — проверить", callback_data=f"checkpay_{op_id}"))
    kb.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="pay_cancel"))
    return kb

def tochka_create_payment_link(amount_rub: int | float, purpose: str) -> tuple[str, str]:
    """
    Возвращает (operation_id, payment_link).
    Бросает Exception с текстом ответа при ошибке.
    """
    assert TOCHKA_JWT, "TOCHKA_JWT не задан"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOCHKA_JWT}",
    }
    payload = {
        "Data": {
            "merchantId":   TOCHKA_MERCHANT_ID,
            "customerCode": TOCHKA_CUSTOMER_CODE,
            "amount":       f"{float(amount_rub):.2f}",
            "purpose":      purpose[:255],
            "redirectUrl":      TOCHKA_OK_URL,
            "failRedirectUrl":  os.environ.get("TOCHKA_FAIL_URL", TOCHKA_OK_URL),
            "paymentMode":  ["card", "sbp"],
            "ttl":          10080
        }
    }
    r = requests.post(f"{TOCHKA_API}/payments", headers=headers, json=payload, timeout=60)
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code != 200:
        raise Exception(f"Create payment {r.status_code}: {getattr(r,'text','')}")

    # у Точки бывает два варианта структуры — берём из Data
    D = data.get("Data") or {}
    op_id = D.get("operationId") or D.get("operationID") or ""
    link  = D.get("paymentLink") or ""
    if not (op_id and link):
        raise Exception(f"Create payment: неполный ответ: {data}")
    return op_id, link

def tochka_get_payment_status(op_id: str) -> dict:
    """Возвращает JSON-ответ GET /payments/{op_id}."""
    headers = {"Accept":"application/json", "Authorization": f"Bearer {TOCHKA_JWT}"}
    r = requests.get(f"{TOCHKA_API}/payments/{op_id}", headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()

def _start_auto_check_payment(uid: int, op_id: str, period_sec: int = 10, max_checks: int = 12):
    """
    Автопроверка статуса оплаты Точки:
      - каждые period_sec секунд;
      - не больше max_checks раз (по умолчанию ~120 сек);
      - останавливается, если пользователь отменил оплату или сменился op_id.
    """
    def _worker():
        try:
            for i in range(max_checks):
                # пользователь отменил — выходим
                st = users.setdefault(uid, new_state())
                if not st.get("await_payment"):
                    return
                if st.get("payment_op_id") != op_id:
                    return

                try:
                    resp = tochka_get_payment_status(op_id)
                except Exception as e:
                    print(f"[PAY] auto-check err: {e}")
                    time.sleep(period_sec)
                    continue

                if _is_paid_status(resp):
                    st["payment_confirmed"] = True
                    st["await_payment"] = False
                    bot.send_message(uid, "✅ Оплата получена. Запускаю генерацию.")
                    _after_payment_continue(uid, st)
                    return

                time.sleep(period_sec)
        except Exception as e:
            print(f"[PAY] auto-check thread crash: {e}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def _is_paid_status(resp_json: dict) -> bool:
    """
    true, если оплата прошла.
    Точка может отдавать:
      Data.Operation[0].status == 'APPROVED'|'COMPLETED'
      или Data.status == 'COMPLETED'
    """
    D = resp_json.get("Data") or {}
    op = None
    if isinstance(D.get("Operation"), list) and D["Operation"]:
        op = D["Operation"][0]
    st = (op or D).get("status") or ""
    return st.upper() in {"APPROVED", "COMPLETED"}

def kb_scenes(format_key: str | None = None):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)

    # Список доступных сцен с учётом формата
    scene_keys = available_scene_keys(format_key)
    scene_buttons = [telebot.types.KeyboardButton(k) for k in scene_keys]
    if scene_buttons:
        kb.add(*scene_buttons)

    # служебные — отдельными рядами
    kb.add(
        telebot.types.KeyboardButton("✅ Выбрано, дальше"),
        telebot.types.KeyboardButton("🔁 Сбросить выбор сюжетов"),
    )
    kb.add(telebot.types.KeyboardButton(BTN_GO_HOME))
    return kb

def kb_formats():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    kb.add(*[telebot.types.KeyboardButton(k) for k in FORMATS.keys()])
    kb.add(telebot.types.KeyboardButton(BTN_GO_HOME))
    return kb

def kb_backgrounds():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    for name, path in BACKGROUNDS.items():
        clean_name = name.split(" ", 1)[1] if " " in name else name
        preview_btn = telebot.types.InlineKeyboardButton(
            f"👀 Посмотреть: {clean_name}", callback_data=f"preview_bg_{clean_name}"
        )
        select_btn = telebot.types.InlineKeyboardButton(
            f"✅ {clean_name}", callback_data=f"select_bg_{clean_name}"
        )
        kb.add(preview_btn, select_btn)
    home_btn = telebot.types.InlineKeyboardButton(
        "🏠 В главное меню", callback_data="go_home"
    )
    kb.add(home_btn)
    return kb

def kb_music():
    """Inline-клавиатура для выбора музыки с возможностью прослушивания и загрузки своего трека"""
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)

    for name, path in MUSIC.items():
        clean_name = name.replace("🎵 ", "")
        listen_btn = telebot.types.InlineKeyboardButton(
            f"🎧 : {clean_name}", callback_data=f"listen_{clean_name}"
        )
        select_btn = telebot.types.InlineKeyboardButton(
            f"✅ : {clean_name}", callback_data=f"select_music_{clean_name}"
        )
        kb.add(listen_btn, select_btn)

    no_music_btn = telebot.types.InlineKeyboardButton(
        "🔇 Без музыки", callback_data="select_music_none"
    )
    upload_btn = telebot.types.InlineKeyboardButton(
        "⬆️ Свой трек 50₽", callback_data="upload_music"
    )
    kb.add(no_music_btn, upload_btn)

    home_btn = telebot.types.InlineKeyboardButton(
        "🏠 В главное меню", callback_data="go_home"
    )
    kb.add(home_btn)

    return kb

def kb_titles():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("Без титров", callback_data="titles_none"),
        telebot.types.InlineKeyboardButton("Свои титры 50₽", callback_data="titles_custom"),
    )
    kb.add(telebot.types.InlineKeyboardButton("🏠 В главное меню", callback_data="go_home"))
    return kb

def kb_backgrounds_inline():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    for name in BG_FILES.keys():
        clean = name.split(" ", 1)[1] if " " in name else name
        kb.add(
            telebot.types.InlineKeyboardButton(f"👁️ фон: {clean}", callback_data=f"preview_bg_{clean}"),
            telebot.types.InlineKeyboardButton(f"✅ фон: {clean}",    callback_data=f"select_bg_{clean}")
        )
    kb.add(
        telebot.types.InlineKeyboardButton("🖼 Свой фон 50₽", callback_data="upload_bg"),
        telebot.types.InlineKeyboardButton("🏠 В главное меню",     callback_data="go_home"),
    )
    return kb

def kb_start_approval():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("✅ Согласовать", callback_data="approve_start"),
        telebot.types.InlineKeyboardButton("🔁 Заменить фото",          callback_data="reject_start"),
    )
    return kb

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def alpha_metrics(img: Image.Image, thr: int = 20):
    """
    Возвращает (bbox, y_bottom) по непрозрачным пикселям альфа-канала.
    bbox: (x0, y0, x1, y1) в координатах изображения
    y_bottom: индекс нижней строки содержимого (int)
    """
    a = img.split()[-1]
    arr = np.asarray(a, dtype=np.uint8)
    ys, xs = np.where(arr >= thr)
    if ys.size == 0:
        b = img.getbbox() or (0, 0, img.width, img.height)
        return b, b[3] - 1
    x0, y0 = int(xs.min()), int(ys.min())
    x1, y1 = int(xs.max()) + 1, int(ys.max()) + 1
    return (x0, y0, x1, y1), (y1 - 1)

def _save_layout_debug(canvas_rgba: Image.Image, metrics: dict, base_id: str):
    """
    Сохраняет:
      - renders/temp/metrics_<base_id>.json — метрики компоновки
      - renders/temp/annot_<base_id>.png    — аннотированное превью с рамками
    """
    try:
        os.makedirs("renders/temp", exist_ok=True)
    except Exception:
        pass

    # 1) JSON
    try:
        mpath = f"renders/temp/metrics_{base_id}.json"
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        print(f"[DEBUG] metrics -> {mpath}")
    except Exception as e:
        print(f"[DEBUG] metrics save error: {e}")

    # 2) Аннотированная картинка
    try:
        im = canvas_rgba.convert("RGB")
        draw = ImageDraw.Draw(im)
        font = None
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        except Exception:
            font = ImageFont.load_default()

        # Рамки и подписи
        colors = {"L": (46, 204, 113), "R": (52, 152, 219)}  # зелёный/синий
        for side in ("L", "R"):
            if side not in metrics: 
                continue
            r = metrics[side]["rect_abs"]  # [x0,y0,x1,y1]
            c = colors[side]
            # рамка
            draw.rectangle(r, outline=c, width=3)
            # подпись
            label = (f"{side}: h={metrics[side]['height_px']} "
                     f"({int(round(metrics[side]['height_frac']*100))}% H), "
                     f"w={metrics[side]['width_px']}, "
                     f"cx={int(round(metrics[side]['center_x_frac']*100))}%, "
                     f"scale≈{metrics[side]['scale']:.2f}")
            tx, ty = r[0] + 4, max(4, r[1] - 18)
            draw.rectangle([tx-2, ty-2, tx+draw.textlength(label, font=font)+6, ty+18], fill=(0,0,0,128))
            draw.text((tx, ty), label, fill=(255,255,255), font=font)

            # отметка «пол»
            fy = metrics[side].get("floor_y")
            if isinstance(fy, int):
                draw.line([(r[0], fy), (r[2], fy)], fill=c, width=2)

        # Зазор между людьми
        gap = metrics.get("gap_px")
        if gap is not None:
            text = f"gap={gap}px ({int(round(metrics.get('gap_frac',0)*100))}% W)"
            draw.rectangle([10, 10, 10+draw.textlength(text, font=font)+12, 10+22], fill=(0,0,0,128))
            draw.text((16, 12), text, fill=(255,255,255), font=font)

        apath = f"renders/temp/annot_{base_id}.png"
        im.save(apath, "PNG")
        print(f"[DEBUG] annot -> {apath}")
    except Exception as e:
        print(f"[DEBUG] annot save error: {e}")

# --- Заглушка под старые вызовы ассистента (удалим позже вместе с ними) ---
def _is_minor_only(reasons: list[str] | None) -> bool:
    """Ассистент отключён: минор/мажор причины не анализируем."""
    return False

def validate_photo(path: str) -> tuple[bool, list[str]]:
    """
    Мягкая валидация фото.
    Возвращает (ok, warnings). ok=False — очень маленькое фото, но пайплайн не блокируем.
    """
    warns = []
    ok = True
    try:
        im = Image.open(path)
        # Нормализуем ориентацию по EXIF (если телефон переворачивал)
        try:
            from PIL import ImageOps
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass
    except Exception as e:
        return False, [f"не удалось открыть файл ({e})"]

    w, h = im.size
    min_dim = min(w, h)

    # 1) Размер/разрешение
    if min_dim < 300:
        ok = False
        warns.append(f"очень маленькое разрешение ({w}×{h}) — результат может исказиться")
    elif min_dim < 600:
        warns.append(f"низкое разрешение ({w}×{h}) — желательно ≥ 800px по меньшей стороне")

    # 2) Ориентация (для портретов лучше вертикальная)
    ratio = w / h if h else 1.0
    if ratio > 0.9:
        warns.append("фото не вертикальное — портрет обычно лучше выглядит в вертикали")

    # 3) Темнота/экспозиция (очень грубо)
    gray = im.convert("L")
    arr = np.asarray(gray, dtype=np.float32)
    mean = float(arr.mean())
    if mean < 55:
        warns.append("фото тёмное — попробуйте более светлое/контрастное")

    # 4) Размытость (приблизительно через «края»)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    earr = np.asarray(edges, dtype=np.float32)
    sharpness = float(earr.std())
    if sharpness < 8:
        warns.append("возможная размытость/шум — контуры слабые")

    return ok, warns

def _visible_bbox_height(img: Image.Image) -> int:
    b = img.getbbox() or (0, 0, img.width, img.height)
    return max(1, b[3] - b[1])

def smart_cutout(img_rgba: Image.Image) -> Image.Image:
    """
    Вырезка человека:
      1) пробуем портретную модель, иначе базовую;
      2) если силуэт слишком мал — пробуем ISNet;
      3) убираем «ореол» и чуть смягчаем край.
    """
    def _run(session):
        out = remove(img_rgba, session=session, post_process_mask=True)
        if isinstance(out, (bytes, bytearray)):
            out = Image.open(io.BytesIO(out)).convert("RGBA")
        else:
            out = out.convert("RGBA")
        return out

    # 1) Портретная модель → fallback
    try:
        cut = _run(RMBG_HUMAN)
    except Exception:
        cut = _run(RMBG_SESSION)

    # 2) Если силуэт подозрительно маленький — пробуем ISNet
    try:
        bb = cut.getbbox() or (0, 0, cut.width, cut.height)
        area = (bb[2] - bb[0]) * (bb[3] - bb[1])
        if area < 0.12 * cut.width * cut.height:
            try:
                alt = _run(RMBG_ISNET)
                bb2 = alt.getbbox() or (0, 0, alt.width, alt.height)
                area2 = (bb2[2] - bb2[0]) * (bb2[3] - bb2[1])
                if area2 > area:
                    cut = alt
            except Exception:
                pass
    except Exception:
        pass

    # 3) Рафинирование маски: чуть «поджать» и дать перо
    a = cut.split()[-1]
    a = a.filter(ImageFilter.MinFilter(3))       # ~1px эрозия — убираем ореол
    a = a.filter(ImageFilter.GaussianBlur(1.2))  # мягкое перо ~1–2px
    cut.putalpha(a)
    return cut

def add_bottom_fog(canvas_rgba: Image.Image, start_y: int, color=(255, 224, 170), max_alpha=210):
    """
    Мягкий туман снизу (градиентная альфа от низа к start_y).
    canvas_rgba: RGBA 720x1280
    start_y: пиксельная координата, с которой туман исчезает (выше — 0)
    """
    W, H = canvas_rgba.width, canvas_rgba.height
    start_y = max(0, min(H, int(start_y)))
    if start_y >= H:
        return
    fog = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(fog)
    # рисуем вертикальный альфа-градиент
    for y in range(start_y, H):
        t = (y - start_y) / max(1, (H - start_y))  # 0..1
        a = int(round(max_alpha * t))              # плавное нарастание к низу
        draw.line([(0, y), (W, y)], fill=(color[0], color[1], color[2], a))
    canvas_rgba.alpha_composite(fog)

# ---------- RUNWAY ----------
RUNWAY_API = "https://api.dev.runwayml.com/v1"
HEADERS = {
    "Authorization": f"Bearer {RUNWAY_KEY}",
    "X-Runway-Version": "2024-11-06",
    "Content-Type": "application/json",
}

def encode_image_datauri(path: str) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    ext = path.lower().split(".")[-1]
    mime = "image/jpeg" if ext in ["jpg","jpeg"] else "image/png"
    return f"data:{mime};base64,{b64}"

def ensure_jpeg_copy(path: str, quality: int = 88) -> str:
    """
    Делает JPEG-копию файла (оптимизированную) и возвращает путь к .jpg.
    """
    im = Image.open(path).convert("RGB")
    out = os.path.splitext(path)[0] + ".jpg"
    im.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    try:
        os.sync()  # не у всех ОС есть, ок если свалится
    except Exception:
        pass
    return out

def encode_image_as_jpeg_datauri(path: str, quality: int = 88) -> str:
    """
    Принудительно кодирует изображение в JPEG (RGB) и возвращает dataURI.
    Это уменьшает размер по сравнению с PNG и стабильнее проходит в Runway.
    """
    im = Image.open(path).convert("RGB")
    bio = io.BytesIO()
    im.save(bio, format="JPEG", quality=quality, optimize=True, progressive=True)
    b64 = base64.b64encode(bio.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"

def cut_foreground_to_png(in_path: str) -> str:
    """Вырезает фон из JPG/PNG и сохраняет PNG с альфой."""
    with open(in_path, "rb") as f:
        raw = f.read()
    out = remove(raw, session=RMBG_SESSION)
    out_path = os.path.splitext(in_path)[0] + "_cut.png"
    with open(out_path, "wb") as f:
        f.write(out)
    return out_path

def _to_jpeg_copy(src_path: str, quality: int = 88) -> str:
    im = Image.open(src_path).convert("RGB")
    out_path = os.path.join("uploads", f"startframe_{uuid.uuid4().hex}.jpg")
    im.save(out_path, "JPEG", quality=quality, optimize=True, progressive=True)
    return out_path

def ensure_runway_datauri_under_limit(path: str, limit: int = 5_000_000) -> tuple[str, str]:
    data = encode_image_datauri(path)
    if len(data) <= limit:
        return data, path

    last_path = path
    for q in (88, 80, 72):
        try:
            jpg = _to_jpeg_copy(path, quality=q)
            last_path = jpg
            data = encode_image_datauri(jpg)
            if len(data) <= limit:
                print(f"[Runway] using JPEG q={q}, data_uri={len(data)} bytes")
                return data, jpg
        except Exception as e:
            print(f"[Runway] jpeg fallback q={q} failed: {e}")

    print(f"[Runway] still heavy after JPEG attempts, length={len(data)}")
    return data, last_path

def _post_runway(payload: dict) -> dict | None:
    try:
        _pl = ""
        try:
            _pl = (payload.get("promptText") or payload.get("prompt") or "") if isinstance(payload, dict) else ""
        except Exception:
            pass

        model = payload.get("model")
        ratio = payload.get("ratio") or payload.get("aspect_ratio")
        dur   = payload.get("duration")

        msg = f"[Runway] model={model} dur={dur} ratio={ratio}"
        if _pl:
            msg += f" prompt[{len(_pl)}]={_pl.replace(chr(10),' ')}"
        print(msg)

        if MF_DEBUG:
            try:
                os.makedirs("renders/temp", exist_ok=True)
                preview = {
                    "model": model,
                    "duration": dur,
                    "ratio": ratio,
                    "prompt_len": len(_pl),
                    "image_data_uri_len": len(payload.get("promptImage") or payload.get("image") or ""),
                }
                with open(os.path.join("renders/temp", f"runway_payload_{int(time.time())}.json"), "w", encoding="utf-8") as f:
                    json.dump(preview, f, ensure_ascii=False, indent=2)
                print("[Runway] payload preview saved")
            except Exception as _e:
                print(f"[Runway] payload preview save err: {_e}")

        # Отправка в Runway (заглушка отключена)
        r = requests.post(f"{RUNWAY_API}/image_to_video", headers=HEADERS, json=payload, timeout=60)
        if r.status_code == 200:
            return r.json()
        print(f"[Runway {r.status_code}] {r.text}")
        return None
    except requests.RequestException as e:
        print(f"[Runway transport error] {e}")
        return None

def runway_start(prompt_image_datauri: str, prompt_text: str, duration: int):
    """
    Порядок попыток:
    1) gen4_turbo + promptImage/promptText + ratio (текущая схема этого API)
    2) gen4_turbo + image/prompt + aspect_ratio (альтернативная)
    3) gen3a_turbo + image/prompt + aspect_ratio (запасной)
    """
    variants = [
        {
            "model": "gen4_turbo",
            "promptImage": prompt_image_datauri,   # <-- ОБЯЗАТЕЛЬНО
            "promptText":  prompt_text,
            "ratio": "720:1280",
            "duration": int(duration),
        },
        {
            "model": "gen4_turbo",
            "image": prompt_image_datauri,
            "prompt": prompt_text,
            "aspect_ratio": "9:16",
            "duration": int(duration),
        },
        {
            "model": "gen3a_turbo",
            "image": prompt_image_datauri,
            "prompt": prompt_text,
            "aspect_ratio": "9:16",
            "duration": int(duration),
        },
    ]

    last_keys = ""
    for payload in variants:
        resp = _post_runway(payload)
        if resp:
            return resp
        last_keys = f"{list(payload.keys())}"

    raise RuntimeError(f"Runway returned 400/4xx for all variants (payload={last_keys}). Check logs above.")

def runway_poll(task_id: str, timeout_sec=300, every=5):
    """Опрашивает статус задачи Runway с обработкой ошибок сети."""
    start = time.time()
    attempts = 0
    max_attempts = 10

    while True:
        attempts += 1
        try:
            print(f"[Runway] Polling task {task_id} (attempt {attempts}/{max_attempts})")
            rr = requests.get(f"{RUNWAY_API}/tasks/{task_id}", headers=HEADERS, timeout=30)
            rr.raise_for_status()
            data = rr.json()
            st = data.get("status")
            print(f"[Runway] Status: {st}")

            if st in ("SUCCEEDED","FAILED","ERROR","CANCELED"):
                return data

            if time.time() - start > timeout_sec:
                print(f"[Runway] Timeout after {timeout_sec}s")
                return {"status":"TIMEOUT","raw":data}

            time.sleep(every)

        except requests.exceptions.Timeout:
            print(f"[Runway] Request timeout (attempt {attempts})")
            if attempts >= max_attempts:
                return {"status":"NETWORK_ERROR","error":"Too many timeouts"}
            time.sleep(10)

        except requests.exceptions.RequestException as e:
            print(f"[Runway] Network error (attempt {attempts}): {e}")
            if attempts >= max_attempts:
                return {"status":"NETWORK_ERROR","error":str(e)}
            time.sleep(10)

        except Exception as e:
            print(f"[Runway] Unexpected error (attempt {attempts}): {e}")
            if attempts >= max_attempts:
                return {"status":"ERROR","error":str(e)}
            time.sleep(10)

def download(url: str, save_path: str):
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk: f.write(chunk)
    return save_path

def _video_duration_sec(path: str) -> float:
    """Возвращает длительность видео через ffprobe (секунды)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", path],
            capture_output=True, text=True, check=True
        )
        return float(r.stdout.strip() or "0")
    except Exception:
        return 0.0

def _xfade_two(in1: str, in2: str, out_path: str, fade_sec: float = 0.7):
    """Сшивает два видео с кроссфейдом (без аудио)."""
    d1 = _video_duration_sec(in1)
    offset = max(0.0, d1 - fade_sec)
    # Единый fps/профиль для стабильности
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", in1, "-i", in2,
        "-filter_complex",
        f"[0:v]fps=24,format=yuv420p[v0];[1:v]fps=24,format=yuv420p[v1];"
        f"[v0][v1]xfade=transition=fade:duration={fade_sec}:offset={offset},format=yuv420p[v]",
        "-map", "[v]",
        "-an",
        "-r", "24",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        out_path
    ], tag="xfade", out_hint=out_path)

def _merge_with_fades(video_paths: List[str], fade_sec: float = 0.7) -> str:
    """Чейним кроссфейды попарно: (((v1 xfade v2) xfade v3) ...). Возвращает путь к итоговому ролику."""
    assert len(video_paths) >= 2
    tmp_dir = "renders/temp"
    os.makedirs(tmp_dir, exist_ok=True)
    acc = video_paths[0]
    for i, nxt in enumerate(video_paths[1:], start=1):
        out_i = os.path.join(tmp_dir, f"xfade_{i}_{uuid.uuid4().hex}.mp4")
        _xfade_two(acc, nxt, out_i, fade_sec=fade_sec)
        # следующая итерация будет склеивать out_i с следующим
        acc = out_i
    return acc

def _ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return "ffmpeg"

def _run_ffmpeg(cmd: list[str], tag: str, out_hint: str | None = None):
    """Запускает ffmpeg, пишет stdout/stderr в файлы и печатает хвост ошибки.
    """
    try:
        os.makedirs("renders/temp", exist_ok=True)
    except Exception:
        pass
    log_base = f"renders/temp/ffmpeg_{tag}_{int(time.time())}_{uuid.uuid4().hex}"
    so = f"{log_base}.out.log"
    se = f"{log_base}.err.log"
    try:
        # заменяем первый элемент на конкретный бинарник ffmpeg
        if cmd and os.path.basename(cmd[0]) == "ffmpeg":
            cmd[0] = _ffmpeg_bin()
        res = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with open(so, "wb") as f:
            f.write(res.stdout or b"")
        with open(se, "wb") as f:
            f.write(res.stderr or b"")
        return True
    except subprocess.CalledProcessError as e:
        try:
            with open(so, "wb") as f:
                f.write(e.stdout or b"")
            with open(se, "wb") as f:
                f.write(e.stderr or b"")
        except Exception:
            pass
        tail = (e.stderr or b"").decode("utf-8", "ignore").splitlines()[-20:]
        print(f"[FFMPEG][{tag}] failed. See logs: {so} / {se}")
        if out_hint:
            print(f"[FFMPEG][{tag}] output: {out_hint}")
        for line in tail:
            print(f"[FFMPEG][{tag}] {line}")
        raise

def apply_fullscreen_watermark(in_video: str, out_video: str, wm_path: str,
                               mode: str = FREE_HUGS_WM_MODE,
                               alpha: float = FREE_HUGS_WM_ALPHA):
    """
    Накладывает «большой» полупрозрачный водяной знак на видео.
    mode='single' — один крупный по центру; mode='grid' — сетка маленьких.
    """
    if not os.path.isfile(wm_path):
        raise FileNotFoundError(f"watermark file not found: {wm_path}")

    m = (mode or "").lower()
    if m == "grid":
        cols = max(1, FREE_HUGS_WM_GRID_COLS)
        rows = max(1, FREE_HUGS_WM_GRID_ROWS)
        margin = max(0, FREE_HUGS_WM_GRID_MARGIN)
        N = cols * rows

        # 1) приводим логотип к RGBA и задаём прозрачность
        # 2) scale2ref — масштабируем логотип относительно основного видео:
        #    ширина ячейки = main_w/cols - 2*margin
        # 3) клонируем логотип split'ом и раскладываем overlay'ями по сетке
        labels = "".join(f"[w{i}]" for i in range(N))
        fc = (
        f"[1:v]format=rgba,colorchannelmixer=aa={alpha}[wm0];"
        f"[wm0][0:v]scale2ref=w='(main_w/{cols})-({2*margin})':h=-1[wm][base];"
        f"[wm]split={N}{labels};"
        )
        prev = "[base]"
        idx = 0
        for r in range(rows):
            for c in range(cols):
                x = f"(main_w/{cols})*{c} + ((main_w/{cols})-w)/2"
                y = f"(main_h/{rows})*{r} + ((main_h/{rows})-h)/2"
                nxt = "[v]" if (idx == N - 1) else f"[t{idx}]"
                fc += f"{prev}[w{idx}]overlay=x='{x}':y='{y}':format=auto{nxt};"
                prev = nxt
                idx += 1
    else:
        # single: один крупный логотип по центру; масштаб и поворот настраиваемые
        scale = max(0.2, min(1.5, FREE_HUGS_WM_SCALE))
        rot   = float(FREE_HUGS_WM_ROTATE)

        if abs(rot) > 0.01:
            fc = (
                f"[1:v]format=rgba,colorchannelmixer=aa={alpha}[wm0];"
                f"[wm0][0:v]scale2ref=w='main_w*{scale}':h=-1[wm][base];"
                f"[wm]rotate={rot}*PI/180:c=none:ow='rotw(iw)':oh='roth(ih)'[wmr];"
                f"[base][wmr]overlay=x='(main_w-w)/2':y='(main_h-h)/2':format=auto[v]"
            )
        else:
            fc = (
                f"[1:v]format=rgba,colorchannelmixer=aa={alpha}[wm0];"
                f"[wm0][0:v]scale2ref=w='main_w*{scale}':h=-1[wm][base];"
                f"[base][wm]overlay=x='(main_w-w)/2':y='(main_h-h)/2':format=auto[v]"
            )

    cmd = [
        "ffmpeg", "-y",
        "-i", in_video,
        "-loop", "1", "-i", wm_path,
        "-filter_complex", fc,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        out_video
    ]
    _run_ffmpeg(cmd, tag="wm_fullscreen", out_hint=out_video)
    return out_video

def _log_fail(uid: int, reason: str, payload: dict | None = None, response: dict | None = None):
    try:
        os.makedirs("renders/temp", exist_ok=True)
        path = os.path.join("renders/temp", f"fail_{uid}_{int(time.time())}_{uuid.uuid4().hex}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "ts": datetime.now(timezone.utc).isoformat(),
                "uid": uid,
                "reason": reason,
                "payload": payload or {},
                "response": response or {}
            }, f, ensure_ascii=False, indent=2)
        print(f"[FAILLOG] {reason} -> {path}")
        # если задан ADMIN_CHAT_ID — шлём короткое уведомление
        if ADMIN_CHAT_ID:
            try:
                bot.send_message(int(ADMIN_CHAT_ID), f"⚠️ FAIL {reason} (uid={uid})\n{os.path.basename(path)} сохранён.")
            except Exception:
                pass
    except Exception as e:
        print(f"[FAILLOG] write error: {e}")

def oai_gate_check(start_frame_path: str, base_prompt: str, meta: dict, timeout_sec: int = 120) -> dict | None:
    """
    Ассистент отключён: ничего не проверяем и ничего не добавляем.
    Возвращаем None, чтобы остальной код шёл по «без ассистента» ветке.
    """
    return None

# ---------- ВЫРЕЗАНИЕ И СТАРТ-КАДР ----------
def cutout(path: str) -> Image.Image:
    im = Image.open(path).convert("RGBA")
    cut = remove(im, session=RMBG_SESSION)  # важное: используем общую сессию
    # rembg может вернуть bytes — нормализуем к PIL.Image
    if isinstance(cut, (bytes, bytearray)):
        cut = Image.open(io.BytesIO(cut)).convert("RGBA")
    return cut

def _resize_fit_center(img: Image.Image, W: int, H: int) -> Image.Image:
    """Вписать картинку в холст W×H с сохранением пропорций и кропом по центру."""
    wr, hr = W / img.width, H / img.height
    scale = max(wr, hr)
    new = img.resize((int(img.width * scale), int(img.height * scale)), RESAMPLE.LANCZOS)
    x = (new.width - W) // 2
    y = (new.height - H) // 2
    return new.crop((x, y, x + W, y + H))

def make_start_frame(photo_paths: List[str], framing_key: str, bg_file: str, layout: dict | None = None) -> tuple[str, dict]:
    """
    Формирует стартовый кадр. Ветку для 2х людей упростили (LEAN v0):
    - одинаковая видимая высота силуэтов (~70% H, но не больше MAX_VISIBLE_FRAC);
    - жёсткий внутренний зазор >= 5% ширины;
    - без автоподтяжек/ростов; фиксированная, предсказуемая геометрия.
    """

    def _min_target_for(framing: str, people_count: int) -> float:
        # согласуем с таблицами MIN_SINGLE_FRAC/MIN_PAIR_FRAC выше
        if "В рост" in framing:
            return MIN_PAIR_FRAC["В рост"] if people_count >= 2 else MIN_SINGLE_FRAC["В рост"]
        elif "По пояс" in framing:
            return MIN_PAIR_FRAC["По пояс"] if people_count >= 2 else MIN_SINGLE_FRAC["По пояс"]
        else:  # По грудь
            return MIN_PAIR_FRAC["По грудь"] if people_count >= 2 else MIN_SINGLE_FRAC["По грудь"]

    W, H = 720, 1280
    base_id = uuid.uuid4().hex
    floor_margin = 0  # пол стоит ровно по нижнему краю кадра
    # (опционально: читать из layout)
    if layout and isinstance(layout, dict) and "floor_margin" in layout:
        floor_margin = int(layout["floor_margin"])

    # верхний «воздух»
    if "По грудь" in framing_key:
        HEADROOM_FRAC = 0.03
    elif "По пояс" in framing_key:
        HEADROOM_FRAC = 0.02
    else:
        HEADROOM_FRAC = 0.005  # для «В рост» допускаем почти нулевой запас

    # --- режим и виртуальный «пол» по формату ---
    is_chest = ("По грудь" in framing_key)
    if is_chest:
        virtual_floor_y = int(H * CHEST_VIRTUAL_FLOOR_FRAC)
    else:
        virtual_floor_y = H - 1  # как и раньше: почти у нижней кромки

    is_waist = ("По пояс" in framing_key)
    if is_chest:
        virtual_floor_y = int(H * CHEST_VIRTUAL_FLOOR_FRAC)
    elif is_waist:
        virtual_floor_y = int(H * WAIST_VIRTUAL_FLOOR_FRAC)  # <-- не в самый низ!
    else:
        virtual_floor_y = H - 1

    # 1) фон
    bg = Image.open(bg_file).convert("RGB")
    bg = _resize_fit_center(bg, W, H)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=0.8))
    canvas = bg.convert("RGBA")

    # 2) вырезаем людей
    cuts = []
    for p in photo_paths:
        im = Image.open(p).convert("RGBA")
        try:
            cut_rgba = smart_cutout(im)
        except NameError:
            cut_rgba = remove(im)
            if isinstance(cut_rgba, (bytes, bytearray)):
                cut_rgba = Image.open(io.BytesIO(cut_rgba)).convert("RGBA")
        cuts.append(cut_rgba)

    if MF_DEBUG:
        try:
            for i, c in enumerate(cuts):
                bb, yb = alpha_metrics(c)
                eff_h = max(1, (yb - bb[1] + 1))
                print(f"[LAYOUT] person#{i+1}: img={c.width}x{c.height} eff_h={eff_h} bbox={bb}")
        except Exception as _e:
            print(f"[LAYOUT] cut metrics err: {_e}")

    # 3) целевая высота относительно кадра (используется в одиночной ветке)
    two = (len(photo_paths) > 1)
    if "В рост" in framing_key:
        TARGET_VISIBLE_FRAC = 0.66 if len(cuts) == 2 else 0.66
    elif "По пояс" in framing_key:
        TARGET_VISIBLE_FRAC = 0.60 if len(cuts) == 2 else 0.56
    else:  # «По грудь»
        TARGET_VISIBLE_FRAC = 0.50 if len(cuts) == 2 else 0.48

    MAX_VISIBLE_FRAC = LEAN_MAX_VISIBLE_FRAC
    TARGET_VISIBLE_FRAC = min(TARGET_VISIBLE_FRAC, MAX_VISIBLE_FRAC)

    # инициализация переменной для одиночной ветки
    target_h = TARGET_VISIBLE_FRAC

    # минимум (анти-карлик)
    target_h_min = _min_target_for(framing_key, len(photo_paths))
    if target_h < target_h_min:
        target_h = target_h_min

    def scale_to_target_effective(img: Image.Image, target: float) -> Image.Image:
        bbox, yb = alpha_metrics(img)
        eff_h = max(1, (yb - bbox[1] + 1))
        scale = (H * target) / eff_h
        if scale > MAX_UPSCALE:
            scale = MAX_UPSCALE
        nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
        return img.resize((nw, nh), RESAMPLE.LANCZOS)

    def place_y_for_floor(img: Image.Image, floor_y: int | None = None) -> int:
        """
        Ставит низ видимого силуэта на заданную линию floor_y (если None — старое поведение у нижней кромки).
        """
        bbox, yb = alpha_metrics(img)
        eff_h = (yb - bbox[1] + 1)
        if floor_y is None:
            y_top_content = H - floor_margin - eff_h
        else:
            y_top_content = int(floor_y - eff_h)
        y_img = y_top_content - bbox[1]
        return int(y_img)

    def draw_with_shadow(base: Image.Image, person: Image.Image, x: int, y: int):
        alpha = person.split()[-1]
        soft = alpha.filter(ImageFilter.GaussianBlur(6))
        shadow = Image.new("RGBA", person.size, (0, 0, 0, 0))
        shadow.putalpha(soft.point(lambda a: int(a * 0.45)))
        base.alpha_composite(shadow, (x, y + 8))
        base.alpha_composite(person, (x, y))

    def _rect_at(x, y, img):
        bx, by, bx1, by1 = alpha_metrics(img)[0]
        return (x + bx, y + by, x + bx1, y + by1)

    def _draw_debug_boxes(base: Image.Image, rects: list[tuple[int,int,int,int]]):
        if not START_OVERLAY_DEBUG:
            return
        ov = Image.new("RGBA", base.size, (0,0,0,0))
        g = ImageDraw.Draw(ov)
        for r in rects:
            g.rectangle(r, outline=(255, 0, 0, 200), width=3)
        m = 20
        g.rectangle((m, m, base.width - m, base.height - m), outline=(0, 255, 0, 180), width=2)
        base.alpha_composite(ov)

    # ------------------------------- 1 человек -------------------------------
    if len(cuts) == 1:
        P = scale_to_target_effective(cuts[0], target_h)
        x = (W - P.width) // 2
        y = place_y_for_floor(P, virtual_floor_y)

        # оценка видимой высоты
        def rect_at_single(px, py, img):
            bx, by, bx1, by1 = alpha_metrics(img)[0]
            return (px + bx, py + by, px + bx1, py + by1)

        r = rect_at_single(x, y, P)
        group_h = r[3] - r[1]
        fmt = "В рост" if "В рост" in framing_key else ("По пояс" if "По пояс" in framing_key else "По грудь")
        min_h_frac = MIN_SINGLE_FRAC[fmt]

        if group_h < int(min_h_frac * H):
            need = (min_h_frac * H) / max(1, group_h)
            cap = SINGLE_UPSCALE_CAP
            new_target = min(target_h * need, target_h * cap)
            if new_target > target_h:
                P = scale_to_target_effective(cuts[0], new_target)
                x = (W - P.width) // 2
                y = place_y_for_floor(P)

        margin = 20
        x = max(margin, min(W - P.width - margin, x))
        top_margin = max(margin, int(HEADROOM_FRAC * H))
        y = max(top_margin, y)  # не поднимаем снизу — пол вплотную к низу

        # мягкий ручной layout для 1 человека (если вдруг прилетит)
        if layout and isinstance(layout, dict):
            scl = int(layout.get("scale_left_pct", 0) or 0)
            dxl = int(layout.get("shift_left_px", 0) or 0)
            if scl != 0:
                k = 1.0 + max(-0.20, min(0.20, scl / 100.0))
                nw, nh = max(1, int(P.width * k)), max(1, int(P.height * k))
                P = P.resize((nw, nh), RESAMPLE.LANCZOS)
                y = place_y_for_floor(P)
            if dxl != 0:
                x += int(-dxl)
            x = max(margin, min(W - P.width - margin, x))
            y = max(margin, min(H - P.height - margin, y))

        # анти-карлик для одиночки
        def _visible_frac(img: Image.Image) -> float:
            bb, yb = alpha_metrics(img)
            eff_h = max(1, (yb - bb[1] + 1))
            return eff_h / H

        grow_tries = 0
        while _visible_frac(P) < _min_target_for(framing_key, 1) and grow_tries < 12:
            new_target = min(target_h * 1.04, 0.98)
            newP = scale_to_target_effective(cuts[0], new_target)
            cx = x + P.width // 2
            cy_floor = place_y_for_floor(newP)
            newx = cx - newP.width // 2
            margin = 20
            newx = max(margin, min(W - newP.width - margin, newx))
            newy = max(margin, min(H - newP.height - margin, cy_floor))
            if newy <= margin or newx <= margin or (newx + newP.width) >= (W - margin):
                break
            P, x, y = newP, newx, newy
            target_h = new_target
            grow_tries += 1

        draw_with_shadow(canvas, P, x, y)
        try:
            _draw_debug_boxes(canvas, [_rect_at(x, y, P)])
        except Exception:
            pass

    # ------------------------------ 2 человека (STRICT SIDE-BY-SIDE) ------------------------------
    else:
        L = cuts[0]
        R = cuts[1]

        # --- базовые хелперы ---
        def _vis_rect(img):
            (bx, by, bx1, by1), _ = alpha_metrics(img)
            return bx, by, bx1, by1

        def _vis_w(img):
            bx, by, bx1, by1 = _vis_rect(img)
            return max(1, bx1 - bx)

        def _vis_h(img):
            (bx, by, bx1, by1), yb = alpha_metrics(img)
            return max(1, yb - by + 1)

        def _scale_abs(img, k):
            k = float(k)
            if k <= 0: 
                return img
            nw, nh = max(1, int(round(img.width * k))), max(1, int(round(img.height * k)))
            if nw == img.width and nh == img.height:
                # принудительно уменьшаем на 1 пикс при k<1, чтобы не зациклиться
                if k < 1.0:
                    nw = max(1, img.width - 1)
                    nh = max(1, img.height - 1)
            return img.resize((nw, nh), RESAMPLE.LANCZOS)

        def _place_pair(center_x, gap_px, left_limit, right_limit, floor_y):
            """
            Ставит пару как ЕДИНУЮ группу внутрь [left_limit, right_limit], сохраняя gap_px и «низ» = floor_y.
            Возвращает (lx, yl, rx, yr, ra, rb).
            """
            bxL, byL, bx1L, by1L = _vis_rect(L)
            bxR, byR, bx1R, by1R = _vis_rect(R)
            wL = bx1L - bxL
            wR = bx1R - bxR

            total = wL + gap_px + wR
            group_left_desired = int(round(center_x - (wL + gap_px/2)))
            group_left  = max(left_limit, min(right_limit - total, group_left_desired))
            group_right = group_left + total

            lx = group_left - bxL
            rx = group_left + wL + gap_px - bxR
            yl = place_y_for_floor(L, floor_y)
            yr = place_y_for_floor(R, floor_y)

            def _rect_at(x, y, img):
                bx, by, bx1, by1 = _vis_rect(img)
                return (x + bx, y + by, x + bx1, y + by1)

            ra = _rect_at(lx, yl, L)
            rb = _rect_at(rx, yr, R)
            return lx, yl, rx, yr, ra, rb

        # --- параметры размещения ---
        MARGIN = 20
        is_full = ("В рост" in framing_key) or ("в рост" in framing_key)
        MAX_VISIBLE_FRAC = LEAN_MAX_VISIBLE_FRAC if is_full else max(LEAN_MAX_VISIBLE_FRAC, 0.76)
        TARGET_VISIBLE_FRAC = min(LEAN_TARGET_VISIBLE_FRAC, MAX_VISIBLE_FRAC)

        # начальный масштаб по видимой высоте
        def _scale_to_vis_frac(img, target_frac):
            cur = _vis_h(img) / H
            if cur <= 1e-6:
                return img
            k = max(0.4, min(MAX_UPSCALE, target_frac / cur))
            return _scale_abs(img, k)

        L = _scale_to_vis_frac(L, TARGET_VISIBLE_FRAC)
        R = _scale_to_vis_frac(R, TARGET_VISIBLE_FRAC)
        if (_vis_h(L)/H) > MAX_VISIBLE_FRAC:
            L = _scale_to_vis_frac(L, MAX_VISIBLE_FRAC)
        if (_vis_h(R)/H) > MAX_VISIBLE_FRAC:
            R = _scale_to_vis_frac(R, MAX_VISIBLE_FRAC)

        # НИКАКОЙ «полосы» — используем всю ширину кадра (кроме безопасных полей)
        left_limit  = MARGIN
        right_limit = W - MARGIN
        available_width = right_limit - left_limit

        # жёсткий минимум зазора
        min_gap = max(MIN_GAP_PX, int(LEAN_MIN_GAP_FRAC * W))
        ideal_gap = max(min_gap, int(IDEAL_GAP_FRAC * W))
        center_x = W // 2

        # Разрешаем лёгкий нахлёст только для «По пояс»
        if "По пояс" in framing_key and os.environ.get("ALLOW_OVERLAP_WAIST", "1") == "1":
            max_ov = float(os.environ.get("MAX_OVERLAP_WAIST_FRAC", "0.1"))  # до 10% ширины кадра
            min_gap = -int(W * max_ov)          # отрицательный gap = допустимый нахлёст
            ideal_gap = max(min_gap, ideal_gap) # если без нахлёста не влезает — упадём до min_gap

        # Ручная настройка, если нужно «ещё ближе»
        if layout and isinstance(layout, dict):
            if "gap_px" in layout:
                ideal_gap = max(min_gap, int(layout["gap_px"]))
            elif "gap_pct" in layout:  # в процентах от ширины кадра
                ideal_gap = max(min_gap, int(W * float(layout["gap_pct"]) / 100.0))

        # --- 1) АВТОСКЕЙЛ ПО ГОРИЗОНТАЛИ (равномерно) ---
        for _ in range(60):
            wL = _vis_w(L)
            wR = _vis_w(R)
            need = wL + wR + min_gap
            if need <= available_width:
                break
            k = max(0.40, min(0.995, (available_width / need) * 0.985))  # чуть с запасом
            L = _scale_abs(L, k)
            R = _scale_abs(R, k)
        # страховка от редких «не сжалось»
        wL = _vis_w(L); wR = _vis_w(R)
        if (wL + wR + min_gap) > available_width:
            k = (available_width - min_gap) / max(1, (wL + wR))
            k = max(0.40, min(0.99, k))
            L = _scale_abs(L, k); R = _scale_abs(R, k)

        # --- 2) СТАВИМ ГРУППУ ВНУТРЬ ПОЛОСЫ (без перекрытия) ---
        gap_px = ideal_gap
        # если идеальный зазор не помещается — берём минимальный
        if _vis_w(L) + _vis_w(R) + gap_px > available_width:
            gap_px = min_gap

        lx, yl, rx, yr, ra, rb = _place_pair(center_x, gap_px, left_limit, right_limit, virtual_floor_y)

        # --- 3) HEADROOM/CLAMP: даунскейлим ВСЮ группу, пока всё не ок ---
        headroom_px = int(HEADROOM_FRAC * H)

        def _top_ok(r):  # r = (x0,y0,x1,y1)
            return r[1] > headroom_px

        # --- 3) HEADROOM: людей больше не уменьшаем; place_y_for_floor гарантирует,
        # что верх в кадре, а ноги «на полу».
        pass

        # --- 4) ФИНАЛЬНЫЕ ГАРАНТИИ: no overlap, всё внутри пределов ---
        # Пере-проверка зазора после всех клампов
        def _inner_gap(a, b):  # a,b = rects
            return b[0] - a[2]

        if _inner_gap(ra, rb) < min_gap:
            # Разводим как группу без изменения зазора (двигаем только центр)
            total = _vis_w(L) + gap_px + _vis_w(R)
            # ставим по центру, затем клампим группу
            group_left = max(left_limit, min(right_limit - total, int(round(center_x - (total/2)))))
            lx = group_left - _vis_rect(L)[0]
            rx = group_left + _vis_w(L) + gap_px - _vis_rect(R)[0]
            yl = place_y_for_floor(L, virtual_floor_y); yr = place_y_for_floor(R, virtual_floor_y)
            ra = (lx + _vis_rect(L)[0], yl + _vis_rect(L)[1], lx + _vis_rect(L)[2], yl + _vis_rect(L)[3])
            rb = (rx + _vis_rect(R)[0], yr + _vis_rect(R)[1], rx + _vis_rect(R)[2], yr + _vis_rect(R)[3])

            # если всё ещё тесно — минимальный даунскейл пары и повторная постановка
            trips = 0
            while _inner_gap(ra, rb) < min_gap and trips < 20:
                L = _scale_abs(L, 0.98)
                R = _scale_abs(R, 0.98)
                total = _vis_w(L) + min_gap + _vis_w(R)
                if total > available_width:
                    # гарантированный вариант: сжимаем так, чтобы ровно поместилось
                    k = (available_width - min_gap) / max(1, (_vis_w(L) + _vis_w(R)))
                    L = _scale_abs(L, k); R = _scale_abs(R, k)
                gap_px = max(min_gap, min(ideal_gap, available_width - (_vis_w(L) + _vis_w(R))))
                lx, yl, rx, yr, ra, rb = _place_pair(center_x, gap_px, left_limit, right_limit, virtual_floor_y)
                trips += 1

        # Рисуем строго слева-направо, перекрытий геометрически нет
        draw_with_shadow(canvas, L, lx, yl)
        draw_with_shadow(canvas, R, rx, yr)
        try:
            _draw_debug_boxes(canvas, [_rect_at(lx, yl, L), _rect_at(rx, yr, R)])
        except Exception:
            pass

        # --- CHEST-UP: мягкий туман снизу, чтобы спрятать «несуществующие» ноги ---
        if is_chest:
            fog_y = int(H * CHEST_FOG_START_FRAC)
            add_bottom_fog(canvas, fog_y, color=CHEST_FOG_COLOR, max_alpha=CHEST_FOG_MAX_ALPHA)
        elif is_waist:
            fog_y = int(H * WAIST_FOG_START_FRAC)
            add_bottom_fog(canvas, fog_y, color=CHEST_FOG_COLOR, max_alpha=WAIST_FOG_MAX_ALPHA)

    # --- метрики/сохранение ---
    # Добавляем дату и время в имя файла
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = f"uploads/start_{timestamp}_{base_id}.png"
    metrics = {"W": W, "H": H, "framing": framing_key}

    def _abs_rect(x, y, img):
        (bx, by, bx1, by1), yb = alpha_metrics(img)
        return [x + bx, y + by, x + bx1, y + by1], yb + y

    if len(cuts) == 1:
        rP, fy = _abs_rect(x, y, P)
        h_px = rP[3] - rP[1]
        w_px = rP[2] - rP[0]
        metrics["L"] = {
            "rect_abs": rP, "height_px": int(h_px), "width_px": int(w_px),
            "height_frac": float(h_px) / H,
            "center_x_frac": float((rP[0]+rP[2])/2) / W,
            "scale": float(P.width) / max(1.0, cuts[0].width),
            "floor_y": int(fy)
        }
    else:
        rL, fyl = _abs_rect(lx, yl, L)
        rR, fyr = _abs_rect(rx, yr, R)
        hL = rL[3]-rL[1]; wL = rL[2]-rL[0]
        hR = rR[3]-rR[1]; wR = rR[2]-rR[0]
        gap_px = max(0, rR[0] - rL[2])
        metrics["L"] = {
            "rect_abs": rL, "height_px": int(hL), "width_px": int(wL),
            "height_frac": float(hL)/H,
            "center_x_frac": float((rL[0]+rL[2])/2)/W,
            "scale": float(L.width)/max(1.0, cuts[0].width),
            "floor_y": int(fyl)
        }
        metrics["R"] = {
            "rect_abs": rR, "height_px": int(hR), "width_px": int(wR),
            "height_frac": float(hR)/H,
            "center_x_frac": float((rR[0]+rR[2])/2)/W,
            "scale": float(R.width)/max(1.0, cuts[1].width),
            "floor_y": int(fyr)
        }
        metrics["gap_px"]  = int(gap_px)
        metrics["gap_frac"]= float(gap_px)/W

    if OAI_DEBUG or PREVIEW_START_FRAME:
        _save_layout_debug(canvas, metrics, base_id)
    canvas.save(out, "PNG")
    print(f"[frame] saved → {out} ({canvas.width}×{canvas.height})")

    # Очистка старых стартовых кадров
    cleanup_uploads_folder()

    return out, metrics

# ---------- ПОСТ-ОБРАБОТКА через ffmpeg (wm + музыка + титр + склейка) ----------
def create_title_image(width: int, height: int, text: str, output_path: str):
    """Создает изображение с титром с автоматическим подбором размера шрифта"""
    title_img = Image.new("RGB", (width, height), (0, 0, 0))
    d = ImageDraw.Draw(title_img)

    # Автоматический подбор размера шрифта
    max_width = width - 40  # Отступ 20 пикселей с каждой стороны
    font_size = 60  # Начинаем с большого шрифта

    while font_size > 12:  # Минимальный размер шрифта
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except:
            font = ImageFont.load_default()

        # Измеряем ширину текста с текущим шрифтом
        bbox = d.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]

        if text_width <= max_width:
            break  # Шрифт подходит

        font_size -= 2  # Уменьшаем размер шрифта

    # Рисуем текст по центру
    d.text((width//2, height//2), text, fill=(255,255,255), font=font, anchor="mm")
    title_img.save(output_path)
    return output_path

def _fit_text_in_box(draw, text, box_w, box_h, font_path, max_size, min_size=18, line_spacing=1.15, bold=False, anchor="mm"):
    size = max_size
    while size >= min_size:
        try:
            font = ImageFont.truetype(font_path, size)
        except Exception:
            font = ImageFont.load_default()
        # перенос по словам
        words = text.split()
        lines, cur = [], []
        for w in words:
            test = " ".join(cur + [w]) if cur else w
            bbox = draw.textbbox((0,0), test, font=font)
            if bbox[2] - bbox[0] <= box_w:
                cur.append(w)
            else:
                if not cur:  # слово само по себе длиннее строки
                    cur = [w]
                lines.append(" ".join(cur))
                cur = [w]
        if cur:
            lines.append(" ".join(cur))

        # высота всех строк
        heights = []
        for line in lines:
            b = draw.textbbox((0,0), line, font=font)
            heights.append(b[3]-b[1])
        total_h = int(sum(heights) + (len(lines)-1) * (heights[0] if heights else 0) * (line_spacing-1))

        if total_h <= box_h:
            return font, lines, total_h
        size -= 2
    return font, [text], min(box_h, 0)

def create_memorial_title_image(width, height, fio, dates, mem_text, output_path, candle_path=None):
    # было: Image.new("RGB", (width, height), (0, 0, 0))
    img = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)

    candle = None
    if candle_path and os.path.isfile(candle_path):
        try:
            candle = Image.open(candle_path).convert("RGBA")  # уже RGBA — ок
        except Exception:
            candle = None

    pad = int(os.environ.get("TITLE_PAD", "24"))
    left, right, top, bottom = pad, width - pad, pad, height - pad

    # Размер и позиция свечи
    candle_w = int(width * CANDLE_WIDTH_FRAC) if candle else 0
    candle_h = 0
    if candle:
        k = candle_w / candle.width
        candle = candle.resize((candle_w, int(candle.height * k)), RESAMPLE.LANCZOS)
        candle_h = candle.height
        # Компонуем внизу слева
        img.alpha_composite(candle, (left, height - pad - candle.height))

    # Области под текст
    # 1) FIO (верх, по центру)
    fio_box_w = width - 2*pad
    # сдвигаем всё, что «сверху», ниже углового логотипа
    safe_top = _wm_safe_top_px()
    fio_box_h = int(height * 0.16)
    fio_y0 = top + safe_top
    fio_font, fio_lines, fio_h = _fit_text_in_box(
        d, fio, fio_box_w, fio_box_h,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", max_size=72, min_size=26, line_spacing=1.12
    )
    y = fio_y0 + (fio_box_h - fio_h)//2
    for line in fio_lines:
        b = d.textbbox((0,0), line, font=fio_font)
        d.text((width//2, y + (b[3]-b[1])//2), line, fill=(255,255,255), font=fio_font, anchor="mm")
        y += int((b[3]-b[1]) * 1.12)

    # 2) Dates (сразу под ФИО)
    dates_box_h = int(height * 0.08)
    dates_y0 = fio_y0 + fio_box_h + int(pad*0.6)
    dates_font, dates_lines, dates_h = _fit_text_in_box(
        d, dates, fio_box_w, dates_box_h,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max_size=42, min_size=20, line_spacing=1.0
    )
    y = dates_y0 + (dates_box_h - dates_h)//2
    for line in dates_lines:
        b = d.textbbox((0,0), line, font=dates_font)
        d.text((width//2, y + (b[3]-b[1])//2), line, fill=(200,200,200), font=dates_font, anchor="mm")
        y += (b[3]-b[1])

    # 3) Memorial text (предпочтение: по всей ширине; fallback — справа от свечи)
    mem_top = max(int(height * 0.52), dates_y0 + dates_box_h + pad)  # верх памятного текста
    mem_full_left  = left
    mem_full_right = right
    mem_full_w     = mem_full_right - mem_full_left
    mem_full_h     = bottom - mem_top

    # PASS A: пробуем уместить по всей ширине
    mem_font, mem_lines, mem_h = _fit_text_in_box(
        d, mem_text, mem_full_w, mem_full_h,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max_size=40, min_size=18, line_spacing=1.18
    )
    # если уместилось — рисуем по центру всей ширины
    if mem_h <= mem_full_h:
        y = mem_top + (mem_full_h - mem_h)//2
        for line in mem_lines:
            b = d.textbbox((0,0), line, font=mem_font)
            d.text((width//2, y + (b[3]-b[1])//2), line, fill=(255,255,255), font=mem_font, anchor="mm")
            y += int((b[3]-b[1]) * 1.18)
    else:
        # PASS B: рисуем только там, где свечи нет (справа от неё)
        candle_reserved_w = (candle_w + pad) if candle else 0
        mem_left  = max(left, candle_reserved_w + pad)
        mem_right = right
        mem_box_w = max(100, mem_right - mem_left)
        mem_box_h = max(100, bottom - mem_top)

        mem_font, mem_lines, mem_h = _fit_text_in_box(
            d, mem_text, mem_box_w, mem_box_h,
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max_size=40, min_size=18, line_spacing=1.18
        )
        y = mem_top + (mem_box_h - mem_h)//2
        cx = mem_left + mem_box_w//2
        for line in mem_lines:
            b = d.textbbox((0,0), line, font=mem_font)
            d.text((cx, y + (b[3]-b[1])//2), line, fill=(255,255,255), font=mem_font, anchor="mm")
            y += int((b[3]-b[1]) * 1.18)

    img.save(output_path)
    return output_path

def postprocess_concat_ffmpeg(video_paths: List[str], music_path: str|None, title_text: str, save_as: str, bg_overlay_file: str|None = None, titles_meta: dict|None = None, candle_path: str|None = None) -> str:
    """Постобработка видео через ffmpeg (склейка + фон-анимация + водяной знак + музыка). С фолбэком, faststart и портативной копией."""
    import tempfile

    def _escape_concat_path(p: str) -> str:
        # экранируем одинарные кавычки для concat-файла
        return os.path.abspath(p).replace("'", "'\\''")

    temp_dir = "renders/temp"
    os.makedirs(temp_dir, exist_ok=True)

    # Если несколько сцен — сначала делаем промежуточную склейку с кроссфейдами,
    # а дальше работаем как с одним видео.
    if len(video_paths) > 1:
        premerged = _merge_with_fades(video_paths, fade_sec=CROSSFADE_SEC)
        video_paths = [premerged]

    # 1) Финальный титр (PNG)
    title_img_path = f"{temp_dir}/title.png"
    if titles_meta:
        create_memorial_title_image(
            720, 1280,
            titles_meta.get("fio","") or "",
            titles_meta.get("dates","") or "",
            titles_meta.get("mem","") or "",
            title_img_path,
            candle_path=candle_path or CANDLE_PATH
        )
    else:
        create_title_image(720, 1280, title_text, title_img_path)

    # 2) 2-секундный ролик из титра
    title_video_path = f"{temp_dir}/title_video.mp4"
    _run_ffmpeg([
        "ffmpeg", "-y", "-loop", "1", "-i", title_img_path,
        "-t", "2", "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        title_video_path
    ], tag="title_video", out_hint=title_video_path)

    # 3) Файл для concat
    concat_list_path = f"{temp_dir}/concat_list.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for vp in video_paths:
            f.write(f"file '{_escape_concat_path(vp)}'\n")
        f.write(f"file '{_escape_concat_path(title_video_path)}'\n")

    # 4) Склейка (попытка без перекодирования)
    concat_video_path = f"{temp_dir}/concat_video.mp4"
    try:
        _run_ffmpeg([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-c", "copy", "-movflags", "+faststart",
            concat_video_path
        ], tag="concat_copy", out_hint=concat_video_path)
    except subprocess.CalledProcessError:
        # Фолбэк: перекодирование под общий профиль
        _run_ffmpeg([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-r", "24",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-movflags", "+faststart",
            concat_video_path
        ], tag="concat_reencode", out_hint=concat_video_path)

    # 4.5) Деликатная анимация фона (если есть картинка)
    bg_anim_video_path = concat_video_path
    if bg_overlay_file and os.path.isfile(bg_overlay_file):
        try:
            bg_anim_video_path = f"{temp_dir}/with_bg_anim.mp4"
            _run_ffmpeg([
                "ffmpeg", "-y",
                "-i", concat_video_path,
                "-loop", "1", "-i", bg_overlay_file,
                "-filter_complex",
                "[1:v]scale=720:1280,boxblur=25:1,format=rgba,colorchannelmixer=aa=0.08,setsar=1[ov];"
                "[0:v][ov]overlay=x='t*2':y=0:shortest=1,format=yuv420p[v]",
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                "-movflags", "+faststart",
                bg_anim_video_path
            ], tag="bg_overlay", out_hint=bg_anim_video_path)
        except Exception as e:
            print(f"BG overlay skipped: {e}")
    else:
        print("BG overlay disabled (no file)")

    # 5) Водяной знак
    wm_video_path = bg_anim_video_path
    if os.path.isfile(WATERMARK_PATH):
        wm_video_path = f"{temp_dir}/with_watermark.mp4"
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", bg_anim_video_path, "-i", WATERMARK_PATH,
            "-filter_complex", "[1:v]scale=120:-1[wm];[0:v][wm]overlay=W-w-24:24",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            wm_video_path
        ], tag="wm_corner", out_hint=wm_video_path)

    # 6) Музыка (или просто сохранить)
    if music_path and os.path.isfile(music_path):
        # зациклить музыку и подложить под видео
        _run_ffmpeg([
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", music_path,     # бесконечная музыка
            "-i", wm_video_path,                         # видео
            "-map", "1:v", "-map", "0:a",
            "-c:v", "copy",
            "-c:a", "aac", "-ar", "44100",
            "-shortest", "-af", "volume=0.6",
            "-movflags", "+faststart",
            save_as
        ], tag="mux_music", out_hint=save_as)
    else:
        # портативная копия + faststart
        import shutil
        shutil.copyfile(wm_video_path, save_as)
        try:
            tmp_fast = f"{temp_dir}/faststart.mp4"
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", save_as, "-c", "copy", "-movflags", "+faststart", tmp_fast
            ], tag="faststart_copy", out_hint=tmp_fast)
            shutil.move(tmp_fast, save_as)
        except Exception:
            pass

    return save_as

def cleanup_dir_keep_last_n(dir_path: str, keep_n: int = 20, extensions: tuple[str, ...] = ()):
    try:
        items = []
        for name in os.listdir(dir_path):
            p = os.path.join(dir_path, name)
            if os.path.isfile(p):
                if not extensions or name.lower().endswith(extensions):
                    items.append((p, os.path.getmtime(p)))
        items.sort(key=lambda x: x[1], reverse=True)
        for p, _ in items[keep_n:]:
            try:
                os.remove(p)
            except Exception:
                pass
    except FileNotFoundError:
        pass

def cleanup_artifacts(keep_last: int = 20):
    # Полностью чистим временную папку рендеров (кроме режима отладки)
    if not OAI_DEBUG:
        shutil.rmtree("renders/temp", ignore_errors=True)
    # Оставляем только N последних оригиналов и финалов
    cleanup_dir_keep_last_n("uploads", keep_n=keep_last, extensions=(".jpg", ".jpeg", ".png", ".webp"))
    cleanup_dir_keep_last_n("renders", keep_n=keep_last, extensions=(".mp4", ".mov", ".mkv", ".webm"))

def _download_tg_photo(file_id: str, uid: int) -> str:
    fi = bot.get_file(file_id)
    content = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{fi.file_path}", timeout=120).content
    pth = f"uploads/{uid}_{int(time.time())}_{uuid.uuid4().hex}.jpg"
    with open(pth, "wb") as f:
        f.write(content)

    # Очистка старых входящих фото
    cleanup_uploads_folder()

    return pth

def _download_tg_audio(file_id: str, uid: int) -> str:
    """Скачивает аудиофайл Telegram и кладёт в папку audio с именем user_{uid}_*.ext"""
    fi = bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{fi.file_path}"
    content = requests.get(url, timeout=300).content

    # Определяем расширение
    ext = ""
    try:
        import os
        _, ext = os.path.splitext(fi.file_path or "")
        ext = ext.lower()
    except Exception:
        pass
    if not ext or ext not in ALLOWED_AUDIO_EXTS:
        ext = ".mp3"

    os.makedirs("audio", exist_ok=True)
    pth = f"audio/user_{uid}_{uuid.uuid4().hex}{ext}"
    with open(pth, "wb") as f:
        f.write(content)
    return pth

# ---------- ХЭНДЛЕРЫ ----------
@bot.message_handler(commands=["start","reset"])
def start_cmd(m: telebot.types.Message):
    uid = m.from_user.id
    if ADMIN_CHAT_ID:
        try:
            if m.from_user.username:
                user_label = f"@{m.from_user.username}"
            else:
                fn = (m.from_user.first_name or "").strip()
                ln = (m.from_user.last_name or "").strip()
                user_label = (f"{fn} {ln}".strip() or "—")
            bot.send_message(int(ADMIN_CHAT_ID), f"🚀 Старт бота\nuid: {uid}\nuser: {user_label}")
        except Exception:
            pass
    cleanup_user_custom_bg(uid)
    # Сброс текущего состояния и показ главного меню
    users[uid] = new_state()
    show_main_menu(uid, 'Выберите пункт меню или перейдите к созданию видео, нажав «Сделать видео».')
    example_paths = [
        "assets/examples/example3.mp4",
        "assets/examples/example2.mp4",
        "assets/examples/example1.mp4",
    ]
    for ex_path in example_paths:
        if os.path.isfile(ex_path):
            try:
                with open(ex_path, "rb") as f:
                    bot.send_video(uid, f, caption="🎞 Пример ролика Memory Forever")
            except Exception as e:
                print(f"[START] example send failed: {e}")
            finally:
                break

# Главное меню (кнопка)
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_MAIN)
def on_menu_main(m: telebot.types.Message):
    uid = m.from_user.id
    # Не трогаем текущую генерацию, просто показываем меню
    show_main_menu(uid)

# Запуск мастера (кнопка «Сделать видео»)
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_START)
def on_menu_start_wizard(m: telebot.types.Message):
    uid = m.from_user.id
    users[uid] = new_state()
    bot.send_message(
        uid,
        "Шаг 1/6. Выберите <b>формат кадра</b>.",
        reply_markup=kb_formats()
    )

# Стоимость
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_PRICE)
def on_menu_price(m: telebot.types.Message):
    uid = m.from_user.id
    price_text = (
        "💲 <b>Стоимость</b>\n\n"
        "• <b>5 сек</b> — <b>бесплатно</b> (до 2 раз на пользователя)\n"
        "• <b>10 сек</b> — <b>100 ₽</b> за каждый выбранный сюжет\n"
        "• <b>Объединение сюжетов</b> — сумма цен всех выбранных сюжетов\n\n"
        "🧩 <b>Опции</b>\n"
        "• <b>Загрузить свой фон</b> — <b>50 ₽</b>\n"
        "• <b>Загрузить свою музыку</b> — <b>50 ₽</b>\n"
        "• <b>Свои финальные титры</b> — <b>50 ₽</b> (до 60 символов)\n\n"
        "• <b>Вторая вариация (другой сервис генерации)</b> — <b>+50% к итоговой стоимости</b>\n"
        "<i>Опции применяются ко всему ролику и добавляются к итоговой цене.</i>"
    )
    bot.send_message(uid, price_text, reply_markup=kb_main_menu())

# Инструкция
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_GUIDE)
def on_menu_guide(m: telebot.types.Message):
    uid = m.from_user.id
    guide = (
        "<b>ВАЖНО!</b> Для пары — похожий масштаб людей. Чем ближе масштаб на фото, тем качественнее будет видео.\n\n"
        "<b>Как сделать видео</b>\n"
        "1) Нажмите «Сделать видео».\n"
        "2) Выберите формат кадра (🧍 В рост / 👨‍💼 По пояс / 👨‍💼 По грудь).\n"
        "3) Выберите сюжет или несколько сюжетов (мы их объединим в один) → «✅ Выбрано, дальше». Подсказка: «Уходит в небеса» доступен только для «В рост».\n"
        "4) Выберите ✅ фон:\n"
        "   • «👁 Предпросмотр» — посмотреть фон.\n"
        "   • «✅ Выбрать» — зафиксировать фон.\n"
        "   • «➕ Загрузить свой фон» — вертикальный 9:16 (≥720×1280); хранится только в этой сессии и удаляется после выдачи видео.\n"
        "5) Выберите ✅ музыку:\n"
        "   • «🎧 Прослушать», «✅ Выбрать» или «🔇 Без музыки».\n"
        "   • «➕ Загрузить свой трек» — MP3/M4A/WAV; трек автоматически зациклим/обрежем и приглушим под видео.\n"
        "6) Выберите ✅ нужны ли Вам титры:\n"
        "   • «✅ Добавить титры» или «🔇 Без титров».\n"
        "   • «➕ Загрузить свои Титры: Ф.И.О., даты, памятную надпись.\n"
        "7) Пришлите фото для каждого сюжета:\n"
        "   • Одиночная сцена — 1 фото (анфас, отдельным сообщением).\n"
        "   • Пара — 2 отдельных фото (каждый — анфас).\n"
        "   • Если 2 фото не строго анфас, то сначала пришлите фото которое обращено вправо, затем фото которое обращено влево, чтобы получить более качественно видео.\n"
        "   • После фото бот покажет старт-кадр: «✅ Согласовать» или «🔁 Заменить фото».\n"
        "   • Генерация начнётся после согласования старт-кадров по всем сюжетам.\n"
        "8) Получение результата:\n"
        "   • Сгенерируем сцены, склеим с плавными переходами, добавим титр/водяной знак/музыку и пришлём ролик сюда.\n\n"
        "<b>Советы</b>\n"
        "• Фото: светлое, чёткое; лучше вертикальные. Для пары — похожая ширина плеч/масштаб.\n"
        "• Фон: вертикальный 9:16, без посторонних лиц/логотипов.\n"
        "• Начать заново: «🏠 В главное меню» или /start.\n"
        "• Если что-то не получилось — бот сообщит причину и подскажет, что поправить."
    )
    # 1) текстовая инструкция (оставляем клавиатуру)
    bot.send_message(uid, guide, reply_markup=kb_main_menu())

    # 2) видео-инструкция (если файл на месте)
    try:
        if os.path.isfile(GUIDE_VIDEO_PATH):
            with open(GUIDE_VIDEO_PATH, "rb") as f:
                bot.send_video(
                    uid, f,
                    caption="🎥 Короткая видео-инструкция",
                    supports_streaming=True,
                    width=720, height=1280
                )
        else:
            bot.send_message(
                uid,
                "Чтобы отправлять видео-инструкцию, положите файл <code>guide.mov</code> "
                "в папку <code>assets/guide</code>."
            )
    except Exception as e:
        bot.send_message(uid, f"Не удалось отправить видео-инструкцию: {e}")

# Примеры работ
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_DEMO)
def on_menu_demo(m: telebot.types.Message):
    uid = m.from_user.id
    demo_dir = "assets/examples"
    paths = [
        os.path.join(demo_dir, "example1.mp4"),
        os.path.join(demo_dir, "example2.mp4"),
        os.path.join(demo_dir, "example3.mp4"),
        os.path.join(demo_dir, "example4.mp4"),
        os.path.join(demo_dir, "example5.mp4"),
        os.path.join(demo_dir, "example6.mp4"),
        os.path.join(demo_dir, "example7.mp4"),
        os.path.join(demo_dir, "example8.mp4"),
    ]
    sent = False
    for p in paths:
        if os.path.isfile(p):
            with open(p, "rb") as f:
                bot.send_video(uid, f)
            sent = True
    if not sent:
        bot.send_message(uid, "Загрузите 3 файла примеров в папку <code>assets/examples</code> под именами example1.mp4, example2.mp4, example3.mp4", reply_markup=kb_main_menu())

# Техподдержка
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_SUPPORT)
def on_menu_support(m: telebot.types.Message):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    st["support"] = True
    bot.send_message(uid, "Напишите ваше сообщение. Мы свяжемся с вами. (Для выхода нажмите «В главное меню»).", reply_markup=kb_main_menu())

@bot.pre_checkout_query_handler(func=lambda q: True)
def on_pre_checkout_stars(q: telebot.types.PreCheckoutQuery):
    try:
        bot.answer_pre_checkout_query(q.id, ok=True)
    except Exception:
        # молча, чтобы не падать на редких глюках
        pass

@bot.message_handler(content_types=['successful_payment'])
def on_successful_payment(m: telebot.types.Message):
    uid = m.from_user.id
    st  = users.setdefault(uid, new_state())
    sp  = m.successful_payment

    # Для Stars валюта XTR; total_amount — число звёзд
    if getattr(sp, "currency", "") == "XTR":
        st["payment_confirmed"] = True
        st["await_payment"] = False
        # можешь залогировать: sp.total_amount (кол-во ⭐), sp.invoice_payload и т.п.
        try:
            bot.send_message(uid, f"✅ Оплата Stars получена ({sp.total_amount}⭐). Запускаю генерацию.")
        except Exception:
            pass
        try:
            _after_payment_continue(uid, st)   # у тебя уже используется в Точке — переиспользуем
        except Exception as e:
            print(f"[PAY] after stars payment error: {e}")
            # на всякий — прямой вызов, если у тебя нет _after_payment_continue:
            try:
                _render_all_scenes_from_approved(uid, st)
            except Exception as e2:
                bot.send_message(uid, f"Не удалось запустить генерацию: {e2}")
    else:
        # это были «старые» провайдерские платежи, если появятся — игнор/логируй
        pass

@bot.message_handler(func=lambda msg: msg.text=="🔁 Сбросить выбор сюжетов")
def reset_scenes(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    st["scenes"] = []
    bot.send_message(uid, "Сюжеты очищены. Выберите заново.", reply_markup=kb_scenes(st.get("format")))

@bot.message_handler(func=lambda msg: msg.text=="✅ Выбрано, дальше")
def after_scenes(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    if not st["scenes"]:
        bot.send_message(uid, "Пока ничего не выбрано. Отметьте хотя бы один сюжет.",
                         reply_markup=kb_scenes(st.get("format")))
        return
    bot.send_message(uid, "Шаг 3/6. Выберите ✅ <b>фон</b>. Можно предварительно 👁 посмотреть. Или загрузите свой.", reply_markup=kb_backgrounds_inline())

@bot.message_handler(func=lambda msg: msg.text in SCENES.keys())
def choose_scene(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())

    # если формат ещё не выбран (на всякий) — просим выбрать формат
    if not st.get("format"):
        bot.send_message(uid, "Сначала выберите формат кадра (Шаг 1/6).", reply_markup=kb_formats())
        return

    allowed = set(available_scene_keys(st["format"]))
    if m.text not in allowed:
        # конкретная ошибка для «лестницы»
        if SCENES.get(m.text, {}).get("kind") == "stairs":
            bot.send_message(uid, "Сюжет «Уходит в небеса» доступен только для формата «🧍 В рост». "
                                  "Поменяйте формат или выберите другой сюжет.",
                             reply_markup=kb_scenes(st["format"]))
        else:
            bot.send_message(uid, "Этот сюжет недоступен для выбранного формата.", reply_markup=kb_scenes(st["format"]))
        return

    # --- Запрет смешивания бесплатного сюжета с любыми другими ---
    if m.text == FREE_HUGS_SCENE and st["scenes"]:
        bot.send_message(uid, "Нельзя соединять бесплатный сюжет «Объятия 5с» с другими. Соединение доступно только между платными сюжетами.",
                         reply_markup=kb_scenes(st.get("format")))
        return
    if m.text != FREE_HUGS_SCENE and FREE_HUGS_SCENE in st["scenes"]:
        bot.send_message(uid, "Нельзя соединять бесплатный сюжет «Объятия 5с» с другими. Соединение доступно только между платными сюжетами.",
                         reply_markup=kb_scenes(st.get("format")))
        return

    # --- Ранняя проверка лимита бесплатных генераций (2 на аккаунт) ---
    if (m.text == FREE_HUGS_SCENE 
        and get_free_hugs_count(uid) >= FREE_HUGS_LIMIT
        and not is_free_hugs_whitelisted(uid)):
        bot.send_message(uid, "Вы уже сделали 2 бесплатные генерации по сюжету «Объятия 5с». "
                              "Можно попробовать с другого аккаунта или выбрать платный сюжет.",
                         reply_markup=kb_scenes(st.get("format")))
        return

    if m.text not in st["scenes"]:
        st["scenes"].append(m.text)

    picked = " · ".join(st["scenes"])
    bot.send_message(uid, f"Выбрано: {picked}\nДобавьте ещё или нажмите «✅ Выбрано, дальше».",
                     reply_markup=kb_scenes(st["format"]))

@bot.message_handler(func=lambda msg: msg.text in FORMATS.keys())
def choose_format(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    st["format"] = m.text
    st["scenes"] = []  # обнуляем выбор сцен под новый формат
    bot.send_message(
        uid,
        "Шаг 2/6. Выберите <b>сюжеты</b> (можно несколько). Когда закончите — нажмите «✅ Выбрано, дальше».",
        reply_markup=kb_scenes(st["format"])
    )

@bot.message_handler(func=lambda msg: msg.text in BACKGROUNDS.keys())
def choose_background(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    st["bg"] = m.text
    bot.send_message(uid, "Шаг 4/6. Выберите ✅ <b>музыку</b>. Можно предварительно 🎧 прослушать. Или загрузите свой трек.", reply_markup=kb_music())

@bot.message_handler(func=lambda msg: msg.text in MUSIC.keys() or msg.text=="🔇 Без музыки")
def choose_music(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    st["music"] = None if m.text=="🔇 Без музыки" else m.text

    if not st["scenes"]:
        bot.send_message(uid, "Ошибка: не выбраны сюжеты. Начните с /start")
        return

    # Переходим к шагу 5/6: Титры
    st["titles_mode"] = "none"
    st["await_titles_field"] = None
    bot.send_message(
        uid,
        "Шаг 5/6. <b>Титры</b>\nВыберите вариант:",
        reply_markup=kb_titles()
    )

@bot.message_handler(func=lambda msg: msg.text == BTN_GO_HOME)
def go_home(m: telebot.types.Message):
    uid = m.from_user.id
    # Не ломаем текущую очередь задач — просто показываем меню
    show_main_menu(uid)

@bot.message_handler(content_types=["photo"])
def on_photo(m: telebot.types.Message):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    st["await_approval"] = None  # если прилетели новые фото — сбрасываем прошлое превью

    # 0) Если ждём пользовательский фон — принимаем его и уходим к шагу музыки
    if st.get("await_custom_bg"):
        file_id = m.photo[-1].file_id
        tmp_path = _download_tg_photo(file_id, uid)

        ext = os.path.splitext(tmp_path)[1].lower() or ".jpg"
        new_path = f"uploads/custombg_{uid}_{int(time.time())}_{uuid.uuid4().hex}{ext}"
        try:
            os.replace(tmp_path, new_path)  # попытка переименовать
        except Exception:
            shutil.copyfile(tmp_path, new_path)  # фолбэк на копию

        st["bg"] = CUSTOM_BG_KEY
        st["bg_custom_path"] = new_path
        st["await_custom_bg"] = False

        bot.send_message(
            uid,
            "🖼 Пользовательский фон загружен и выбран ✅\n\nШаг 4/6. Выберите ✅ <b>музыку</b>. Можно предварительно 🎧 прослушать. Или загрузите свой трек.",
            reply_markup=kb_music()
        )
        return

    # Проверяем, что шаги до фото пройдены
    if not (st["scenes"] and st["format"] and st["bg"]):
        bot.send_message(uid, "Сначала пройдите шаги: Формат → Сюжет(ы) → Фон → (Музыка — можно «Без музыки»).")
        return

    jobs = st.get("scene_jobs") or []
    if not jobs:
        # на всякий – инициализируем очередь (если пришли фото после рестарта бота)
        _init_scene_jobs(st)
        jobs = st["scene_jobs"]

    idx = st.get("scene_idx", 0)
    if idx >= len(jobs):
        bot.send_message(uid, "Все сюжеты уже обработаны.")
        return

    job = jobs[idx]
    need_people = job["people"]

    # Для одиночной сцены не принимаем альбомы (2+ фото разом) — но шлём предупреждение только ОДИН раз на альбом
    if need_people == 1 and m.media_group_id:
        key = (uid, m.media_group_id)
        if key in SINGLE_ALBUM_REJECTED:
            return  # остальные фото из того же альбома игнорируем молча
        SINGLE_ALBUM_REJECTED.add(key)
        bot.send_message(
            uid,
            "Для данного сюжета предполагается только 1 фото с 1 человеком, пришлите 1 фото (анфас)."
        )
        # необязательно, но можно: если сет разрастётся — время от времени чистить
        if len(SINGLE_ALBUM_REJECTED) > 5000:
            SINGLE_ALBUM_REJECTED.clear()
        return

    # Если уже собрали достаточно фото для текущего сюжета — вежливо игнорируем лишнее
    if len(job["photos"]) >= need_people:
        if need_people == 1:
            bot.send_message(
                uid,
                "Для данного сюжета нужно только 1 фото. "
                "Если хотите заменить — нажмите «🔁 Заменить фото» и пришлите новое 1 фото (анфас)."
            )
        else:
            bot.send_message(uid, "Фото уже получены для текущего сюжета — дождитесь согласования старт-кадра.")
        return

    # Скачиваем фото
    file_id = m.photo[-1].file_id
    saved_path = _download_tg_photo(file_id, uid)

    # Мягкая валидация
    ok_photo, warns = validate_photo(saved_path)
    if warns:
        bot.send_message(uid, "⚠️ Подсказка по фото:\n" + "\n".join(f"• {w}" for w in warns))
    if not ok_photo:
        bot.send_message(uid, "Фото очень низкого качества. Можем продолжить, но результат может быть хуже. "
                              "Если есть другое фото — пришлите ещё одно. Продолжаю с этим фото.")

    # Если альбом (media_group)
    if m.media_group_id:
        rec = PENDING_ALBUMS.setdefault(
            m.media_group_id,
            {"uid": uid, "scene_idx": idx, "need": need_people, "paths": []}
        )
        # на случай если альбом «переехал» на другой индекс сюжета/юзера
        rec["uid"] = uid
        rec["scene_idx"] = idx
        rec["need"] = need_people
        rec["paths"].append(saved_path)

        if len(rec["paths"]) >= need_people:
            job["photos"].extend(rec["paths"][:need_people])
            PENDING_ALBUMS.pop(m.media_group_id, None)

            bot.send_message(uid, "Начинаю генерацию стартового кадра…")
            try:
                _prepare_start_for_scene_and_ask_approval(uid, st, idx)
            except Exception as e:
                print("GEN ERR:", e)
                bot.send_message(uid, f"Что-то пошло не так: {e}")
                users[uid] = new_state()
                show_main_menu(uid)
        return

    # Обычное одиночное фото
    job["photos"].append(saved_path)
    if len(job["photos"]) < need_people:
        left = need_people - len(job["photos"])
        bot.send_message(uid, f"Фото получено ✅  Осталось прислать ещё {left}.")
        return

    bot.send_message(uid, "Начинаю генерацию стартового кадра…")
    try:
        _prepare_start_for_scene_and_ask_approval(uid, st, idx)
    except Exception as e:
        print("GEN ERR:", e)
        bot.send_message(uid, f"Что-то пошло не так: {e}")
        users[uid] = new_state()
        show_main_menu(uid)

@bot.message_handler(content_types=["audio", "document"])
def on_audio_upload(m: telebot.types.Message):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())

    # Принимаем трек ТОЛЬКО если мы на шаге музыки и ждем пользовательский трек
    if not st.get("await_custom_music"):
        return

    # Определяем file_id
    file_id = None
    # 1) стандартный аудио-тип
    if getattr(m, "audio", None):
        file_id = m.audio.file_id
        # дополнительная проверка не нужна: Telegram уже классифицировал как audio
    # 2) документ, но это аудио по mime/расширению
    elif getattr(m, "document", None):
        mt = (m.document.mime_type or "").lower()
        fname = (m.document.file_name or "").lower()
        is_audio_doc = mt.startswith("audio/") or any(fname.endswith(ext) for ext in ALLOWED_AUDIO_EXTS)
        if is_audio_doc:
            file_id = m.document.file_id

    if not file_id:
        bot.send_message(uid, "Это похоже не аудиофайл. Пришлите mp3/m4a/wav/ogg.")
        return

    # Скачиваем
    try:
        path = _download_tg_audio(file_id, uid)
    except Exception as e:
        bot.send_message(uid, f"Не удалось сохранить трек: {e}")
        return

    # Сохраняем выбор и убираем флаг ожидания
    st["custom_music_path"] = path
    st["music"] = CUSTOM_MUSIC_KEY
    st["await_custom_music"] = False

    bot.send_message(uid, "✅ Трек загружен.")
    # Переходим к шагу 5/6: Титры
    st["titles_mode"] = "none"
    st["await_titles_field"] = None
    bot.send_message(
        uid,
        "Шаг 5/6. <b>Титры</b>\nВыберите вариант:",
        reply_markup=kb_titles()
    )

@bot.message_handler(func=lambda m: users.get(m.from_user.id, {}).get("await_titles_field") in {"fio","dates","mem"})
def on_titles_input(m: telebot.types.Message):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    stage = st.get("await_titles_field")

    if stage == "fio":
        st["titles_fio"] = (m.text or "").strip()
        st["await_titles_field"] = "dates"
        bot.send_message(uid, "Шаг 5/6 · Титры · 2/3\nПришлите <b>дату рождения — дату смерти</b> в формате <code>ДД.ММ.ГГГГ — ДД.ММ.ГГГГ</code>.")
        return

    if stage == "dates":
        txt = (m.text or "").strip()
        # принимаем дефис/тире; пробелы вокруг — допускаем
        if not re.match(r"^\d{2}\.\d{2}\.\d{4}\s*[—-]\s*\d{2}\.\d{2}\.\d{4}$", txt):
            bot.send_message(uid, "Пожалуйста, укажите даты в формате <code>ДД.ММ.ГГГГ — ДД.ММ.ГГГГ</code> (пример: 01.02.1950 — 03.04.2020).")
            return
        st["titles_dates"] = txt
        st["await_titles_field"] = "mem"
        bot.send_message(uid, "Шаг 5/6 · Титры · 3/3\nПришлите <b>памятную надпись</b>. Кратко, чтобы хорошо смотрелось в кадре.")
        return

    if stage == "mem":
        st["titles_text"] = (m.text or "").strip()
        st["await_titles_field"] = None
        bot.send_message(uid, "Титры сохранены ✅\nПереходим к шагу 6/6 — фото.")
        _init_scene_jobs(st)
        _ask_photos_for_current_scene(uid, st)

@bot.message_handler(commands=["cfg"])
def cmd_cfg(m: telebot.types.Message):
    uid = m.from_user.id
    if not _is_admin(uid):
        return bot.reply_to(m, "Недоступно")
    txt = (
        f"<b>Config</b>\n"
        f"PREVIEW_START_FRAME: {PREVIEW_START_FRAME}\n"
        f"DEBUG_TO_ADMIN: {DEBUG_TO_ADMIN}\n"
        f"RUNWAY_SEND_JPEG: {RUNWAY_SEND_JPEG}\n"
    )
    bot.reply_to(m, txt)

@bot.message_handler(commands=["preview_on", "preview_off"])
def cmd_preview(m: telebot.types.Message):
    uid = m.from_user.id
    if not _is_admin(uid):
        return bot.reply_to(m, "Недоступно")
    global PREVIEW_START_FRAME
    PREVIEW_START_FRAME = (m.text == "/preview_on")
    bot.reply_to(m, f"PREVIEW_START_FRAME = {PREVIEW_START_FRAME}")

@bot.message_handler(commands=["admdbg_on", "admdbg_off"])
def cmd_admdbg(m: telebot.types.Message):
    uid = m.from_user.id
    if not _is_admin(uid):
        return bot.reply_to(m, "Недоступно")
    global DEBUG_TO_ADMIN
    DEBUG_TO_ADMIN = (m.text == "/admdbg_on")
    bot.reply_to(m, f"DEBUG_TO_ADMIN = {DEBUG_TO_ADMIN}")

@bot.message_handler(commands=["jpeg_on", "jpeg_off"])
def cmd_jpeg(m: telebot.types.Message):
    uid = m.from_user.id
    if not _is_admin(uid):
        return bot.reply_to(m, "Недоступно")
    global RUNWAY_SEND_JPEG
    RUNWAY_SEND_JPEG = (m.text == "/jpeg_on")
    bot.reply_to(m, f"RUNWAY_SEND_JPEG = {RUNWAY_SEND_JPEG}")

# ---------- ЛЕГАЛ (Оферта/Политика/Согласие) ----------
OFFER_VERSION = "1.0"
OFFER_DATE    = "29.09.2025"
OFFER_VERSION_STR = f"v{OFFER_VERSION} от {OFFER_DATE}"
POLICY_VERSION   = OFFER_VERSION
POLICY_DATE      = OFFER_DATE
POLICY_VERSION_STR = f"v{POLICY_VERSION} от {POLICY_DATE}"

SHORT_OFFER_MSG = (
    "<b>КРАТКО: ДОГОВОР-ОФЕРТА</b>\n"
    "• Сервис «Memory Forever» генерирует видео с помощью ИИ из ваших фото/аудио.\n"
    "• <b>Важно</b>: результат синтетический, возможны художественные искажения; «полная схожесть» не гарантируется.\n"
    "• <b>Запрещено</b>: порнография, насилие, экстремизм, наркотики, чужие ПДн, нелицензированные музыка/изображения, выдача синтетики за реальную съёмку для обмана.\n"
    "• <b>Ответственность</b>: подтверждаете права на материалы и несёте ответственность за их использование.\n"
    "• <b>Оплата</b>: цены в боте. После запуска генерации возврата нет (цифровая услуга).\n"
    "• <b>Доставка</b>: готовое видео приходит в чат Telegram.\n"
    f"Версия оферты: {OFFER_VERSION_STR}"
)

SHORT_POLICY_MSG = (
    "<b>КРАТКО: ПОЛИТИКА И СОГЛАСИЕ</b>\n"
    "• Данные: Telegram ID и ник, загруженные фото/музыка/титры; тех.журналы, факт оплаты.\n"
    "• Цели: оказание услуги, поддержка, модерация, закон, улучшение сервиса.\n"
    "• Передача: подрядчикам — облака/ИИ-платформы/платежи.\n"
    "• Хранение: материалы — до завершения сессии, результаты — до звершения сессии; журналы — ≥1г.\n"
    "• Права: доступ/исправление/удаление/отзыв согласия (контакты — в полном тексте).\n"
    f"Версия политики: {OFFER_VERSION_STR}"
)

def _ensure_dir(p: str):
    try:
        os.makedirs(p, exist_ok=True)
    except Exception:
        pass

def _send_long_text(uid: int, title: str, text: str):
    """Безопасно шлём длинный текст чанками ≤ 3500 символов."""
    MAX = 3500
    if not text:
        bot.send_message(uid, f"{title}\nТекст недоступен.")
        return
    head = f"<b>{title}</b>\n"
    if len(head) + len(text) <= MAX:
        bot.send_message(uid, head + text)
        return
    chunks = []
    cur = text
    while cur:
        chunk = cur[:MAX]
        # стараемся резать по абзацам/точкам
        cut = max(chunk.rfind("\n\n"), chunk.rfind("\n"), chunk.rfind(". "))
        if cut > 800:
            chunk = cur[:cut+1]
        chunks.append(chunk)
        cur = cur[len(chunk):]
    bot.send_message(uid, head + chunks[0])
    for part in chunks[1:]:
        bot.send_message(uid, part)

def kb_legal_consent():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("📄 Договор-оферта (файл)",  callback_data="legal_offer_full"),
        telebot.types.InlineKeyboardButton("🔐 Политика данных (файл)", callback_data="legal_policy_full"),
    )
    kb.add(
        telebot.types.InlineKeyboardButton("✅ Согласен",       callback_data="legal_accept"),
        telebot.types.InlineKeyboardButton("🏠 В главное меню", callback_data="go_home"),
    )
    return kb

def send_legal_gate(uid: int):
    """Экран согласия перед запуском генерации."""
    txt = (
        "Перед запуском генерации необходимо подтвердить согласие с условиями.\n\n"
        f"{SHORT_OFFER_MSG}\n\n{SHORT_POLICY_MSG}\n\n"
        "Если согласны — нажмите «✅ Согласен»."
    )
    bot.send_message(uid, txt, reply_markup=kb_legal_consent())

def _legal_log_accept(uid: int, st: dict, call: telebot.types.CallbackQuery | None = None):
    """Логируем согласие в файл JSONL."""
    try:
        _ensure_dir("legal_logs")
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "uid": uid,
            "username": getattr(call.from_user, "username", None) if call else None,
            "first_name": getattr(call.from_user, "first_name", None) if call else None,
            "last_name": getattr(call.from_user, "last_name", None) if call else None,
            "offer_version": OFFER_VERSION_STR,
            "policy_version": POLICY_VERSION_STR,
        }
        with open("legal_logs/accept.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[LEGAL] log error: {e}")

# --- Хендлеры меню «Оферта/Политика» (кратко) ---
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_OFFER)
def on_menu_offer(m: telebot.types.Message):
    uid = m.from_user.id
    path = _find_legal_file(OFFER_FULL_BASENAME)
    if path:
        try:
            with open(path, "rb") as f:
                bot.send_document(
                    uid, f,
                    caption=f"Полный текст договора-оферты ({OFFER_VERSION_STR})"
                )
            return
        except Exception as e:
            print(f"[LEGAL] send offer file error: {e}")
    # Фолбэк: если файл не найден/не отправился — показываем кратко
    bot.send_message(
        uid,
        SHORT_OFFER_MSG + "\n\n(Полный текст не найден. Положите файл в assets/legal/offer_full.*)",
        reply_markup=kb_main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_POLICY)
def on_menu_policy(m: telebot.types.Message):
    uid = m.from_user.id
    path = _find_legal_file(POLICY_FULL_BASENAME)
    if path:
        try:
            with open(path, "rb") as f:
                bot.send_document(
                    uid, f,
                    caption=f"Полная политика и согласие ({OFFER_VERSION_STR})"
                )
            return
        except Exception as e:
            print(f"[LEGAL] send policy file error: {e}")
    # Фолбэк: если файл не найден/не отправился — показываем кратко
    bot.send_message(
        uid,
        SHORT_POLICY_MSG + "\n\n(Полный текст не найден. Положите файл в assets/legal/policy_full.*)",
        reply_markup=kb_main_menu()
    )

# --- Callback внутри экрана согласия ---
@bot.callback_query_handler(func=lambda call: call.data == "legal_offer")
def cb_legal_offer(call: telebot.types.CallbackQuery):
    bot.answer_callback_query(call.id)
    bot.send_message(call.from_user.id, SHORT_OFFER_MSG)

@bot.callback_query_handler(func=lambda call: call.data == "legal_policy")
def cb_legal_policy(call: telebot.types.CallbackQuery):
    bot.answer_callback_query(call.id)
    bot.send_message(call.from_user.id, SHORT_POLICY_MSG)

@bot.callback_query_handler(func=lambda call: call.data == "titles_none")
def cb_titles_none(call: telebot.types.CallbackQuery):
    uid = call.from_user.id
    st = users.setdefault(uid, new_state())
    st["titles_mode"] = "none"
    st["await_titles_field"] = None
    bot.answer_callback_query(call.id, "Без титров")
    # Сразу к шагу 6/6 — фото
    _init_scene_jobs(st)
    _ask_photos_for_current_scene(uid, st)

@bot.callback_query_handler(func=lambda call: call.data == "titles_custom")
def cb_titles_custom(call: telebot.types.CallbackQuery):
    uid = call.from_user.id
    st = users.setdefault(uid, new_state())
    st["titles_mode"] = "custom"
    st["titles_fio"] = None
    st["titles_dates"] = None
    st["titles_text"] = None
    st["await_titles_field"] = "fio"
    bot.answer_callback_query(call.id, "Добавляем титры (+50 ₽)")
    bot.send_message(uid, "Шаг 5/6 · Титры · 1/3\nПришлите <b>Ф.И.О. полностью</b> (как в титрах).")

@bot.callback_query_handler(func=lambda call: call.data == "legal_offer_full")
def cb_legal_offer_full(call: telebot.types.CallbackQuery):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    path = _find_legal_file(OFFER_FULL_BASENAME)
    if path:
        try:
            with open(path, "rb") as f:
                bot.send_document(uid, f, caption=f"Полный текст договора-оферты ({OFFER_VERSION_STR})")
        except Exception as e:
            bot.send_message(uid, f"Не удалось отправить файл оферты: {e}")
    else:
        bot.send_message(uid, "Полный текст оферты не найден. Положите файл в <code>assets/legal/offer_full.*</code>")

@bot.callback_query_handler(func=lambda call: call.data == "legal_policy_full")
def cb_legal_policy_full(call: telebot.types.CallbackQuery):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    path = _find_legal_file(POLICY_FULL_BASENAME)
    if path:
        try:
            with open(path, "rb") as f:
                bot.send_document(uid, f, caption=f"Полная политика и согласие ({OFFER_VERSION_STR})")
        except Exception as e:
            bot.send_message(uid, f"Не удалось отправить файл политики: {e}")
    else:
        bot.send_message(uid, "Полная политика не найдена. Положите файл в <code>assets/legal/policy_full.*</code>")

@bot.callback_query_handler(func=lambda call: call.data == "legal_accept")
def cb_legal_accept(call: telebot.types.CallbackQuery):
    uid = call.from_user.id
    st = users.setdefault(uid, new_state())
    st["offer_accepted"] = True
    st["offer_accepted_ver"] = OFFER_VERSION_STR
    _legal_log_accept(uid, st, call)
    bot.answer_callback_query(call.id, "Согласие зафиксировано")
    bot.send_message(uid, "Спасибо! Согласие получено. Продолжаю…")

    # Если уже утверждены старт-кадры по всем сюжетам — показываем счёт/рендерим,
    # иначе просто продолжаем сбор по сценарию (ничего больше не делаем здесь).
    jobs = st.get("scene_jobs") or []
    all_ready = jobs and all(j.get("start_frame") for j in jobs)

    if not all_ready:
        return

    if PAYMENT_GATE_ENABLED and not st.get("payment_confirmed"):
        st["await_payment"] = True
        send_payment_quote(uid, st)
        return

    _render_all_scenes_from_approved(uid, st)

# ---------- ПАЙПЛАЙН ----------
# === HARD-OFF for OpenAI Assistants (safe stub layer) =========================
# Отключаем любые проверки/добавки от Assistant'а и делаем функции-стабы.

try:
    ASSISTANT_GATE_ENABLED = False  # на всякий — принудительно OFF
except NameError:
    pass

def _short_gate(g: dict | None) -> str:  # используется в превью — оставим нейтральный вывод
    return "gate: disabled"

def _normalize_gate(g: dict | None) -> dict | None:
    return None

def oai_upload_image(path: str) -> str | None:
    # не загружаем ничего в Assistants
    return None

def oai_create_thread_with_image(user_text: str, file_id: str) -> str | None:
    # не создаём thread в Assistants
    return None

def oai_gate_check(start_frame_path: str, base_prompt: str, meta: dict, timeout_sec: int = 120) -> dict | None:
    # всегда «без вмешательства»: возвращаем None
    return None

# ==============================================================================
def _generate_scene_from_approved(uid: int, data: dict) -> str | None:
    """
    Запуск Runway для ОДНОЙ сцены по уже согласованному старт-кадру.
    Возвращает путь к СЫРОМУ видео-сегменту (без музыки/титров/WM), либо None при ошибке.
    """
    scene_key   = data["scene_key"]
    scene       = SCENES[scene_key]
    start_frame = data["start_frame"]
    prompt      = data["prompt"]
    duration    = int(data["duration"])

    # Safety: повторная проверка лимита перед запуском рендера
    if (scene_key == FREE_HUGS_SCENE
        and get_free_hugs_count(uid) >= FREE_HUGS_LIMIT
        and not is_free_hugs_whitelisted(uid)):
        try:
            bot.send_message(uid, "Вы уже использовали 2 бесплатные генерации по сюжету «Объятия 5с». "
                                  "Выберите платный сюжет.")
        except Exception:
            pass
        return None

    # Подготовка старт-кадра
    send_path = ensure_jpeg_copy(start_frame) if RUNWAY_SEND_JPEG else start_frame
    data_uri, used_path = ensure_runway_datauri_under_limit(send_path)
    try:
        fs = os.path.getsize(used_path)
        print(f"[Runway] start_frame path={used_path} size={fs} bytes (jpeg={RUNWAY_SEND_JPEG})")
    except Exception:
        pass
    if not data_uri or len(data_uri) < 64:
        bot.send_message(uid, f"Сцена «{scene_key}»: пустой data URI старт-кадра")
        return None

    # Запуск Runway
    try:
        start_resp = runway_start(data_uri, prompt, duration)
    except RuntimeError as e:
        bot.send_message(uid, f"Сцена «{scene_key}» упала с ошибкой: {e}")
        _log_fail(uid, "runway_start_failed_approved",
                  {"scene": scene_key, "prompt_len": len(prompt)}, str(e))
        return None

    task_id = start_resp.get("id") or start_resp.get("task", {}).get("id")
    if not task_id:
        bot.send_message(uid, f"Не получил id задачи от Runway для «{scene_key}».")
        _log_fail(uid, "no_task_id_approved", {"scene": scene_key, "prompt_len": len(prompt)}, start_resp)
        return None

    poll = runway_poll(task_id)
    status = (poll or {}).get("status")
    print(f"[Runway] Final status for {scene_key}: {status}")

    if status != "SUCCEEDED":
        err_txt = ""
        if isinstance(poll, dict):
            err_txt = poll.get("error") or poll.get("message") or poll.get("failure_reason") or ""

        # Специальные сообщения для разных типов ошибок
        if status == "TIMEOUT":
            bot.send_message(uid, f"Сцена «{scene_key}» превысила время ожидания (5 минут). Попробуйте позже или другие фото.")
        elif status == "NETWORK_ERROR":
            bot.send_message(uid, f"Сцена «{scene_key}» не удалась из-за проблем с сетью. Попробуйте позже.")
        elif err_txt:
            bot.send_message(uid, f"Сцена «{scene_key}» не удалась: {status}. {err_txt}")
        else:
            bot.send_message(uid, f"Сцена «{scene_key}» не удалась: {status}. Попробуйте другой фон или фото.")
        _log_fail(uid, "poll_failed_approved", {"scene": scene_key, "prompt_len": len(prompt)}, poll)
        return None

    out = poll.get("output") or []
    url = out[0] if isinstance(out[0], str) else (out[0].get("url") if out else None)
    if not url:
        bot.send_message(uid, f"Runway не вернул ссылку для «{scene_key}».")
        _log_fail(uid, "no_url_approved", {"scene": scene_key}, poll)
        return None

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    seg_path = f"renders/{uid}_{timestamp}_{uuid.uuid4().hex}.mp4"
    download(url, seg_path)

    # --- доп. полноэкранный водяной знак ТОЛЬКО для бесплатной сцены «Объятия 5с» ---
    try:
        if _is_free_hugs(scene_key) and FULL_WATERMARK_PATH and os.path.isfile(FULL_WATERMARK_PATH):
            wm_out = f"renders/{uid}_{timestamp}_{uuid.uuid4().hex}_WM.mp4"
            apply_fullscreen_watermark(
                in_video=seg_path,
                out_video=wm_out,
                wm_path=FULL_WATERMARK_PATH,
                mode=FREE_HUGS_WM_MODE,
                alpha=FREE_HUGS_WM_ALPHA,
            )
            seg_path = wm_out
    except Exception as e:
        print(f"[WM] fullscreen watermark failed: {e}")

    # Учитываем успешную бесплатную генерацию
    if scene_key == FREE_HUGS_SCENE and not is_free_hugs_whitelisted(uid):
        try:
            inc_free_hugs_count(uid)
            print(f"[QUOTA] FREE HUGS used: uid={uid} -> {get_free_hugs_count(uid)}")
        except Exception as e:
            print(f"[QUOTA] inc failed: {e}")

    return seg_path

def _render_all_scenes_from_approved(uid: int, st: dict):
    """
    Батч: пробегаем по ВСЕМ согласованным сценам и рендерим их по очереди.
    В конце вызываем финализацию (склейка+музыка+титр) и отправку.
    """
    if uid in IN_RENDER:
        try:
            bot.send_message(uid, "Уже идёт генерация видео…")
        except Exception:
            pass
        return

    IN_RENDER.add(uid)
    try:
        jobs = st.get("scene_jobs") or []
        if not jobs:
            bot.send_message(uid, "Нет сцен для генерации.")
            return

        total = len(jobs)
        for i, job in enumerate(jobs, start=1):
            # если уже есть сегмент (повторный запуск) — пропускаем
            if job.get("video_path"):
                continue

            sf = job.get("start_frame")
            if not sf or not os.path.isfile(sf):
                bot.send_message(uid, f"Сцена «{job.get('scene_key','?')}» не согласована — пропускаю.")
                continue

            data = {
                "scene_key": job["scene_key"],
                "start_frame": job["start_frame"],
                "prompt": job.get("prompt", ""),
                "duration": int(job.get("duration") or SCENES[job["scene_key"]]["duration"]),
            }

            try:
                bot.send_message(uid, f"Генерация {i}/{total}: «{job['scene_key']}»…")
            except Exception:
                pass

            seg_path = _generate_scene_from_approved(uid, data)
            if seg_path:
                job["video_path"] = seg_path
                print(f"[RENDER] Scene {i}/{total} completed: {job['scene_key']} -> {seg_path}")
            else:
                print(f"[RENDER] Scene {i}/{total} failed: {job['scene_key']}")
                try:
                    bot.send_message(uid, f"⚠️ Сцена «{job['scene_key']}» не получилась. "
                                          f"Попробуйте заменить фото или фон и повторите позже.")
                except Exception:
                    pass

        print(f"[RENDER] All scenes processed, calling _finalize_all_scenes_and_send")
        _finalize_all_scenes_and_send(uid, st)
    finally:
        IN_RENDER.discard(uid)

def _finalize_all_scenes_and_send(uid: int, st: dict):
    """Собирает все сегменты в порядке выбора, делает кроссфейды и постобработку, отправляет результат."""
    print(f"[FINALIZE] Starting finalization for uid={uid}")
    jobs = st.get("scene_jobs") or []
    segs = [j.get("video_path") for j in jobs if j.get("video_path")]
    print(f"[FINALIZE] Found {len(segs)} video segments: {segs}")
    if not segs:
        bot.send_message(uid, "Ни одна сцена не сгенерировалась. Попробуйте другие фото.")
        cleanup_user_custom_bg(uid)
        # удалить пользовательский трек, если был
        try:
            if st.get("custom_music_path") and os.path.isfile(st["custom_music_path"]):
                os.remove(st["custom_music_path"])
        except Exception:
            pass
        users[uid] = new_state()
        show_main_menu(uid)
        return

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    final_path = f"renders/{uid}_{timestamp}_{uuid.uuid4().hex}_FINAL.mp4"
    title_text = "Memory Forever — Память навсегда с вами"

    if st.get("music") == CUSTOM_MUSIC_KEY:
        music_path = st.get("custom_music_path")
    else:
        music_path = MUSIC.get(st["music"]) if st.get("music") else None
    bg_file = (st.get("bg_custom_path") if st.get("bg") == CUSTOM_BG_KEY else (BG_FILES[st["bg"]] if st.get("bg") else None))
    # Готовим метаданные для титра (если пользователь выбрал «Добавить титры»)
    if st.get("titles_mode") == "custom":
        titles_meta = {
            "fio": (st.get("titles_fio") or "").strip(),
            "dates": (st.get("titles_dates") or "").strip(),
            "mem": (st.get("titles_text") or "").strip(),
        }
    else:
        titles_meta = None

    print(f"[FINALIZE] Starting postprocess: music={music_path}, bg={bg_file}, titles={titles_meta}")
    try:
        # Внутри постпроцесса мы уже добавим титр/фон-анимацию/WM/музыку.
        postprocess_concat_ffmpeg(
            segs,
            music_path,
            title_text,
            final_path,
            bg_overlay_file=bg_file,
            titles_meta=titles_meta,
            candle_path=CANDLE_PATH
        )
        print(f"[FINALIZE] Postprocess completed successfully: {final_path}")
    except Exception as e:
        print(f"Postprocess error (final): {e}")
        bot.send_message(uid, f"Постобработка не удалась ({e}). Шлю сырые сцены по отдельности.")
        for i, p in enumerate(segs, 1):
            try:
                with open(p, "rb") as f:
                    bot.send_video(uid, f, caption=f"Сцена {i}")
            except Exception:
                pass
        cleanup_artifacts(keep_last=20)
        cleanup_user_custom_bg(uid)
        # удалить пользовательский трек, если был
        try:
            if st.get("custom_music_path") and os.path.isfile(st["custom_music_path"]):
                os.remove(st["custom_music_path"])
        except Exception:
            pass
        users[uid] = new_state()
        show_main_menu(uid, "Готово! Видео (без постобработки) отправлены.")
        return

    try:
        _order_log_success(uid, st, final_path)
    except Exception as e:
        print(f"[ORDERLOG] write error: {e}")

    # Уведомление в техподдержку об успешной генерации (если задан ADMIN_CHAT_ID)
    if ADMIN_CHAT_ID:
        try:
            scenes_txt = " · ".join(st.get("scenes") or [])
            fmt_txt = st.get("format") or "—"
            dur = None
            try:
                dur = _video_duration_sec(final_path)
            except Exception:
                pass
            sz = None
            try:
                sz = os.path.getsize(final_path)
            except Exception:
                pass
            meta = []
            if dur is not None:
                meta.append(f"dur={int(dur)}s")
            if sz is not None:
                meta.append(f"size={sz//1024}KB")
            meta_str = (" · ".join(meta)) if meta else ""
            bot.send_message(int(ADMIN_CHAT_ID), (
                "✅ Успешная генерация\n"
                f"uid: {uid}\n"
                f"format: {fmt_txt}\n"
                f"scenes: {scenes_txt}\n"
                f"file: {final_path}\n"
                f"{meta_str}"
            ).strip())
        except Exception as e:
            print(f"[ADMIN_NOTIFY] send success msg failed: {e}")

    with open(final_path, "rb") as f:
        cap = " · ".join(st["scenes"]) + f" · {st['format']}"
        bot.send_video(uid, f, caption=cap)

    cleanup_artifacts(keep_last=20)
    cleanup_user_custom_bg(uid)
    # удалить пользовательский трек, если был
    try:
        if st.get("custom_music_path") and os.path.isfile(st["custom_music_path"]):
            os.remove(st["custom_music_path"])
    except Exception:
        pass
    users[uid] = new_state()
    show_main_menu(uid, "Готово! Видео создано успешно.")

def _init_scene_jobs(st: dict):
    """Строит очередь сцен: по каждой — метаданные и пустые контейнеры под фото/видео."""
    st["scene_jobs"] = []
    for k in st["scenes"]:
        st["scene_jobs"].append({
            "scene_key": k,
            "people": SCENES[k]["people"],
            "photos": [],
            "start_frame": None,
            "duration": SCENES[k]["duration"],
            "prompt": SCENE_PROMPTS.get(SCENES[k]["kind"], ""),
            "video_path": None,
        })
    st["scene_idx"] = 0

def _ask_photos_for_current_scene(uid: int, st: dict):
    """Просит фото под ТЕКУЩИЙ (scene_idx) сюжет."""
    idx = st.get("scene_idx", 0)
    jobs = st.get("scene_jobs") or []
    if idx >= len(jobs):
        return
    job = jobs[idx]
    need = job["people"]
    name = job["scene_key"]
    bot.send_message(uid, f"Шаг 6/6. Сюжет {idx+1}/{len(jobs)}: <b>{name}</b>\nПришлите {need} фото (анфас).")

def _order_log_success(uid: int, st: dict, final_video_path: str, extras: dict | None = None):
    """
    Логируем УДАЧНУЮ генерацию в JSONL: orders_logs/generations.jsonl
    """
    try:
        os.makedirs("orders_logs", exist_ok=True)

        # Подтянем ФИО/username (если получится)
        username = first_name = last_name = None
        try:
            ch = bot.get_chat(uid)
            username   = getattr(ch, "username", None)
            first_name = getattr(ch, "first_name", None)
            last_name  = getattr(ch, "last_name", None)
        except Exception:
            pass

        # Цены и опции
        total_rub, br = calc_order_price(st)
        stars, _ = stars_amount_for_state(st)

        # Текущий платёжный контекст (если был)
        pay_kind = st.get("payment_kind")              # "stars" | "tochka" | None
        pay_id   = st.get("payment_op_id")             # op_id Точки или наш stars_*
        pay_ok   = bool(st.get("payment_confirmed"))   # факт подтверждения

        # Файл и длительность (если получится)
        size_b = None
        duration_s = None
        try:
            size_b = os.path.getsize(final_video_path)
        except Exception:
            pass
        try:
            duration_s = _video_duration_sec(final_video_path)
        except Exception:
            pass

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "uid": uid,
            "username": username,
            "first_name": first_name,
            "last_name": last_name,

            # Выбор пользователя
            "format": st.get("format"),
            "scenes": list(st.get("scenes") or []),
            "bg": st.get("bg"),
            "bg_is_custom": bool(st.get("bg") == "__CUSTOM__" and st.get("bg_custom_path")),
            "music": st.get("music"),
            "music_is_custom": bool(st.get("music") == "🎵 Свой трек" and st.get("custom_music_path")),
            "titles_mode": st.get("titles_mode"),
            "titles_fio": st.get("titles_fio") if st.get("titles_mode") == "custom" else None,
            "titles_dates": st.get("titles_dates") if st.get("titles_mode") == "custom" else None,

            # Деньги
            "price_total_rub": total_rub,
            "price_breakdown": br,    # {scenes:[(name,price)], options:[(label,price)]}
            "stars_quote": stars,     # сколько ⭐ запрашивали бы за этот заказ

            # Платёж
            "payment_kind": pay_kind,
            "payment_id": pay_id,
            "payment_confirmed": pay_ok,

            # Результат
            "video_path": final_video_path,
            "video_size_bytes": size_b,
            "video_duration_sec": duration_s,

            # Прочее
            "free_hugs_count_used": get_free_hugs_count(uid),
        }

        if extras:
            payload.update(extras)

        with open("orders_logs/generations.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[ORDERLOG] write error: {e}")

def _prepare_start_for_scene_and_ask_approval(uid: int, st: dict, scene_idx: int):
    """Генерит старт-кадр по фото текущего сюжета, показывает превью с кнопками, кладёт await_approval."""
    jobs = st.get("scene_jobs") or []
    job = jobs[scene_idx]
    bg_file = (st.get("bg_custom_path") if st.get("bg") == CUSTOM_BG_KEY else BG_FILES[st["bg"]])
    start_frame, layout_metrics = make_start_frame(job["photos"], st["format"], bg_file, layout=None)

    warn_txt = ""
    if "L" in layout_metrics and "R" in layout_metrics:
        wL = max(1, int(layout_metrics["L"]["width_px"]))
        wR = max(1, int(layout_metrics["R"]["width_px"]))
        ratio = (max(wL, wR) / max(1, min(wL, wR)))
        if ratio >= PAIR_WIDTH_WARN_RATIO:
            pct = int(round((ratio - 1.0) * 100))
            warn_txt = (
                f"⚠️ Ширина фигур сильно отличается (~{pct}%). "
                "Из-за этого обе будут меньше по высоте в кадре.\n"
                "Рекомендуем прислать другие фото, где люди примерно одинаковой ширины по плечам/рукам, "
                "без широко разведённых локтей, анфас."
            )

    bg_disp = "Пользовательский фон" if st.get("bg") == CUSTOM_BG_KEY else st["bg"]
    cap = (
        f"Предпросмотр старт-кадра → {job['scene_key']}  ({scene_idx+1}/{len(jobs)})\n"
        f"Формат: {st['format']}  ·  Фон: {bg_disp}\n"
        + (warn_txt + "\n" if warn_txt else "")
        + "Нажмите «Согласовать» или «Заменить фото»."
    )
    try:
        with open(start_frame, "rb") as ph:
            bot.send_photo(uid, ph, caption=cap, reply_markup=kb_start_approval())
    except Exception as _e:
        print(f"[DBG] preview send err: {_e}")

    # сохраняем контекст для approve/reject
    st["await_approval"] = {
        "scene_idx": scene_idx,
        "scene_key": job["scene_key"],
        "format": st["format"],
        "bg_file": bg_file,
        "music_path": (
            st.get("custom_music_path")
            if st.get("music") == CUSTOM_MUSIC_KEY
            else (MUSIC.get(st["music"]) if st.get("music") else None)
        ),
        "start_frame": start_frame,
        "prompt": job["prompt"],
        "duration": job["duration"],
    }

# ---------- ОБРАБОТЧИКИ CALLBACK-КНОПОК МУЗЫКИ ----------
@bot.callback_query_handler(func=lambda call: call.data.startswith("listen_"))
def on_music_listen(call):
    uid = call.from_user.id
    music_name = call.data.replace("listen_", "")
    music_path = MUSIC_BY_CLEAN.get(music_name)   # ← без find_music_by_name

    if music_path and os.path.isfile(music_path):
        try:
            with open(music_path, 'rb') as audio:
                bot.send_audio(uid, audio, title=music_name, performer="Memory Forever")
            bot.answer_callback_query(call.id, f"🎧 Воспроизводится: {music_name}")
        except Exception as e:
            bot.answer_callback_query(call.id, f"Ошибка при отправке аудио: {e}")
    else:
        bot.answer_callback_query(call.id, "Файл не найден")

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_music_"))
def on_music_select(call):
    uid = call.from_user.id
    st = users.setdefault(uid, new_state())

    music_choice = call.data.replace("select_music_", "")

    if music_choice == "none":
        st["music"] = None
        bot.answer_callback_query(call.id, "🔇 Выбрано: Без музыки")
    else:
        if music_choice in MUSIC_BY_CLEAN:
            # если раньше загружали свой трек — удалим файл и очистим указатель
            if st.get("custom_music_path"):
                try:
                    os.remove(st["custom_music_path"])
                except Exception:
                    pass
                st["custom_music_path"] = None
            st["await_custom_music"] = False
            st["music"] = f"🎵 {music_choice}"        # храним ключ, как в меню
            bot.answer_callback_query(call.id, f"✅ Выбрано: {music_choice}")
        else:
            bot.answer_callback_query(call.id, "Музыка не найдена")
            return

    if not st["scenes"]:
        bot.send_message(uid, "Ошибка: не выбраны сюжеты. Начните с /start")
        return

    # Переходим к шагу 5/6: Титры
    st["titles_mode"] = "none"
    st["await_titles_field"] = None
    bot.send_message(
        uid,
        "Шаг 5/6. <b>Титры</b>\nВыберите вариант:",
        reply_markup=kb_titles()
    )

@bot.callback_query_handler(func=lambda call: call.data == "upload_music")
def on_upload_music(call):
    uid = call.from_user.id
    st = users.setdefault(uid, new_state())
    st["await_custom_music"] = True
    bot.answer_callback_query(call.id, "Загрузка трека")
    bot.send_message(
        uid,
        "Пришлите аудиофайл (mp3, m4a, wav, ogg и т.п.). "
        "После загрузки перейдём к следующему шагу."
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("preview_bg_"))
def on_bg_preview(call):
    uid = call.from_user.id
    clean = call.data.replace("preview_bg_", "", 1)
    orig = _bg_orig_from_clean(clean)
    if not orig:
        return bot.answer_callback_query(call.id, "Фон не найден")
    path = BG_FILES[orig]
    try:
        with open(path, "rb") as ph:
            bot.send_photo(uid, ph, caption=f"Предпросмотр фона: {orig}")
        bot.answer_callback_query(call.id, "Открыт предпросмотр")
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка предпросмотра: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_bg_"))
def on_bg_select(call):
    uid = call.from_user.id
    st = users.setdefault(uid, new_state())
    clean = call.data.replace("select_bg_", "", 1)
    orig = _bg_orig_from_clean(clean)
    if not orig:
        return bot.answer_callback_query(call.id, "Фон не найден")

    st["bg"] = orig
    st["await_custom_bg"] = False
    st["bg_custom_path"] = None

    bot.answer_callback_query(call.id, f"Выбрано: {orig}")
    bot.send_message(uid, "Шаг 4/6. Выберите ✅ <b>музыку</b>. Можно предварительно 🎧 прослушать. Или загрузите свой трек.", reply_markup=kb_music())

@bot.callback_query_handler(func=lambda c: c.data in {"pay_now","pay_tochka"} or c.data.startswith("checkpay_"))
def on_payment_callbacks(call: telebot.types.CallbackQuery):
    uid = call.from_user.id
    st  = users.setdefault(uid, new_state())

    # 1) нажали «Оплатить»
    if call.data == "pay_now":
        total, _ = calc_order_price(st)
        if total <= 0:
            bot.answer_callback_query(call.id, "Оплата не требуется")
            # уберём старые кнопки из сообщения с «Итог к оплате», если жали оттуда
            try:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=None
                )
            except Exception:
                pass
            st["await_payment"] = False
            st["payment_confirmed"] = True
            bot.send_message(uid, "Стоимость 0 ₽ — оплата не требуется. Продолжаем ✅")
            _after_payment_continue(uid, st)
            return

        # как было: показать выбор способов оплаты
        bot.answer_callback_query(call.id)
        bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                      message_id=call.message.message_id,
                                      reply_markup=None)
        bot.send_message(uid, "Выберите способ оплаты:", reply_markup=kb_payment_methods())
        return

    # 2) Точка — создаём платёжную ссылку
    if call.data == "pay_tochka":
        bot.answer_callback_query(call.id)
        total, br = calc_order_price(st)
        if total <= 0:
            st["payment_confirmed"] = True
            bot.send_message(uid, "Стоимость 0 ₽ — оплата не требуется. Продолжаем ✅")
            _after_payment_continue(uid, st)   # см. функцию ниже
            return
        purpose = "Оплата Memory Forever — видео"
        try:
            op_id, link = tochka_create_payment_link(total, purpose)
        except Exception as e:
            bot.send_message(uid, f"Не удалось создать ссылку на оплату: {e}")
            return
        st["await_payment"]  = True
        st["payment_op_id"]  = op_id
        st["payment_link"]   = link
        bot.send_message(uid,
            f"Счёт на <b>{total} ₽</b> создан.\n"
            f"Нажмите «Открыть платёж» и оплатите картой или через СБП.\n"
            f"После оплаты — жмите «Проверить».",
            reply_markup=kb_tochka_link(op_id, link)
        )
        _start_auto_check_payment(uid, op_id)
        return

    # 4) Проверка оплаты (жмут после оплаты)
    if call.data.startswith("checkpay_"):
        op_id = call.data.split("_", 1)[1]
        bot.answer_callback_query(call.id, "Проверяю оплату…")
        try:
            resp = tochka_get_payment_status(op_id)
        except Exception as e:
            bot.send_message(uid, f"Ошибка проверки: {e}")
            return
        if _is_paid_status(resp):
            st["payment_confirmed"] = True
            st["await_payment"] = False
            bot.send_message(uid, "✅ Оплата получена. Запускаю генерацию.")
            _after_payment_continue(uid, st)
        else:
            bot.send_message(uid, "Пока оплата не найдена. Если уже оплатили — подождите 5–10 секунд и нажмите «Проверить» ещё раз.")

def _after_payment_continue(uid: int, st: dict):
    """
    Продолжаем пайплайн сразу после подтверждения оплаты:
    - если есть несогласованные сюжеты — просим их завершить;
    - если оферта ещё не принята — показываем экран согласия;
    - иначе — запускаем рендер всех согласованных сцен.
    """
    try:
        jobs = st.get("scene_jobs") or []
        all_ready = jobs and all(j.get("start_frame") for j in jobs)
        if not all_ready:
            bot.send_message(uid, "Оплата получена. Завершите согласование старт-кадров по всем сюжетам — и я запущу генерацию.")
            return

        if not st.get("offer_accepted"):
            send_legal_gate(uid)
            return

        _render_all_scenes_from_approved(uid, st)
    except Exception as e:
        print(f"[PAY] after-payment continue err: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "pay_stars")
def on_pay_stars(call: telebot.types.CallbackQuery):
    uid = call.from_user.id
    st  = users.setdefault(uid, new_state())

    # Если ещё не показывали счёт — покажем (на всякий случай)
    if not st.get("await_payment"):
        st["await_payment"] = True

    stars, total_rub = stars_amount_for_state(st)
    if total_rub <= 0 or stars <= 0:
        bot.answer_callback_query(call.id, "Оплата не требуется")
        st["await_payment"] = False
        st["payment_confirmed"] = True
        bot.send_message(uid, "Стоимость 0 ₽ — оплата не требуется. Продолжаем ✅")
        _after_payment_continue(uid, st)
        return
    op_id = f"stars_{uuid4().hex}"          # свой ID операции для трекинга
    st["payment_op_id"] = op_id
    st["payment_kind"]  = "stars"

    title = "Оплата заказа • Memory Forever"
    # описание не длиннее 255 символов — делай кратко
    description = f"Итог {total_rub} ₽ • Оплата в Telegram Stars: {stars}⭐"

    # по требованиям Stars: currency='XTR', provider_token='' (пустая строка), ROVNO ОДНА price-позиция
    prices = [LabeledPrice(label=f"{stars}⭐", amount=stars)]

    payload = json.dumps({
        "kind": "stars",
        "uid": uid,
        "op_id": op_id,
        "rub": total_rub,
        "stars": stars
    }, ensure_ascii=False)

    try:
        msg = bot.send_invoice(
            chat_id=uid,
            title=title,
            description=description,
            invoice_payload=payload,
            provider_token="",   # <— для Stars токен НЕ нужен
            currency="XTR",
            prices=prices,
            need_email=False,
            need_name=False,
            need_phone_number=False,
            is_flexible=False
        )
        st["stars_invoice_msg_id"] = getattr(msg, "message_id", None)
        bot.answer_callback_query(call.id)  # уберем «часики»
    except Exception as e:
        st["await_payment"] = False
        bot.answer_callback_query(call.id, "Не удалось создать счёт", show_alert=True)
        bot.send_message(uid, f"Ошибка создания счёта Stars: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "upload_bg")
def on_bg_upload(call):
    uid = call.from_user.id
    st = users.setdefault(uid, new_state())
    st["await_custom_bg"] = True
    bot.answer_callback_query(call.id, "Загрузка своего фона")
    bot.send_message(uid, "Пришлите фото <b>своего фона</b> (лучше вертикальное 9:16). После загрузки перейдём к выбору музыки.")

@bot.callback_query_handler(func=lambda call: call.data == "approve_start")
def on_approve_start(call):
    # убрать инлайн-кнопки у старт-кадра
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    uid = call.from_user.id
    st  = users.setdefault(uid, new_state())

    data = st.get("await_approval")
    if not data:
        bot.answer_callback_query(call.id, "Нет старт-кадра для согласования")
        return

    # убрать кнопки у превью (если ещё есть)
    try:
        bot.edit_message_reply_markup(chat_id=uid, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Согласовано ✅")

    # --- зафиксировать старт-кадр ТЕКУЩЕГО сюжета (без рендера) ---
    idx  = int(data.get("scene_idx", st.get("scene_idx", 0)))
    jobs = st.get("scene_jobs") or []
    if idx < len(jobs):
        jobs[idx]["start_frame"] = data.get("start_frame")
        jobs[idx]["prompt"]      = data.get("prompt", jobs[idx].get("prompt"))
        jobs[idx]["duration"]    = int(data.get("duration", jobs[idx].get("duration", 0)))

    st["await_approval"] = None  # очищаем контекст согласования

    # --- если есть следующий сюжет — запрашиваем его фото и выходим ---
    if idx + 1 < len(jobs):
        st["scene_idx"] = idx + 1
        # чистим буферы альбомов этого юзера
        for k, rec in list(PENDING_ALBUMS.items()):
            if rec.get("uid") == uid:
                PENDING_ALBUMS.pop(k, None)
        _ask_photos_for_current_scene(uid, st)
        return

    # --- это был последний сюжет: оферта → счёт → рендер ---
    # (лимит бесплатных проверим позже, на запуске генерации; здесь ничего не рендерим)
    if not st.get("offer_accepted"):
        send_legal_gate(uid)
        return

    # если включён paygate и оплаты ещё нет — показываем счёт (БЕЗ «Выберите способ оплаты» тут)
    if PAYMENT_GATE_ENABLED and not st.get("payment_confirmed"):
        st["await_payment"] = True
        send_payment_quote(uid, st)  # кнопка «Оплатить» → выбор способа уже в on_payment_callbacks
        return

    _render_all_scenes_from_approved(uid, st)

@bot.callback_query_handler(func=lambda call: call.data == "reject_start")
def on_reject_start(call):
    uid = call.from_user.id
    st = users.setdefault(uid, new_state())

    data = st.get("await_approval")
    st["await_approval"] = None

    # определить индекс сюжета
    idx = st.get("scene_idx", 0)
    if data and isinstance(data, dict) and isinstance(data.get("scene_idx"), int):
        idx = data["scene_idx"]

    jobs = st.get("scene_jobs") or []
    if idx < len(jobs):
        jobs[idx]["photos"] = []
        jobs[idx]["start_frame"] = None

    # чистим буфер альбомов этого юзера
    for k, rec in list(PENDING_ALBUMS.items()):
        if rec.get("uid") == uid:
            PENDING_ALBUMS.pop(k, None)

    need_people = 1
    scene_name = "?"
    if idx < len(jobs):
        need_people = jobs[idx].get("people", 1)
        scene_name = jobs[idx].get("scene_key", "?")

    try:
        bot.answer_callback_query(call.id, "Ок, заменим фото")
    except Exception:
        pass

    bot.send_message(uid, f"Пожалуйста, пришлите {need_people} фото (анфас) для сюжета «{scene_name}».")

@bot.callback_query_handler(func=lambda call: call.data == "pay_cancel")
def on_pay_cancel(call):
    uid = call.from_user.id
    st = users.setdefault(uid, new_state())

    # закрываем всплывашку
    try:
        bot.answer_callback_query(call.id, "Отменено")
    except Exception:
        pass

    # гасим автопроверку и чистим id операции
    st["await_payment"] = False
    st["payment_op_id"] = None
    st["payment_kind"]  = None              # <— ДОБАВЬ
    st["stars_invoice_msg_id"] = None       # <— если сохраняешь id счета Stars
    st["payment_confirmed"] = False

    # убираем кнопки у сообщения со счётом (если оно ещё есть)
    try:
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None
        )
    except Exception:
        pass

    # возвращаем в главное меню
    show_main_menu(uid, "Оплата отменена. Вы в главном меню.")

@bot.callback_query_handler(func=lambda call: call.data == "go_home")
def on_go_home_callback(call):
    """Обработчик кнопки 'В главное меню' из inline-клавиатуры"""
    uid = call.from_user.id
    bot.answer_callback_query(call.id, "🏠 Переход в главное меню")
    show_main_menu(uid)

@bot.message_handler(func=lambda m: (m.content_type=="text") and m.text and not m.text.startswith("/"))
def fallback_text(m: telebot.types.Message):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())

    # Если ждём сообщение для поддержки — пересылаем админу и выходим в меню
    if st.get("support"):
        if ADMIN_CHAT_ID:
            # сначала пробуем форвард
            ok = True
            try:
                bot.forward_message(int(ADMIN_CHAT_ID), uid, m.message_id)
            except Exception:
                ok = False
            # если не получилось форвардом — отправим как текст
            if not ok:
                uname = (m.from_user.username or "")
                header = f"Сообщение в поддержку от @{uname} (id {uid}):"
                bot.send_message(int(ADMIN_CHAT_ID), f"{header}\n\n{m.text}")
        else:
            bot.send_message(uid, "Адрес поддержки не настроен. Укажите ADMIN_CHAT_ID в Secrets.")

        st["support"] = False
        show_main_menu(uid, "Спасибо! Сообщение передано. Мы свяжемся с вами.")
        return

    # Иначе — вежливый намёк, что надо пользоваться кнопками
    # (ничего не ломаем, просто показываем меню)
    show_main_menu(uid, "Пожалуйста, используйте кнопки ниже.")

# ---------- RUN ----------
if __name__ == "__main__":
    # Отключаем webhook перед запуском polling
    try:
        bot.remove_webhook()
    except Exception as e:
        print(f"Webhook removal warning: {e}")

    print("Memory Forever v0.4 started.")

    bot.infinity_polling(skip_pending=True, timeout=60) 