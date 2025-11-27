"""
Simple smoke test for the web render pipeline.

Usage:
    python scripts/test_web_render.py
"""

from __future__ import annotations

import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from bot.render.pipeline import web_render_video

BASE_DIR = Path(__file__).resolve().parents[1]
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _create_sample_image(path: Path, text: str, color: tuple[int, int, int]) -> None:
    img = Image.new("RGB", (1024, 1536), color)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("Arial.ttf", 72)
    except Exception:
        font = ImageFont.load_default()
    draw.text((50, 50), text, fill=(255, 255, 255), font=font)
    img.save(path, "JPEG", quality=95)


def main() -> None:
    photo_paths: list[str] = []
    for idx, color in enumerate([(180, 80, 80), (80, 120, 190)], start=1):
        file_path = UPLOADS_DIR / f"web_smoke_{uuid.uuid4().hex}.jpg"
        _create_sample_image(file_path, f"Photo {idx}", color)
        photo_paths.append(str(file_path.resolve()))

    video_path = web_render_video(
        format_key="🧍 В рост",
        scene_key="👫 Объятия 5с - БЕСПЛАТНО",
        background_key="☁️ Облака",
        music_key="🎵 Спокойная",
        title="Тестовый титр",
        subtitle="Здесь будет текст",
        photo_paths=photo_paths,
        session_id="web_smoke_test",
    )
    print(f"[SMOKE] Rendered video at: {video_path}")


if __name__ == "__main__":
    main()
