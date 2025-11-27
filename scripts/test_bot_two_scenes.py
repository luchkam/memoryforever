"""
Smoke-style test that mimics the Telegram bot pipeline for two scenes without hitting Runway.
It builds start frames, generates dummy video segments, and runs postprocess_concat_ffmpeg.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from moviepy import ColorClip
from PIL import Image, ImageDraw

from bot.assets import SCENES
from bot.render import pipeline

BASE_DIR = Path(__file__).resolve().parents[1]
UPLOADS_DIR = BASE_DIR / "uploads"
RENDERS_DIR = BASE_DIR / "renders"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
RENDERS_DIR.mkdir(parents=True, exist_ok=True)


def _make_dummy_photo(path: Path, label: str) -> None:
    img = Image.new("RGB", (1000, 1500), (50, 70, 140))
    d = ImageDraw.Draw(img)
    d.text((40, 40), label, fill=(255, 255, 255))
    img.save(path, "JPEG", quality=92)


def _make_dummy_segment(label: str, duration: float) -> str:
    clip = ColorClip(size=(720, 1280), color=(20, 20, 50), duration=duration)
    dst = RENDERS_DIR / f"dummy_{label}_{uuid.uuid4().hex}.mp4"
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
    # Two scenes: hugs (2 people) + farewell (1 person)
    scenes = [
        ("🫂 Объятия 10с - 100 рублей", ["L1", "R1"]),
        ("👋 Прощание 10с - 100 рублей", ["Solo"]),
    ]
    format_key = "🧍 В рост"

    # Custom background and music placeholders
    bg_path = UPLOADS_DIR / "test_custom_bg.jpg"
    Image.new("RGB", (720, 1280), (90, 60, 110)).save(bg_path, "JPEG", quality=90)
    music_path = (BASE_DIR / "audio" / "soft_pad.mp3").as_posix()

    # Monkey-patch smart_cutout to avoid rembg dependence in this smoke test
    original_cutout = pipeline.smart_cutout

    def _fake_cutout(img):
        return img.convert("RGBA")

    pipeline.smart_cutout = _fake_cutout  # type: ignore[assignment]
    try:
        segments: list[str] = []
        for idx, (scene_key, labels) in enumerate(scenes, start=1):
            photos = []
            for lab in labels:
                p = UPLOADS_DIR / f"test_photo_{lab}_{uuid.uuid4().hex}.jpg"
                _make_dummy_photo(p, f"{scene_key} {lab}")
                photos.append(p.as_posix())

            print(f"[TEST] Building start frame for scene {idx}: {scene_key}")
            start_frame, layout = pipeline.make_start_frame(photos, format_key, bg_path.as_posix(), layout=None)
            print(f"[TEST] start_frame -> {start_frame}")
            print(f"[TEST] layout metrics -> {layout}")

            duration = int(SCENES[scene_key]["duration"])
            seg_path = _make_dummy_segment(f"{idx}", duration=duration / 10.0)
            print(f"[TEST] dummy segment -> {seg_path}")
            segments.append(seg_path)

        final_path = RENDERS_DIR / f"test_final_{uuid.uuid4().hex}.mp4"
        titles_meta = {"fio": "Тестовый Пользователь", "dates": "1950–2024", "mem": "Память навсегда"}
        print("[TEST] Running postprocess_concat_ffmpeg…")
        pipeline.postprocess_concat_ffmpeg(
            segments,
            music_path,
            pipeline.DEFAULT_TITLE_TEXT,
            final_path.as_posix(),
            bg_overlay_file=bg_path.as_posix(),
            titles_meta=titles_meta,
            candle_path=pipeline.CANDLE_PATH,
        )
        print(f"[TEST] Final video: {final_path}")
    finally:
        pipeline.smart_cutout = original_cutout  # type: ignore[assignment]


if __name__ == "__main__":
    main()
