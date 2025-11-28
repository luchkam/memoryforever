from __future__ import annotations

import json
import glob
import os
from pathlib import Path
from typing import Dict

_CATALOG_PATH = Path(__file__).with_name("assets_catalog.json")

with open(_CATALOG_PATH, "r", encoding="utf-8") as fh:
    _catalog = json.load(fh)

CATALOG = _catalog

FORMATS: Dict[str, str] = {item["key"]: item["description"] for item in _catalog.get("formats", [])}

SCENES: Dict[str, dict] = {
    item["key"]: {
        "duration": int(item.get("duration", 0)),
        "kind": item.get("kind", ""),
        "people": int(item.get("people", 1)),
        "price_rub": int(item.get("price_rub", 0)),
    }
    for item in _catalog.get("scenes", [])
}
SCENE_PRICES: Dict[str, int] = {k: v.get("price_rub", 0) for k, v in SCENES.items()}

BG_FILES: Dict[str, str] = {item["key"]: item["path"] for item in _catalog.get("backgrounds", [])}
BACKGROUNDS = BG_FILES
BG_BY_CLEAN = {
    (name.split(" ", 1)[1] if " " in name else name): name
    for name in BG_FILES.keys()
}
CUSTOM_BG_KEY = "__CUSTOM__"

MUSIC: Dict[str, str] = {item["key"]: item["path"] for item in _catalog.get("music", [])}
MUSIC_BY_CLEAN = {name.replace("🎵 ", ""): path for name, path in MUSIC.items()}
CUSTOM_MUSIC_KEY = "🎵 Свой трек"
ALLOWED_AUDIO_EXTS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".flac",
    ".oga",
    ".opus",
}

SCENE_PROMPTS = {
    item["kind"]: item.get("prompt", "")
    for item in _catalog.get("scenes", [])
    if item.get("kind")
}

# Backwards compatibility: if prompts were not specified in catalog, fall back to defaults
if not SCENE_PROMPTS:
    SCENE_PROMPTS = {
        "hug": "Медленный равномерный dolly-in на людей, без резких зумов, стабильный кадр. Люди из стартового кадра начинают плавное сближение, поворачиваются друг к другу лицом, обнимаются, объятие длится, они покачиваются, руки меняют положение, головы касаются, но лица полностью не закрываются от камеры, мимика тёплая, движения сохраняются весь ролик. Фон оживает на протяжении всего видео. ",
        "kiss_cheek": "Люди из стартового кадра начинают плавное сближение, поворачиваются друг к другу лицом, обнимаются, готовясь к медленному и очень нежному поцелую — щека к щеке, они чуть покачиваются, слегка прижимаются, позы и взгляды плавно меняются на протяжении всего видео, лица никогда полностью не перекрываются. Фон оживает на протяжении всего видео. Медленный равномерный dolly-in на людей, без резких зумов, стабильный кадр. ",
        "wave": "Человек из стартового кадра дружелюбно машет рукой, меняя амплитуду и темп; корпус слегка разворачивается, вес перекатывается с ноги на ногу, возможен маленький шаг на месте; рука опускается и снова поднимается — движение непрерывное. Фон оживает на протяжении всего видео. Медленный равномерный dolly-in на персонажа, без резких зумов, стабильный кадр. ",
        "stairs": "Человек медленно машет рукой около трех секунд, разворачивается спиной и уходит вверх по лестнице. Камера плавно следует, без резких зумов. В конце фигура мягко растворяется в светлой дымке. ",
    }
else:
    # Ensure prompts dict covers every known kind
    for scene in _catalog.get("scenes", []):
        kind = scene.get("kind")
        if kind and kind not in SCENE_PROMPTS:
            SCENE_PROMPTS[kind] = scene.get("prompt", "")


def original_bg_from_clean(clean: str) -> str | None:
    return BG_BY_CLEAN.get(clean)


def cleanup_user_custom_bg(uid: int) -> None:
    for path in glob.glob(f"uploads/custombg_{uid}_*.*"):
        try:
            os.remove(path)
        except Exception:
            pass
