"""
Smoke test that the watermark step accepts the new argument signature.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from moviepy import ColorClip
from PIL import Image, ImageDraw

from bot.render import pipeline

BASE_DIR = Path(__file__).resolve().parents[1]
UPLOADS_DIR = BASE_DIR / "uploads"
RENDERS_DIR = BASE_DIR / "renders"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
RENDERS_DIR.mkdir(parents=True, exist_ok=True)


def _create_photo(path: Path, text: str) -> None:
    img = Image.new("RGB", (800, 1200), (50, 100, 150))
    draw = ImageDraw.Draw(img)
    draw.text((50, 50), text, fill=(255, 255, 255))
    img.save(path, "JPEG", quality=90)


def _dummy_segment() -> str:
    clip = ColorClip(size=(720, 1280), color=(10, 30, 60), duration=1)
    dst = RENDERS_DIR / f"dummy_segment_{uuid.uuid4().hex}.mp4"
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
    photos = []
    for idx in range(2):
        p = UPLOADS_DIR / f"wmtest_{uuid.uuid4().hex}.jpg"
        _create_photo(p, f"WM {idx + 1}")
        photos.append(p.as_posix())

    dummy = _dummy_segment()
    original = pipeline._runway_segment_from_startframe  # type: ignore[attr-defined]

    def fake_segment(*args, **kwargs):
        return dummy

    pipeline._runway_segment_from_startframe = fake_segment  # type: ignore[attr-defined]
    try:
        result = pipeline.web_render_video(
            format_key="🧍 В рост",
            scene_key="👫 Объятия 5с - БЕСПЛАТНО",
            background_key="☁️ Облака",
            music_key=None,
            title="WM Test",
            subtitle="",
            photo_paths=photos,
            session_id="wm_test_session",
        )
        print(f"[WATERMARK TEST] final video: {result}")
    finally:
        pipeline._runway_segment_from_startframe = original  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
