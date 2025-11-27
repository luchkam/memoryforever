"""
Quick end-to-end smoke test for the web render engine without hitting Runway.
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


def _create_test_photo(path: Path, text: str) -> None:
    img = Image.new("RGB", (1000, 1500), (120, 80, 170))
    draw = ImageDraw.Draw(img)
    draw.text((40, 40), text, fill=(255, 255, 255))
    img.save(path, "JPEG", quality=92)


def _dummy_segment() -> str:
    clip = ColorClip(size=(720, 1280), color=(10, 30, 70), duration=1.5)
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
    photos: list[str] = []
    for idx in range(2):
        path = UPLOADS_DIR / f"e2e_{uuid.uuid4().hex}.jpg"
        _create_test_photo(path, f"E2E {idx+1}")
        photos.append(path.as_posix())

    dummy = _dummy_segment()
    original = pipeline._runway_segment_from_startframe  # type: ignore[attr-defined]

    def fake_segment(*args, **kwargs) -> str:
        return dummy

    pipeline._runway_segment_from_startframe = fake_segment  # type: ignore[attr-defined]
    try:
        result = pipeline.render_full_video_from_photos_web(
            format_key="🧍 В рост",
            scene_key="👫 Объятия 5с - БЕСПЛАТНО",
            background_key="☁️ Облака",
            music_key=None,
            title="E2E Test",
            subtitle="Smoke",
            photo_paths=photos,
            owner_label="test_web_end2end",
            session_id="test_web_end2end",
        )
        print(f"[E2E TEST] Final video: {result}")
    finally:
        pipeline._runway_segment_from_startframe = original  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
