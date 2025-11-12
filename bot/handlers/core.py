from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import subprocess
import textwrap
import threading
import time
import uuid
from uuid import uuid4
from datetime import datetime, timezone
from typing import List

import numpy as np
import requests
import telebot
from telebot.types import LabeledPrice

from ..app import bot
from .. import config
from .. import assets
from ..state import (
    users,
    IN_RENDER,
    PENDING_ALBUMS,
    new_state,
    is_free_hugs_whitelisted,
    inc_free_hugs_count,
    get_free_hugs_count,
    is_free_hugs,
    is_admin,
)
from ..payment import (
    calc_order_price,
    payment_methods_keyboard,
    send_payment_quote,
    start_auto_check_payment,
    tochka_link_keyboard,
    stars_amount_for_state,
)
from ..payment import tochka
from ..render.pipeline import (
    validate_photo,
    ensure_jpeg_copy,
    ensure_runway_datauri_under_limit,
    runway_start,
    runway_poll,
    download,
    _video_duration_sec,
    apply_fullscreen_watermark,
    _log_fail,
    make_start_frame,
    postprocess_concat_ffmpeg,
    cleanup_artifacts,
)
from ..utils import cleanup_uploads_folder

SCENES = assets.SCENES
FORMATS = assets.FORMATS
BACKGROUNDS = assets.BACKGROUNDS
BG_FILES = assets.BG_FILES
BG_BY_CLEAN = assets.BG_BY_CLEAN
CUSTOM_BG_KEY = assets.CUSTOM_BG_KEY
MUSIC = assets.MUSIC
MUSIC_BY_CLEAN = assets.MUSIC_BY_CLEAN
CUSTOM_MUSIC_KEY = assets.CUSTOM_MUSIC_KEY
ALLOWED_AUDIO_EXTS = assets.ALLOWED_AUDIO_EXTS
SCENE_PROMPTS = assets.SCENE_PROMPTS
original_bg_from_clean = assets.original_bg_from_clean
cleanup_user_custom_bg = assets.cleanup_user_custom_bg

ADMIN_CHAT_ID = config.ADMIN_CHAT_ID
PREVIEW_START_FRAME = config.PREVIEW_START_FRAME
DEBUG_TO_ADMIN = config.DEBUG_TO_ADMIN
RUNWAY_SEND_JPEG = config.RUNWAY_SEND_JPEG
START_OVERLAY_DEBUG = config.START_OVERLAY_DEBUG
MF_DEBUG = config.MF_DEBUG
CROSSFADE_SEC = config.CROSSFADE_SEC
CANDLE_WIDTH_FRAC = config.CANDLE_WIDTH_FRAC
MEM_TOP_FRAC = config.MEM_TOP_FRAC
WM_CORNER_WIDTH_PX = config.WM_CORNER_WIDTH_PX
WM_CORNER_MARGIN_PX = config.WM_CORNER_MARGIN_PX
GUIDE_VIDEO_PATH = config.GUIDE_VIDEO_PATH
WATERMARK_PATH = config.WATERMARK_PATH
CANDLE_PATH = config.CANDLE_PATH
FREE_HUGS_SCENE = config.FREE_HUGS_SCENE
FREE_HUGS_LIMIT = config.FREE_HUGS_LIMIT
PAYMENT_GATE_ENABLED = config.PAYMENT_GATE_ENABLED
ASSISTANT_GATE_ENABLED = False
START_OVERLAY_DEBUG = False
FULL_WATERMARK_PATH = config.FULL_WATERMARK_PATH
FREE_HUGS_WM_MODE = config.FREE_HUGS_WM_MODE
FREE_HUGS_WM_ALPHA = config.FREE_HUGS_WM_ALPHA
FREE_HUGS_WM_SCALE = config.FREE_HUGS_WM_SCALE
FREE_HUGS_WM_ROTATE = config.FREE_HUGS_WM_ROTATE
FREE_HUGS_WM_GRID_COLS = config.FREE_HUGS_WM_GRID_COLS
FREE_HUGS_WM_GRID_ROWS = config.FREE_HUGS_WM_GRID_ROWS
FREE_HUGS_WM_GRID_MARGIN = config.FREE_HUGS_WM_GRID_MARGIN
TG_TOKEN = config.settings.telegram_bot_token
PAIR_WIDTH_WARN_RATIO = config.PAIR_WIDTH_WARN_RATIO

SINGLE_ALBUM_REJECTED: set[str] = set()

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
    if is_free_hugs(scene_key):
        return False
    meta = SCENES.get(scene_key, {})
    return int(meta.get("duration", 0)) >= 10

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
    if not is_admin(uid):
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
    if not is_admin(uid):
        return bot.reply_to(m, "Недоступно")
    global PREVIEW_START_FRAME
    PREVIEW_START_FRAME = (m.text == "/preview_on")
    bot.reply_to(m, f"PREVIEW_START_FRAME = {PREVIEW_START_FRAME}")

@bot.message_handler(commands=["admdbg_on", "admdbg_off"])
def cmd_admdbg(m: telebot.types.Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return bot.reply_to(m, "Недоступно")
    global DEBUG_TO_ADMIN
    DEBUG_TO_ADMIN = (m.text == "/admdbg_on")
    bot.reply_to(m, f"DEBUG_TO_ADMIN = {DEBUG_TO_ADMIN}")

@bot.message_handler(commands=["jpeg_on", "jpeg_off"])
def cmd_jpeg(m: telebot.types.Message):
    uid = m.from_user.id
    if not is_admin(uid):
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
        send_payment_quote(bot, uid, st, _after_payment_continue)
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
        if is_free_hugs(scene_key) and FULL_WATERMARK_PATH and os.path.isfile(FULL_WATERMARK_PATH):
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
    orig = original_bg_from_clean(clean)
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
    orig = original_bg_from_clean(clean)
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
        total, _ = calc_order_price(st)
        if total <= 0:
            st["payment_confirmed"] = True
            bot.send_message(uid, "Стоимость 0 ₽ — оплата не требуется. Продолжаем ✅")
            _after_payment_continue(uid, st)   # см. функцию ниже
            return
        purpose = "Оплата Memory Forever — видео"
        try:
            op_id, link = tochka.create_payment_link(total, purpose)
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
            reply_markup=tochka_link_keyboard(op_id, link)
        )
        start_auto_check_payment(bot, uid, op_id, _after_payment_continue)
        return

    # 4) Проверка оплаты (жмут после оплаты)
    if call.data.startswith("checkpay_"):
        op_id = call.data.split("_", 1)[1]
        bot.answer_callback_query(call.id, "Проверяю оплату…")
        try:
            resp = tochka.get_payment_status(op_id)
        except Exception as e:
            bot.send_message(uid, f"Ошибка проверки: {e}")
            return
        if tochka.is_paid_status(resp):
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
        send_payment_quote(bot, uid, st, _after_payment_continue)  # кнопка «Оплатить» → выбор способа уже в on_payment_callbacks
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
