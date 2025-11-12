from __future__ import annotations

import json
import os
from typing import Dict

from . import assets
from .config import (
    ADMIN_CHAT_ID,
    FREE_HUGS_LIMIT,
    FREE_HUGS_QUOTA_FILE,
    FREE_HUGS_SCENE_KEYS,
    FREE_HUGS_WHITELIST,
    QUOTA_DIR,
)

users: Dict[int, dict] = {}
IN_RENDER: set[int] = set()
PENDING_ALBUMS: Dict[str, dict] = {}


def is_admin(uid: int) -> bool:
    try:
        return ADMIN_CHAT_ID and str(uid) == str(int(ADMIN_CHAT_ID))
    except Exception:
        return False


def is_free_hugs_whitelisted(uid: int) -> bool:
    if is_admin(uid):
        return True
    return str(uid) in FREE_HUGS_WHITELIST


def _quota_load() -> dict:
    os.makedirs(QUOTA_DIR, exist_ok=True)
    try:
        with open(FREE_HUGS_QUOTA_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _quota_save(data: dict) -> None:
    os.makedirs(QUOTA_DIR, exist_ok=True)
    tmp_path = FREE_HUGS_QUOTA_FILE + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, FREE_HUGS_QUOTA_FILE)


def get_free_hugs_count(uid: int) -> int:
    data = _quota_load()
    try:
        return int(data.get(str(uid), 0))
    except Exception:
        return 0


def inc_free_hugs_count(uid: int, delta: int = 1) -> None:
    data = _quota_load()
    key = str(uid)
    data[key] = int(data.get(key, 0)) + delta
    _quota_save(data)


def is_free_hugs(scene_key: str) -> bool:
    meta = assets.SCENES.get(scene_key, {})
    return (
        scene_key in FREE_HUGS_SCENE_KEYS
        or (
            meta.get('kind') == 'hug'
            and meta.get('duration') == 5
            and 'БЕСПЛАТНО' in scene_key
        )
    )


def free_hugs_remaining(uid: int) -> int:
    used = get_free_hugs_count(uid)
    return max(0, FREE_HUGS_LIMIT - used)


def new_state() -> dict:
    return {
        'scenes': [],
        'format': None,
        'bg': None,
        'music': None,
        'scene_idx': 0,
        'scene_jobs': [],
        'photos': [],
        'ready': False,
        'support': False,
        'await_approval': None,
        'await_custom_bg': False,
        'bg_custom_path': None,
        'await_custom_music': False,
        'custom_music_path': None,
        'offer_accepted': False,
        'offer_accepted_ver': None,
        'titles_mode': 'none',
        'titles_fio': None,
        'titles_dates': None,
        'titles_text': None,
        'await_titles_field': None,
        'await_payment': False,
        'payment_confirmed': False,
    }
