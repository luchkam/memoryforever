from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOTENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(DOTENV_FILE, override=False)


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, '1' if default else '0') == '1'


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    runway_api_key: str
    tochka_jwt: str
    tochka_customer_code: str
    tochka_merchant_id: str
    tochka_ok_url: str
    tochka_fail_url: str
    tochka_api_base: str = 'https://enter.tochka.com/uapi/acquiring/v1.0'


settings = Settings(
    telegram_bot_token=os.environ.get('TELEGRAM_BOT_TOKEN', ''),
    runway_api_key=os.environ.get('RUNWAY_API_KEY', ''),
    tochka_jwt=os.environ.get('TOCHKA_JWT', ''),
    tochka_customer_code=os.environ.get('TOCHKA_CUSTOMER_CODE', ''),
    tochka_merchant_id=os.environ.get('TOCHKA_MERCHANT_ID', ''),
    tochka_ok_url=os.environ.get('TOCHKA_OK_URL', 'https://api.memoryforever.ru/ok'),
    tochka_fail_url=os.environ.get(
        'TOCHKA_FAIL_URL',
        os.environ.get('TOCHKA_OK_URL', 'https://api.memoryforever.ru/ok'),
    ),
)

RUNWAY_KEY = settings.runway_api_key  # legacy alias used across render pipeline

OAI_DEBUG = os.environ.get('OAI_DEBUG', '1') == '1'
PREVIEW_START_FRAME = os.environ.get('PREVIEW_START_FRAME', '0') == '1'
DEBUG_TO_ADMIN = os.environ.get('DEBUG_TO_ADMIN', '1') == '1'
RUNWAY_SEND_JPEG = os.environ.get('RUNWAY_SEND_JPEG', '1') == '1'
START_OVERLAY_DEBUG = os.environ.get('START_OVERLAY_DEBUG', '0') == '1'
MF_DEBUG = OAI_DEBUG or (os.environ.get('MF_DEBUG', '0') == '1')
CROSSFADE_SEC = _env_float('CROSSFADE_SEC', 0.7)

CANDLE_WIDTH_FRAC = _env_float('CANDLE_WIDTH_FRAC', 0.32)
MEM_TOP_FRAC = _env_float('MEM_TOP_FRAC', 0.48)
WM_CORNER_WIDTH_PX = _env_int('WM_CORNER_WIDTH_PX', 120)
WM_CORNER_MARGIN_PX = _env_int('WM_CORNER_MARGIN_PX', 24)

UPLOADS_DIR = 'uploads'
RENDERS_DIR = 'renders'
ASSETS_DIR = 'assets'
AUDIO_DIR = 'audio'
GUIDE_DIR = os.path.join(ASSETS_DIR, 'guide')

GUIDE_VIDEO_PATH = os.environ.get('GUIDE_VIDEO_PATH', os.path.join(GUIDE_DIR, 'guide.mov'))
WATERMARK_PATH = 'assets/watermark_black.jpg'
CANDLE_PATH = os.environ.get('CANDLE_PATH', 'assets/overlays/candle_flowers.png')

FREE_HUGS_SCENE = '👫 Объятия 5с - БЕСПЛАТНО'
FULL_WATERMARK_PATH = os.environ.get('FULL_WATERMARK_PATH')
if not FULL_WATERMARK_PATH:
    for candidate in ('assets/watermark_full.png', 'assets/watermark.png', WATERMARK_PATH):
        if os.path.isfile(candidate):
            FULL_WATERMARK_PATH = candidate
            break

FREE_HUGS_WM_MODE = os.environ.get('FREE_HUGS_WM_MODE', 'single')
FREE_HUGS_WM_ALPHA = _env_float('FREE_HUGS_WM_ALPHA', 0.25)
FREE_HUGS_WM_SCALE = _env_float('FREE_HUGS_WM_SCALE', 0.90)
FREE_HUGS_WM_ROTATE = _env_float('FREE_HUGS_WM_ROTATE', 0.0)
FREE_HUGS_WM_GRID_COLS = _env_int('FREE_HUGS_WM_GRID_COLS', 3)
FREE_HUGS_WM_GRID_ROWS = _env_int('FREE_HUGS_WM_GRID_ROWS', 6)
FREE_HUGS_WM_GRID_MARGIN = _env_int('FREE_HUGS_WM_GRID_MARGIN', 16)

FREE_HUGS_LIMIT = _env_int('FREE_HUGS_LIMIT', 2)
SCENE_PRICE_10S = _env_int('SCENE_PRICE_10S', 100)
OPT_PRICE_CUSTOM_BG = _env_int('OPT_PRICE_CUSTOM_BG', 50)
OPT_PRICE_CUSTOM_MUSIC = _env_int('OPT_PRICE_CUSTOM_MUSIC', 50)
OPT_PRICE_TITLES = _env_int('OPT_PRICE_TITLES', 50)

PAYMENT_GATE_ENABLED = _env_bool('PAYMENT_GATE_ENABLED', True)

QUOTA_DIR = 'quota'
FREE_HUGS_QUOTA_FILE = os.path.join(QUOTA_DIR, 'free_hugs_usage.json')
FREE_HUGS_WHITELIST = {
    value.strip()
    for value in os.environ.get('FREE_HUGS_WHITELIST', '').split(',')
    if value.strip()
}
FREE_HUGS_SCENE_KEYS = {
    '👫 Объятия 5с - БЕСПЛАТНО',
    '🫂 Объятия 5с - БЕСПЛАТНО',
}

LEGAL_DIR = 'assets/legal'
OFFER_FULL_BASENAME = 'offer_full'
POLICY_FULL_BASENAME = 'policy_full'
LEGAL_EXTS = ['.pdf', '.docx', '.doc', '.txt', '.md', '.html']

RESAMPLE = getattr(Image, 'Resampling', Image)

MIN_GAP_PX = 5
IDEAL_GAP_FRAC = 0.005
CENTER_BIAS_FRAC = 0.40
PAIR_WIDTH_WARN_RATIO = _env_float('PAIR_WIDTH_WARN_RATIO', 1.40)
MAX_UPSCALE = _env_float('MAX_UPSCALE', 1.8)

MIN_VISIBLE_FRAC = {
    ('🧍 В рост', 1): 0.66,
    ('🧍 В рост', 2): 0.64,
    ('👨‍💼 По пояс', 1): 0.56,
    ('👨‍💼 По пояс', 2): 0.54,
    ('👨‍💼 По грудь', 1): 0.48,
    ('👨‍💼 По грудь', 2): 0.46,
}


def min_visible_frac(format_key: str, count_people: int) -> float:
    return MIN_VISIBLE_FRAC.get((format_key, count_people), 0.56)


TH_FULL_SINGLE = 0.66
TH_FULL_DOUBLE = 0.66
TH_WAIST_SINGLE = 0.60
TH_WAIST_DOUBLE = 0.60
TH_CHEST_SINGLE = 0.50
TH_CHEST_DOUBLE = 0.50

LEAN_TARGET_VISIBLE_FRAC = _env_float('LEAN_TARGET_VISIBLE_FRAC', 0.76)
LEAN_MAX_VISIBLE_FRAC = _env_float('LEAN_MAX_VISIBLE_FRAC', 0.82)
LEAN_MIN_GAP_FRAC = _env_float('LEAN_MIN_GAP_FRAC', 0.01)
LEAN_CX_LEFT = _env_float('LEAN_CX_LEFT', 0.34)
LEAN_CX_RIGHT = _env_float('LEAN_CX_RIGHT', 0.66)

CHEST_VIRTUAL_FLOOR_FRAC = _env_float('CHEST_VIRTUAL_FLOOR_FRAC', 0.74)
CHEST_FOG_START_FRAC = _env_float('CHEST_FOG_START_FRAC', 0.62)
CHEST_FOG_MAX_ALPHA = _env_int('CHEST_FOG_MAX_ALPHA', 210)
CHEST_FOG_COLOR = tuple(
    map(int, os.environ.get('CHEST_FOG_COLOR', '255,224,170').split(','))
)

WAIST_VIRTUAL_FLOOR_FRAC = _env_float('WAIST_VIRTUAL_FLOOR_FRAC', 0.88)
WAIST_FOG_START_FRAC = _env_float('WAIST_FOG_START_FRAC', 0.80)
WAIST_FOG_MAX_ALPHA = _env_int('WAIST_FOG_MAX_ALPHA', 180)

MIN_SINGLE_FRAC = {'В рост': 0.66, 'По пояс': 0.56, 'По грудь': 0.48}
MIN_PAIR_FRAC = {'В рост': 0.64, 'По пояс': 0.54, 'По грудь': 0.46}

PAIR_UPSCALE_CAP = 1.10
SINGLE_UPSCALE_CAP = 1.12

_raw_admin = os.environ.get('ADMIN_CHAT_ID', '').strip()
try:
    ADMIN_CHAT_ID = int(_raw_admin) if _raw_admin.lstrip('-').isdigit() else None
except ValueError:
    ADMIN_CHAT_ID = None


def ensure_directories() -> None:
    for path in (
        UPLOADS_DIR,
        RENDERS_DIR,
        ASSETS_DIR,
        AUDIO_DIR,
        GUIDE_DIR,
        QUOTA_DIR,
        LEGAL_DIR,
    ):
        os.makedirs(path, exist_ok=True)
