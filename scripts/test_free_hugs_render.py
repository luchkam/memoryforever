"""
Smoke-test that `web_render_video` works for the free-hugs scene without hitting
NameError or real Runway calls.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from moviepy import ColorClip
from PIL import Image, ImageDraw, ImageFont

from bot.render import pipeline

BASE_DIR = Path(__file__).resolve().parents[1]
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR = BASE_DIR / "renders"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def _create_image(path: Path, label: str) -> None:
    img = Image.new("RGB", (900, 1400), (120, 90, 180))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("Arial.ttf", 80)
    except Exception:
        font = ImageFont.load_default()
    draw.text((50, 50), label, fill=(255, 255, 255), font=font)
    img.save(path, "JPEG", quality=95)


def _prepare_dummy_segment() -> str:
    clip = ColorClip(size=(720, 1280), color=(10, 40, 90), duration=1)
    dst = TEMP_DIR / f"dummy_runway_{uuid.uuid4().hex}.mp4"
    clip.write_videofile(
        dst.as_posix(),
        codec="libx264",
        audio=False,
        fps=24,
        preset="ultrafast",
        verbose=False,
        logger=None,
    )
    clip.close()
    return dst.as_posix()


def main() -> None:
    photo_paths: list[str] = []
    for idx in range(2):
        path = UPLOADS_DIR / f"freehug_{uuid.uuid4().hex}.jpg"
        _create_image(path, f"FH {idx+1}")
        photo_paths.append(path.as_posix())

    dummy_segment = _prepare_dummy_segment()

    original = pipeline._runway_segment_from_startframe  # type: ignore[attr-defined]

    def _fake_segment(*args, **kwargs):
        return dummy_segment

    pipeline._runway_segment_from_startframe = _fake_segment  # type: ignore[attr-defined]
    try:
        video_path = pipeline.web_render_video(
            format_key="🧍 В рост",
            scene_key="👫 Объятия 5с - БЕСПЛАТНО",
            background_key="☁️ Облака",
            music_key=None,
            title="Тест WEB",
            subtitle="Free hugs",
            photo_paths=photo_paths,
            session_id="test_free_hugs",
        )
        print(f"[TEST] web_render_video produced: {video_path}")
    finally:
        pipeline._runway_segment_from_startframe = original  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
