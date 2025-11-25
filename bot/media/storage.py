from __future__ import annotations

import io
import time
import uuid
from pathlib import Path

from PIL import Image

UPLOADS_DIR = Path("uploads")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def save_upload_image_bytes(
    data: bytes,
    *,
    owner_label: str | int | None = None,
    ext_hint: str | None = ".jpg",
) -> str:
    if not data:
        raise ValueError("Image data is empty")

    suffix = (ext_hint or ".jpg").lower()
    if not suffix.startswith("."):
        suffix = f".{suffix}"

    parts: list[str] = []
    if owner_label is not None:
        parts.append(str(owner_label))
    parts.append(str(int(time.time())))
    parts.append(uuid.uuid4().hex)
    filename = "_".join(parts) + suffix
    dest = UPLOADS_DIR / filename

    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            if fmt not in {"JPEG", "PNG"}:
                fmt = "JPEG"
            if fmt == "JPEG":
                img = img.convert("RGB")
                img.save(dest, "JPEG", quality=95, optimize=True)
            else:
                img.save(dest, "PNG")
    except Exception as exc:  # noqa: BLE001
        print(f"[IMG_SAVE] Pillow failed ({type(exc).__name__}: {exc}); saving raw bytes")
        dest.write_bytes(data)

    return f"uploads/{filename}"
