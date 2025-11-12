# Render pipeline helpers
from __future__ import annotations

import base64
import io
import json
import math
import os
import shutil
import subprocess
import textwrap
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import List

import numpy as np
import requests
try:
    from rembg import remove, new_session  # type: ignore[attr-defined]
except Exception as exc:  # noqa: BLE001
    remove = None  # type: ignore[assignment]
    new_session = None  # type: ignore[assignment]
    _REMBG_IMPORT_ERROR: Exception | None = exc
else:
    _REMBG_IMPORT_ERROR = None
from PIL import Image, ImageDraw, ImageFilter, ImageFont

TITLE_FONT_REGULAR_ENV = os.environ.get("TITLE_FONT_PATH")
TITLE_FONT_BOLD_ENV = os.environ.get("TITLE_FONT_BOLD_PATH")

TITLE_FONT_REGULAR_CANDIDATES = [
    TITLE_FONT_REGULAR_ENV,
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode MS.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode MS.ttf",
    "/Library/Fonts/Arial.ttf",
]
TITLE_FONT_BOLD_CANDIDATES = [
    TITLE_FONT_BOLD_ENV,
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]

TITLE_FONT_REGULAR_CANDIDATES = [p for p in TITLE_FONT_REGULAR_CANDIDATES if p]
TITLE_FONT_BOLD_CANDIDATES = [p for p in TITLE_FONT_BOLD_CANDIDATES if p]

from ..config import (
    ADMIN_CHAT_ID,
    CANDLE_PATH,
    CANDLE_WIDTH_FRAC,
    CHEST_FOG_COLOR,
    CHEST_FOG_MAX_ALPHA,
    CHEST_FOG_START_FRAC,
    CHEST_VIRTUAL_FLOOR_FRAC,
    CROSSFADE_SEC,
    FULL_WATERMARK_PATH,
    FREE_HUGS_WM_ALPHA,
    FREE_HUGS_WM_GRID_COLS,
    FREE_HUGS_WM_GRID_MARGIN,
    FREE_HUGS_WM_GRID_ROWS,
    FREE_HUGS_WM_MODE,
    FREE_HUGS_WM_ROTATE,
    FREE_HUGS_WM_SCALE,
    IDEAL_GAP_FRAC,
    LEAN_CX_LEFT,
    LEAN_CX_RIGHT,
    LEAN_MAX_VISIBLE_FRAC,
    LEAN_MIN_GAP_FRAC,
    LEAN_TARGET_VISIBLE_FRAC,
    MAX_UPSCALE,
    OAI_DEBUG,
    PREVIEW_START_FRAME,
    DEBUG_TO_ADMIN,
    RUNWAY_SEND_JPEG,
    START_OVERLAY_DEBUG,
    MF_DEBUG,
    MEM_TOP_FRAC,
    MIN_GAP_PX,
    MIN_PAIR_FRAC,
    MIN_SINGLE_FRAC,
    PAIR_UPSCALE_CAP,
    PAIR_WIDTH_WARN_RATIO,
    PROJECT_ROOT,
    RESAMPLE,
    RUNWAY_KEY,
    SINGLE_UPSCALE_CAP,
    TH_CHEST_DOUBLE,
    TH_CHEST_SINGLE,
    TH_FULL_DOUBLE,
    TH_FULL_SINGLE,
    TH_WAIST_DOUBLE,
    TH_WAIST_SINGLE,
    WAIST_FOG_MAX_ALPHA,
    WAIST_FOG_START_FRAC,
    WAIST_VIRTUAL_FLOOR_FRAC,
    WATERMARK_PATH,
)
from ..config import settings
from ..app import bot

os.environ.setdefault("U2NET_HOME", str(PROJECT_ROOT / "models"))


def _ensure_rembg_available() -> None:
    if remove is None or new_session is None:
        message = "rembg is not installed; background removal is unavailable on this host."
        if _REMBG_IMPORT_ERROR is not None:
            raise ModuleNotFoundError(message) from _REMBG_IMPORT_ERROR
        raise ModuleNotFoundError(message)


@lru_cache(maxsize=None)
def _get_rmbg_session(model: str):
    _ensure_rembg_available()
    assert new_session is not None
    return new_session(model)


def _rembg_remove(data, *, model: str, **kwargs):
    _ensure_rembg_available()
    assert remove is not None
    session = _get_rmbg_session(model)
    return remove(data, session=session, **kwargs)

from ..assets import SCENE_PROMPTS
from ..state import users, IN_RENDER
from ..utils import cleanup_uploads_folder


def alpha_metrics(img: Image.Image, thr: int = 20):
    """
    Возвращает (bbox, y_bottom) по непрозрачным пикселям альфа-канала.
    bbox: (x0, y0, x1, y1) в координатах изображения
    y_bottom: индекс нижней строки содержимого (int)
    """
    a = img.split()[-1]
    arr = np.asarray(a, dtype=np.uint8)
    ys, xs = np.where(arr >= thr)
    if ys.size == 0:
        b = img.getbbox() or (0, 0, img.width, img.height)
        return b, b[3] - 1
    x0, y0 = int(xs.min()), int(ys.min())
    x1, y1 = int(xs.max()) + 1, int(ys.max()) + 1
    return (x0, y0, x1, y1), (y1 - 1)

def _save_layout_debug(canvas_rgba: Image.Image, metrics: dict, base_id: str):
    """
    Сохраняет:
      - renders/temp/metrics_<base_id>.json — метрики компоновки
      - renders/temp/annot_<base_id>.png    — аннотированное превью с рамками
    """
    try:
        os.makedirs("renders/temp", exist_ok=True)
    except Exception:
        pass

    # 1) JSON
    try:
        mpath = f"renders/temp/metrics_{base_id}.json"
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        print(f"[DEBUG] metrics -> {mpath}")
    except Exception as e:
        print(f"[DEBUG] metrics save error: {e}")

    # 2) Аннотированная картинка
    try:
        im = canvas_rgba.convert("RGB")
        draw = ImageDraw.Draw(im)
        font = None
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        except Exception:
            font = ImageFont.load_default()

        # Рамки и подписи
        colors = {"L": (46, 204, 113), "R": (52, 152, 219)}  # зелёный/синий
        for side in ("L", "R"):
            if side not in metrics: 
                continue
            r = metrics[side]["rect_abs"]  # [x0,y0,x1,y1]
            c = colors[side]
            # рамка
            draw.rectangle(r, outline=c, width=3)
            # подпись
            label = (f"{side}: h={metrics[side]['height_px']} "
                     f"({int(round(metrics[side]['height_frac']*100))}% H), "
                     f"w={metrics[side]['width_px']}, "
                     f"cx={int(round(metrics[side]['center_x_frac']*100))}%, "
                     f"scale≈{metrics[side]['scale']:.2f}")
            tx, ty = r[0] + 4, max(4, r[1] - 18)
            draw.rectangle([tx-2, ty-2, tx+draw.textlength(label, font=font)+6, ty+18], fill=(0,0,0,128))
            draw.text((tx, ty), label, fill=(255,255,255), font=font)

            # отметка «пол»
            fy = metrics[side].get("floor_y")
            if isinstance(fy, int):
                draw.line([(r[0], fy), (r[2], fy)], fill=c, width=2)

        # Зазор между людьми
        gap = metrics.get("gap_px")
        if gap is not None:
            text = f"gap={gap}px ({int(round(metrics.get('gap_frac',0)*100))}% W)"
            draw.rectangle([10, 10, 10+draw.textlength(text, font=font)+12, 10+22], fill=(0,0,0,128))
            draw.text((16, 12), text, fill=(255,255,255), font=font)

        apath = f"renders/temp/annot_{base_id}.png"
        im.save(apath, "PNG")
        print(f"[DEBUG] annot -> {apath}")
    except Exception as e:
        print(f"[DEBUG] annot save error: {e}")

# --- Заглушка под старые вызовы ассистента (удалим позже вместе с ними) ---
def _is_minor_only(reasons: list[str] | None) -> bool:
    """Ассистент отключён: минор/мажор причины не анализируем."""
    return False

def validate_photo(path: str) -> tuple[bool, list[str]]:
    """
    Мягкая валидация фото.
    Возвращает (ok, warnings). ok=False — очень маленькое фото, но пайплайн не блокируем.
    """
    warns = []
    ok = True
    try:
        im = Image.open(path)
        # Нормализуем ориентацию по EXIF (если телефон переворачивал)
        try:
            from PIL import ImageOps
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass
    except Exception as e:
        return False, [f"не удалось открыть файл ({e})"]

    w, h = im.size
    min_dim = min(w, h)

    # 1) Размер/разрешение
    if min_dim < 300:
        ok = False
        warns.append(f"очень маленькое разрешение ({w}×{h}) — результат может исказиться")
    elif min_dim < 600:
        warns.append(f"низкое разрешение ({w}×{h}) — желательно ≥ 800px по меньшей стороне")

    # 2) Ориентация (для портретов лучше вертикальная)
    ratio = w / h if h else 1.0
    if ratio > 0.9:
        warns.append("фото не вертикальное — портрет обычно лучше выглядит в вертикали")

    # 3) Темнота/экспозиция (очень грубо)
    gray = im.convert("L")
    arr = np.asarray(gray, dtype=np.float32)
    mean = float(arr.mean())
    if mean < 55:
        warns.append("фото тёмное — попробуйте более светлое/контрастное")

    # 4) Размытость (приблизительно через «края»)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    earr = np.asarray(edges, dtype=np.float32)
    sharpness = float(earr.std())
    if sharpness < 8:
        warns.append("возможная размытость/шум — контуры слабые")

    return ok, warns

def _visible_bbox_height(img: Image.Image) -> int:
    b = img.getbbox() or (0, 0, img.width, img.height)
    return max(1, b[3] - b[1])

def smart_cutout(img_rgba: Image.Image) -> Image.Image:
    """
    Вырезка человека:
      1) пробуем портретную модель, иначе базовую;
      2) если силуэт слишком мал — пробуем ISNet;
      3) убираем «ореол» и чуть смягчаем край.
    """
    def _run(model_name: str):
        out = _rembg_remove(img_rgba, model=model_name, post_process_mask=True)
        if isinstance(out, (bytes, bytearray)):
            out = Image.open(io.BytesIO(out)).convert("RGBA")
        else:
            out = out.convert("RGBA")
        return out

    # 1) Портретная модель → fallback
    try:
        cut = _run("u2net_human_seg")
    except Exception:
        cut = _run("u2net")

    # 2) Если силуэт подозрительно маленький — пробуем ISNet
    try:
        bb = cut.getbbox() or (0, 0, cut.width, cut.height)
        area = (bb[2] - bb[0]) * (bb[3] - bb[1])
        if area < 0.12 * cut.width * cut.height:
            try:
                alt = _run("isnet-general-use")
                bb2 = alt.getbbox() or (0, 0, alt.width, alt.height)
                area2 = (bb2[2] - bb2[0]) * (bb2[3] - bb2[1])
                if area2 > area:
                    cut = alt
            except Exception:
                pass
    except Exception:
        pass

    # 3) Рафинирование маски: чуть «поджать» и дать перо
    a = cut.split()[-1]
    a = a.filter(ImageFilter.MinFilter(3))       # ~1px эрозия — убираем ореол
    a = a.filter(ImageFilter.GaussianBlur(1.2))  # мягкое перо ~1–2px
    cut.putalpha(a)
    return cut

def add_bottom_fog(canvas_rgba: Image.Image, start_y: int, color=(255, 224, 170), max_alpha=210):
    """
    Мягкий туман снизу (градиентная альфа от низа к start_y).
    canvas_rgba: RGBA 720x1280
    start_y: пиксельная координата, с которой туман исчезает (выше — 0)
    """
    W, H = canvas_rgba.width, canvas_rgba.height
    start_y = max(0, min(H, int(start_y)))
    if start_y >= H:
        return
    fog = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(fog)
    # рисуем вертикальный альфа-градиент
    for y in range(start_y, H):
        t = (y - start_y) / max(1, (H - start_y))  # 0..1
        a = int(round(max_alpha * t))              # плавное нарастание к низу
        draw.line([(0, y), (W, y)], fill=(color[0], color[1], color[2], a))
    canvas_rgba.alpha_composite(fog)

# ---------- RUNWAY ----------
RUNWAY_API = "https://api.dev.runwayml.com/v1"
HEADERS = {
    "Authorization": f"Bearer {RUNWAY_KEY}",
    "X-Runway-Version": "2024-11-06",
    "Content-Type": "application/json",
}

def encode_image_datauri(path: str) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    ext = path.lower().split(".")[-1]
    mime = "image/jpeg" if ext in ["jpg","jpeg"] else "image/png"
    return f"data:{mime};base64,{b64}"

def ensure_jpeg_copy(path: str, quality: int = 88) -> str:
    """
    Делает JPEG-копию файла (оптимизированную) и возвращает путь к .jpg.
    """
    im = Image.open(path).convert("RGB")
    out = os.path.splitext(path)[0] + ".jpg"
    im.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    try:
        os.sync()  # не у всех ОС есть, ок если свалится
    except Exception:
        pass
    return out

def encode_image_as_jpeg_datauri(path: str, quality: int = 88) -> str:
    """
    Принудительно кодирует изображение в JPEG (RGB) и возвращает dataURI.
    Это уменьшает размер по сравнению с PNG и стабильнее проходит в Runway.
    """
    im = Image.open(path).convert("RGB")
    bio = io.BytesIO()
    im.save(bio, format="JPEG", quality=quality, optimize=True, progressive=True)
    b64 = base64.b64encode(bio.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"

def cut_foreground_to_png(in_path: str) -> str:
    """Вырезает фон из JPG/PNG и сохраняет PNG с альфой."""
    with open(in_path, "rb") as f:
        raw = f.read()
    out = _rembg_remove(raw, model="u2net")
    out_path = os.path.splitext(in_path)[0] + "_cut.png"
    with open(out_path, "wb") as f:
        f.write(out)
    return out_path

def _to_jpeg_copy(src_path: str, quality: int = 88) -> str:
    im = Image.open(src_path).convert("RGB")
    out_path = os.path.join("uploads", f"startframe_{uuid.uuid4().hex}.jpg")
    im.save(out_path, "JPEG", quality=quality, optimize=True, progressive=True)
    return out_path

def ensure_runway_datauri_under_limit(path: str, limit: int = 5_000_000) -> tuple[str, str]:
    data = encode_image_datauri(path)
    if len(data) <= limit:
        return data, path

    last_path = path
    for q in (88, 80, 72):
        try:
            jpg = _to_jpeg_copy(path, quality=q)
            last_path = jpg
            data = encode_image_datauri(jpg)
            if len(data) <= limit:
                print(f"[Runway] using JPEG q={q}, data_uri={len(data)} bytes")
                return data, jpg
        except Exception as e:
            print(f"[Runway] jpeg fallback q={q} failed: {e}")

    print(f"[Runway] still heavy after JPEG attempts, length={len(data)}")
    return data, last_path

def _post_runway(payload: dict) -> dict | None:
    try:
        _pl = ""
        try:
            _pl = (payload.get("promptText") or payload.get("prompt") or "") if isinstance(payload, dict) else ""
        except Exception:
            pass

        model = payload.get("model")
        ratio = payload.get("ratio") or payload.get("aspect_ratio")
        dur   = payload.get("duration")

        msg = f"[Runway] model={model} dur={dur} ratio={ratio}"
        if _pl:
            msg += f" prompt[{len(_pl)}]={_pl.replace(chr(10),' ')}"
        print(msg)

        if MF_DEBUG:
            try:
                os.makedirs("renders/temp", exist_ok=True)
                preview = {
                    "model": model,
                    "duration": dur,
                    "ratio": ratio,
                    "prompt_len": len(_pl),
                    "image_data_uri_len": len(payload.get("promptImage") or payload.get("image") or ""),
                }
                with open(os.path.join("renders/temp", f"runway_payload_{int(time.time())}.json"), "w", encoding="utf-8") as f:
                    json.dump(preview, f, ensure_ascii=False, indent=2)
                print("[Runway] payload preview saved")
            except Exception as _e:
                print(f"[Runway] payload preview save err: {_e}")

        # Отправка в Runway (заглушка отключена)
        r = requests.post(f"{RUNWAY_API}/image_to_video", headers=HEADERS, json=payload, timeout=60)
        if r.status_code == 200:
            return r.json()
        print(f"[Runway {r.status_code}] {r.text}")
        return None
    except requests.RequestException as e:
        print(f"[Runway transport error] {e}")
        return None

def runway_start(prompt_image_datauri: str, prompt_text: str, duration: int):
    """
    Порядок попыток:
    1) gen4_turbo + promptImage/promptText + ratio (текущая схема этого API)
    2) gen4_turbo + image/prompt + aspect_ratio (альтернативная)
    3) gen3a_turbo + image/prompt + aspect_ratio (запасной)
    """
    variants = [
        {
            "model": "gen4_turbo",
            "promptImage": prompt_image_datauri,   # <-- ОБЯЗАТЕЛЬНО
            "promptText":  prompt_text,
            "ratio": "720:1280",
            "duration": int(duration),
        },
        {
            "model": "gen4_turbo",
            "image": prompt_image_datauri,
            "prompt": prompt_text,
            "aspect_ratio": "9:16",
            "duration": int(duration),
        },
        {
            "model": "gen3a_turbo",
            "image": prompt_image_datauri,
            "prompt": prompt_text,
            "aspect_ratio": "9:16",
            "duration": int(duration),
        },
    ]

    last_keys = ""
    for payload in variants:
        resp = _post_runway(payload)
        if resp:
            return resp
        last_keys = f"{list(payload.keys())}"

    raise RuntimeError(f"Runway returned 400/4xx for all variants (payload={last_keys}). Check logs above.")

def runway_poll(task_id: str, timeout_sec=300, every=5):
    """Опрашивает статус задачи Runway с обработкой ошибок сети."""
    start = time.time()
    attempts = 0
    max_attempts = 10

    while True:
        attempts += 1
        try:
            print(f"[Runway] Polling task {task_id} (attempt {attempts}/{max_attempts})")
            rr = requests.get(f"{RUNWAY_API}/tasks/{task_id}", headers=HEADERS, timeout=30)
            rr.raise_for_status()
            data = rr.json()
            st = data.get("status")
            print(f"[Runway] Status: {st}")

            if st in ("SUCCEEDED","FAILED","ERROR","CANCELED"):
                return data

            if time.time() - start > timeout_sec:
                print(f"[Runway] Timeout after {timeout_sec}s")
                return {"status":"TIMEOUT","raw":data}

            time.sleep(every)

        except requests.exceptions.Timeout:
            print(f"[Runway] Request timeout (attempt {attempts})")
            if attempts >= max_attempts:
                return {"status":"NETWORK_ERROR","error":"Too many timeouts"}
            time.sleep(10)

        except requests.exceptions.RequestException as e:
            print(f"[Runway] Network error (attempt {attempts}): {e}")
            if attempts >= max_attempts:
                return {"status":"NETWORK_ERROR","error":str(e)}
            time.sleep(10)

        except Exception as e:
            print(f"[Runway] Unexpected error (attempt {attempts}): {e}")
            if attempts >= max_attempts:
                return {"status":"ERROR","error":str(e)}
            time.sleep(10)

def download(url: str, save_path: str):
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk: f.write(chunk)
    return save_path

def _video_duration_sec(path: str) -> float:
    """Возвращает длительность видео через ffprobe (секунды)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", path],
            capture_output=True, text=True, check=True
        )
        return float(r.stdout.strip() or "0")
    except Exception:
        return 0.0

def _xfade_two(in1: str, in2: str, out_path: str, fade_sec: float = 0.7):
    """Сшивает два видео с кроссфейдом (без аудио)."""
    d1 = _video_duration_sec(in1)
    offset = max(0.0, d1 - fade_sec)
    # Единый fps/профиль для стабильности
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", in1, "-i", in2,
        "-filter_complex",
        f"[0:v]fps=24,format=yuv420p[v0];[1:v]fps=24,format=yuv420p[v1];"
        f"[v0][v1]xfade=transition=fade:duration={fade_sec}:offset={offset},format=yuv420p[v]",
        "-map", "[v]",
        "-an",
        "-r", "24",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        out_path
    ], tag="xfade", out_hint=out_path)

def _merge_with_fades(video_paths: List[str], fade_sec: float = 0.7) -> str:
    """Чейним кроссфейды попарно: (((v1 xfade v2) xfade v3) ...). Возвращает путь к итоговому ролику."""
    assert len(video_paths) >= 2
    tmp_dir = "renders/temp"
    os.makedirs(tmp_dir, exist_ok=True)
    acc = video_paths[0]
    for i, nxt in enumerate(video_paths[1:], start=1):
        out_i = os.path.join(tmp_dir, f"xfade_{i}_{uuid.uuid4().hex}.mp4")
        _xfade_two(acc, nxt, out_i, fade_sec=fade_sec)
        # следующая итерация будет склеивать out_i с следующим
        acc = out_i
    return acc

def _ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return "ffmpeg"

def _run_ffmpeg(cmd: list[str], tag: str, out_hint: str | None = None):
    """Запускает ffmpeg, пишет stdout/stderr в файлы и печатает хвост ошибки.
    """
    try:
        os.makedirs("renders/temp", exist_ok=True)
    except Exception:
        pass
    log_base = f"renders/temp/ffmpeg_{tag}_{int(time.time())}_{uuid.uuid4().hex}"
    so = f"{log_base}.out.log"
    se = f"{log_base}.err.log"
    try:
        # заменяем первый элемент на конкретный бинарник ffmpeg
        if cmd and os.path.basename(cmd[0]) == "ffmpeg":
            cmd[0] = _ffmpeg_bin()
        res = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with open(so, "wb") as f:
            f.write(res.stdout or b"")
        with open(se, "wb") as f:
            f.write(res.stderr or b"")
        return True
    except subprocess.CalledProcessError as e:
        try:
            with open(so, "wb") as f:
                f.write(e.stdout or b"")
            with open(se, "wb") as f:
                f.write(e.stderr or b"")
        except Exception:
            pass
        tail = (e.stderr or b"").decode("utf-8", "ignore").splitlines()[-20:]
        print(f"[FFMPEG][{tag}] failed. See logs: {so} / {se}")
        if out_hint:
            print(f"[FFMPEG][{tag}] output: {out_hint}")
        for line in tail:
            print(f"[FFMPEG][{tag}] {line}")
        raise

def apply_fullscreen_watermark(in_video: str, out_video: str, wm_path: str,
                               mode: str = FREE_HUGS_WM_MODE,
                               alpha: float = FREE_HUGS_WM_ALPHA):
    """
    Накладывает «большой» полупрозрачный водяной знак на видео.
    mode='single' — один крупный по центру; mode='grid' — сетка маленьких.
    """
    if not os.path.isfile(wm_path):
        raise FileNotFoundError(f"watermark file not found: {wm_path}")

    m = (mode or "").lower()
    if m == "grid":
        cols = max(1, FREE_HUGS_WM_GRID_COLS)
        rows = max(1, FREE_HUGS_WM_GRID_ROWS)
        margin = max(0, FREE_HUGS_WM_GRID_MARGIN)
        N = cols * rows

        # 1) приводим логотип к RGBA и задаём прозрачность
        # 2) scale2ref — масштабируем логотип относительно основного видео:
        #    ширина ячейки = main_w/cols - 2*margin
        # 3) клонируем логотип split'ом и раскладываем overlay'ями по сетке
        labels = "".join(f"[w{i}]" for i in range(N))
        fc = (
        f"[1:v]format=rgba,colorchannelmixer=aa={alpha}[wm0];"
        f"[wm0][0:v]scale2ref=w='(main_w/{cols})-({2*margin})':h=-1[wm][base];"
        f"[wm]split={N}{labels};"
        )
        prev = "[base]"
        idx = 0
        for r in range(rows):
            for c in range(cols):
                x = f"(main_w/{cols})*{c} + ((main_w/{cols})-w)/2"
                y = f"(main_h/{rows})*{r} + ((main_h/{rows})-h)/2"
                nxt = "[v]" if (idx == N - 1) else f"[t{idx}]"
                fc += f"{prev}[w{idx}]overlay=x='{x}':y='{y}':format=auto{nxt};"
                prev = nxt
                idx += 1
    else:
        # single: один крупный логотип по центру; масштаб и поворот настраиваемые
        scale = max(0.2, min(1.5, FREE_HUGS_WM_SCALE))
        rot   = float(FREE_HUGS_WM_ROTATE)

        if abs(rot) > 0.01:
            fc = (
                f"[1:v]format=rgba,colorchannelmixer=aa={alpha}[wm0];"
                f"[wm0][0:v]scale2ref=w='main_w*{scale}':h=-1[wm][base];"
                f"[wm]rotate={rot}*PI/180:c=none:ow='rotw(iw)':oh='roth(ih)'[wmr];"
                f"[base][wmr]overlay=x='(main_w-w)/2':y='(main_h-h)/2':format=auto[v]"
            )
        else:
            fc = (
                f"[1:v]format=rgba,colorchannelmixer=aa={alpha}[wm0];"
                f"[wm0][0:v]scale2ref=w='main_w*{scale}':h=-1[wm][base];"
                f"[base][wm]overlay=x='(main_w-w)/2':y='(main_h-h)/2':format=auto[v]"
            )

    cmd = [
        "ffmpeg", "-y",
        "-i", in_video,
        "-loop", "1", "-i", wm_path,
        "-filter_complex", fc,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        out_video
    ]
    _run_ffmpeg(cmd, tag="wm_fullscreen", out_hint=out_video)
    return out_video

def _log_fail(uid: int, reason: str, payload: dict | None = None, response: dict | None = None):
    try:
        os.makedirs("renders/temp", exist_ok=True)
        path = os.path.join("renders/temp", f"fail_{uid}_{int(time.time())}_{uuid.uuid4().hex}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "ts": datetime.now(timezone.utc).isoformat(),
                "uid": uid,
                "reason": reason,
                "payload": payload or {},
                "response": response or {}
            }, f, ensure_ascii=False, indent=2)
        print(f"[FAILLOG] {reason} -> {path}")
        # если задан ADMIN_CHAT_ID — шлём короткое уведомление
        if ADMIN_CHAT_ID:
            try:
                bot.send_message(int(ADMIN_CHAT_ID), f"⚠️ FAIL {reason} (uid={uid})\n{os.path.basename(path)} сохранён.")
            except Exception:
                pass
    except Exception as e:
        print(f"[FAILLOG] write error: {e}")

def oai_gate_check(start_frame_path: str, base_prompt: str, meta: dict, timeout_sec: int = 120) -> dict | None:
    """
    Ассистент отключён: ничего не проверяем и ничего не добавляем.
    Возвращаем None, чтобы остальной код шёл по «без ассистента» ветке.
    """
    return None

# ---------- ВЫРЕЗАНИЕ И СТАРТ-КАДР ----------
def cutout(path: str) -> Image.Image:
    im = Image.open(path).convert("RGBA")
    cut = _rembg_remove(im, model="u2net")
    # rembg может вернуть bytes — нормализуем к PIL.Image
    if isinstance(cut, (bytes, bytearray)):
        cut = Image.open(io.BytesIO(cut)).convert("RGBA")
    return cut

def _resize_fit_center(img: Image.Image, W: int, H: int) -> Image.Image:
    """Вписать картинку в холст W×H с сохранением пропорций и кропом по центру."""
    wr, hr = W / img.width, H / img.height
    scale = max(wr, hr)
    new = img.resize((int(img.width * scale), int(img.height * scale)), RESAMPLE.LANCZOS)
    x = (new.width - W) // 2
    y = (new.height - H) // 2
    return new.crop((x, y, x + W, y + H))

def make_start_frame(photo_paths: List[str], framing_key: str, bg_file: str, layout: dict | None = None) -> tuple[str, dict]:
    """
    Формирует стартовый кадр. Ветку для 2х людей упростили (LEAN v0):
    - одинаковая видимая высота силуэтов (~70% H, но не больше MAX_VISIBLE_FRAC);
    - жёсткий внутренний зазор >= 5% ширины;
    - без автоподтяжек/ростов; фиксированная, предсказуемая геометрия.
    """

    def _min_target_for(framing: str, people_count: int) -> float:
        # согласуем с таблицами MIN_SINGLE_FRAC/MIN_PAIR_FRAC выше
        if "В рост" in framing:
            return MIN_PAIR_FRAC["В рост"] if people_count >= 2 else MIN_SINGLE_FRAC["В рост"]
        elif "По пояс" in framing:
            return MIN_PAIR_FRAC["По пояс"] if people_count >= 2 else MIN_SINGLE_FRAC["По пояс"]
        else:  # По грудь
            return MIN_PAIR_FRAC["По грудь"] if people_count >= 2 else MIN_SINGLE_FRAC["По грудь"]

    W, H = 720, 1280
    base_id = uuid.uuid4().hex
    floor_margin = 0  # пол стоит ровно по нижнему краю кадра
    # (опционально: читать из layout)
    if layout and isinstance(layout, dict) and "floor_margin" in layout:
        floor_margin = int(layout["floor_margin"])

    # верхний «воздух»
    if "По грудь" in framing_key:
        HEADROOM_FRAC = 0.03
    elif "По пояс" in framing_key:
        HEADROOM_FRAC = 0.02
    else:
        HEADROOM_FRAC = 0.005  # для «В рост» допускаем почти нулевой запас

    # --- режим и виртуальный «пол» по формату ---
    is_chest = ("По грудь" in framing_key)
    if is_chest:
        virtual_floor_y = int(H * CHEST_VIRTUAL_FLOOR_FRAC)
    else:
        virtual_floor_y = H - 1  # как и раньше: почти у нижней кромки

    is_waist = ("По пояс" in framing_key)
    if is_chest:
        virtual_floor_y = int(H * CHEST_VIRTUAL_FLOOR_FRAC)
    elif is_waist:
        virtual_floor_y = int(H * WAIST_VIRTUAL_FLOOR_FRAC)  # <-- не в самый низ!
    else:
        virtual_floor_y = H - 1

    # 1) фон
    bg = Image.open(bg_file).convert("RGB")
    bg = _resize_fit_center(bg, W, H)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=0.8))
    canvas = bg.convert("RGBA")

    # 2) вырезаем людей
    cuts = []
    for p in photo_paths:
        im = Image.open(p).convert("RGBA")
        cut_rgba = smart_cutout(im)
        cuts.append(cut_rgba)

    if MF_DEBUG:
        try:
            for i, c in enumerate(cuts):
                bb, yb = alpha_metrics(c)
                eff_h = max(1, (yb - bb[1] + 1))
                print(f"[LAYOUT] person#{i+1}: img={c.width}x{c.height} eff_h={eff_h} bbox={bb}")
        except Exception as _e:
            print(f"[LAYOUT] cut metrics err: {_e}")

    # 3) целевая высота относительно кадра (используется в одиночной ветке)
    two = (len(photo_paths) > 1)
    if "В рост" in framing_key:
        TARGET_VISIBLE_FRAC = 0.66 if len(cuts) == 2 else 0.66
    elif "По пояс" in framing_key:
        TARGET_VISIBLE_FRAC = 0.60 if len(cuts) == 2 else 0.56
    else:  # «По грудь»
        TARGET_VISIBLE_FRAC = 0.50 if len(cuts) == 2 else 0.48

    MAX_VISIBLE_FRAC = LEAN_MAX_VISIBLE_FRAC
    TARGET_VISIBLE_FRAC = min(TARGET_VISIBLE_FRAC, MAX_VISIBLE_FRAC)

    # инициализация переменной для одиночной ветки
    target_h = TARGET_VISIBLE_FRAC

    # минимум (анти-карлик)
    target_h_min = _min_target_for(framing_key, len(photo_paths))
    if target_h < target_h_min:
        target_h = target_h_min

    def scale_to_target_effective(img: Image.Image, target: float) -> Image.Image:
        bbox, yb = alpha_metrics(img)
        eff_h = max(1, (yb - bbox[1] + 1))
        scale = (H * target) / eff_h
        if scale > MAX_UPSCALE:
            scale = MAX_UPSCALE
        nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
        return img.resize((nw, nh), RESAMPLE.LANCZOS)

    def place_y_for_floor(img: Image.Image, floor_y: int | None = None) -> int:
        """
        Ставит низ видимого силуэта на заданную линию floor_y (если None — старое поведение у нижней кромки).
        """
        bbox, yb = alpha_metrics(img)
        eff_h = (yb - bbox[1] + 1)
        if floor_y is None:
            y_top_content = H - floor_margin - eff_h
        else:
            y_top_content = int(floor_y - eff_h)
        y_img = y_top_content - bbox[1]
        return int(y_img)

    def draw_with_shadow(base: Image.Image, person: Image.Image, x: int, y: int):
        alpha = person.split()[-1]
        soft = alpha.filter(ImageFilter.GaussianBlur(6))
        shadow = Image.new("RGBA", person.size, (0, 0, 0, 0))
        shadow.putalpha(soft.point(lambda a: int(a * 0.45)))
        base.alpha_composite(shadow, (x, y + 8))
        base.alpha_composite(person, (x, y))

    def _rect_at(x, y, img):
        bx, by, bx1, by1 = alpha_metrics(img)[0]
        return (x + bx, y + by, x + bx1, y + by1)

    def _draw_debug_boxes(base: Image.Image, rects: list[tuple[int,int,int,int]]):
        if not START_OVERLAY_DEBUG:
            return
        ov = Image.new("RGBA", base.size, (0,0,0,0))
        g = ImageDraw.Draw(ov)
        for r in rects:
            g.rectangle(r, outline=(255, 0, 0, 200), width=3)
        m = 20
        g.rectangle((m, m, base.width - m, base.height - m), outline=(0, 255, 0, 180), width=2)
        base.alpha_composite(ov)

    # ------------------------------- 1 человек -------------------------------
    if len(cuts) == 1:
        P = scale_to_target_effective(cuts[0], target_h)
        x = (W - P.width) // 2
        y = place_y_for_floor(P, virtual_floor_y)

        # оценка видимой высоты
        def rect_at_single(px, py, img):
            bx, by, bx1, by1 = alpha_metrics(img)[0]
            return (px + bx, py + by, px + bx1, py + by1)

        r = rect_at_single(x, y, P)
        group_h = r[3] - r[1]
        fmt = "В рост" if "В рост" in framing_key else ("По пояс" if "По пояс" in framing_key else "По грудь")
        min_h_frac = MIN_SINGLE_FRAC[fmt]

        if group_h < int(min_h_frac * H):
            need = (min_h_frac * H) / max(1, group_h)
            cap = SINGLE_UPSCALE_CAP
            new_target = min(target_h * need, target_h * cap)
            if new_target > target_h:
                P = scale_to_target_effective(cuts[0], new_target)
                x = (W - P.width) // 2
                y = place_y_for_floor(P)

        margin = 20
        x = max(margin, min(W - P.width - margin, x))
        top_margin = max(margin, int(HEADROOM_FRAC * H))
        y = max(top_margin, y)  # не поднимаем снизу — пол вплотную к низу

        # мягкий ручной layout для 1 человека (если вдруг прилетит)
        if layout and isinstance(layout, dict):
            scl = int(layout.get("scale_left_pct", 0) or 0)
            dxl = int(layout.get("shift_left_px", 0) or 0)
            if scl != 0:
                k = 1.0 + max(-0.20, min(0.20, scl / 100.0))
                nw, nh = max(1, int(P.width * k)), max(1, int(P.height * k))
                P = P.resize((nw, nh), RESAMPLE.LANCZOS)
                y = place_y_for_floor(P)
            if dxl != 0:
                x += int(-dxl)
            x = max(margin, min(W - P.width - margin, x))
            y = max(margin, min(H - P.height - margin, y))

        # анти-карлик для одиночки
        def _visible_frac(img: Image.Image) -> float:
            bb, yb = alpha_metrics(img)
            eff_h = max(1, (yb - bb[1] + 1))
            return eff_h / H

        grow_tries = 0
        while _visible_frac(P) < _min_target_for(framing_key, 1) and grow_tries < 12:
            new_target = min(target_h * 1.04, 0.98)
            newP = scale_to_target_effective(cuts[0], new_target)
            cx = x + P.width // 2
            cy_floor = place_y_for_floor(newP)
            newx = cx - newP.width // 2
            margin = 20
            newx = max(margin, min(W - newP.width - margin, newx))
            newy = max(margin, min(H - newP.height - margin, cy_floor))
            if newy <= margin or newx <= margin or (newx + newP.width) >= (W - margin):
                break
            P, x, y = newP, newx, newy
            target_h = new_target
            grow_tries += 1

        draw_with_shadow(canvas, P, x, y)
        try:
            _draw_debug_boxes(canvas, [_rect_at(x, y, P)])
        except Exception:
            pass

    # ------------------------------ 2 человека (STRICT SIDE-BY-SIDE) ------------------------------
    else:
        L = cuts[0]
        R = cuts[1]

        # --- базовые хелперы ---
        def _vis_rect(img):
            (bx, by, bx1, by1), _ = alpha_metrics(img)
            return bx, by, bx1, by1

        def _vis_w(img):
            bx, by, bx1, by1 = _vis_rect(img)
            return max(1, bx1 - bx)

        def _vis_h(img):
            (bx, by, bx1, by1), yb = alpha_metrics(img)
            return max(1, yb - by + 1)

        def _scale_abs(img, k):
            k = float(k)
            if k <= 0: 
                return img
            nw, nh = max(1, int(round(img.width * k))), max(1, int(round(img.height * k)))
            if nw == img.width and nh == img.height:
                # принудительно уменьшаем на 1 пикс при k<1, чтобы не зациклиться
                if k < 1.0:
                    nw = max(1, img.width - 1)
                    nh = max(1, img.height - 1)
            return img.resize((nw, nh), RESAMPLE.LANCZOS)

        def _place_pair(center_x, gap_px, left_limit, right_limit, floor_y):
            """
            Ставит пару как ЕДИНУЮ группу внутрь [left_limit, right_limit], сохраняя gap_px и «низ» = floor_y.
            Возвращает (lx, yl, rx, yr, ra, rb).
            """
            bxL, byL, bx1L, by1L = _vis_rect(L)
            bxR, byR, bx1R, by1R = _vis_rect(R)
            wL = bx1L - bxL
            wR = bx1R - bxR

            total = wL + gap_px + wR
            group_left_desired = int(round(center_x - (wL + gap_px/2)))
            group_left  = max(left_limit, min(right_limit - total, group_left_desired))
            group_right = group_left + total

            lx = group_left - bxL
            rx = group_left + wL + gap_px - bxR
            yl = place_y_for_floor(L, floor_y)
            yr = place_y_for_floor(R, floor_y)

            def _rect_at(x, y, img):
                bx, by, bx1, by1 = _vis_rect(img)
                return (x + bx, y + by, x + bx1, y + by1)

            ra = _rect_at(lx, yl, L)
            rb = _rect_at(rx, yr, R)
            return lx, yl, rx, yr, ra, rb

        # --- параметры размещения ---
        MARGIN = 20
        is_full = ("В рост" in framing_key) or ("в рост" in framing_key)
        MAX_VISIBLE_FRAC = LEAN_MAX_VISIBLE_FRAC if is_full else max(LEAN_MAX_VISIBLE_FRAC, 0.76)
        TARGET_VISIBLE_FRAC = min(LEAN_TARGET_VISIBLE_FRAC, MAX_VISIBLE_FRAC)

        # начальный масштаб по видимой высоте
        def _scale_to_vis_frac(img, target_frac):
            cur = _vis_h(img) / H
            if cur <= 1e-6:
                return img
            k = max(0.4, min(MAX_UPSCALE, target_frac / cur))
            return _scale_abs(img, k)

        L = _scale_to_vis_frac(L, TARGET_VISIBLE_FRAC)
        R = _scale_to_vis_frac(R, TARGET_VISIBLE_FRAC)
        if (_vis_h(L)/H) > MAX_VISIBLE_FRAC:
            L = _scale_to_vis_frac(L, MAX_VISIBLE_FRAC)
        if (_vis_h(R)/H) > MAX_VISIBLE_FRAC:
            R = _scale_to_vis_frac(R, MAX_VISIBLE_FRAC)

        # НИКАКОЙ «полосы» — используем всю ширину кадра (кроме безопасных полей)
        left_limit  = MARGIN
        right_limit = W - MARGIN
        available_width = right_limit - left_limit

        # жёсткий минимум зазора
        min_gap = max(MIN_GAP_PX, int(LEAN_MIN_GAP_FRAC * W))
        ideal_gap = max(min_gap, int(IDEAL_GAP_FRAC * W))
        center_x = W // 2

        # Разрешаем лёгкий нахлёст только для «По пояс»
        if "По пояс" in framing_key and os.environ.get("ALLOW_OVERLAP_WAIST", "1") == "1":
            max_ov = float(os.environ.get("MAX_OVERLAP_WAIST_FRAC", "0.1"))  # до 10% ширины кадра
            min_gap = -int(W * max_ov)          # отрицательный gap = допустимый нахлёст
            ideal_gap = max(min_gap, ideal_gap) # если без нахлёста не влезает — упадём до min_gap

        # Ручная настройка, если нужно «ещё ближе»
        if layout and isinstance(layout, dict):
            if "gap_px" in layout:
                ideal_gap = max(min_gap, int(layout["gap_px"]))
            elif "gap_pct" in layout:  # в процентах от ширины кадра
                ideal_gap = max(min_gap, int(W * float(layout["gap_pct"]) / 100.0))

        # --- 1) АВТОСКЕЙЛ ПО ГОРИЗОНТАЛИ (равномерно) ---
        for _ in range(60):
            wL = _vis_w(L)
            wR = _vis_w(R)
            need = wL + wR + min_gap
            if need <= available_width:
                break
            k = max(0.40, min(0.995, (available_width / need) * 0.985))  # чуть с запасом
            L = _scale_abs(L, k)
            R = _scale_abs(R, k)
        # страховка от редких «не сжалось»
        wL = _vis_w(L); wR = _vis_w(R)
        if (wL + wR + min_gap) > available_width:
            k = (available_width - min_gap) / max(1, (wL + wR))
            k = max(0.40, min(0.99, k))
            L = _scale_abs(L, k); R = _scale_abs(R, k)

        # --- 2) СТАВИМ ГРУППУ ВНУТРЬ ПОЛОСЫ (без перекрытия) ---
        gap_px = ideal_gap
        # если идеальный зазор не помещается — берём минимальный
        if _vis_w(L) + _vis_w(R) + gap_px > available_width:
            gap_px = min_gap

        lx, yl, rx, yr, ra, rb = _place_pair(center_x, gap_px, left_limit, right_limit, virtual_floor_y)

        # --- 3) HEADROOM/CLAMP: даунскейлим ВСЮ группу, пока всё не ок ---
        headroom_px = int(HEADROOM_FRAC * H)

        def _top_ok(r):  # r = (x0,y0,x1,y1)
            return r[1] > headroom_px

        # --- 3) HEADROOM: людей больше не уменьшаем; place_y_for_floor гарантирует,
        # что верх в кадре, а ноги «на полу».
        pass

        # --- 4) ФИНАЛЬНЫЕ ГАРАНТИИ: no overlap, всё внутри пределов ---
        # Пере-проверка зазора после всех клампов
        def _inner_gap(a, b):  # a,b = rects
            return b[0] - a[2]

        if _inner_gap(ra, rb) < min_gap:
            # Разводим как группу без изменения зазора (двигаем только центр)
            total = _vis_w(L) + gap_px + _vis_w(R)
            # ставим по центру, затем клампим группу
            group_left = max(left_limit, min(right_limit - total, int(round(center_x - (total/2)))))
            lx = group_left - _vis_rect(L)[0]
            rx = group_left + _vis_w(L) + gap_px - _vis_rect(R)[0]
            yl = place_y_for_floor(L, virtual_floor_y); yr = place_y_for_floor(R, virtual_floor_y)
            ra = (lx + _vis_rect(L)[0], yl + _vis_rect(L)[1], lx + _vis_rect(L)[2], yl + _vis_rect(L)[3])
            rb = (rx + _vis_rect(R)[0], yr + _vis_rect(R)[1], rx + _vis_rect(R)[2], yr + _vis_rect(R)[3])

            # если всё ещё тесно — минимальный даунскейл пары и повторная постановка
            trips = 0
            while _inner_gap(ra, rb) < min_gap and trips < 20:
                L = _scale_abs(L, 0.98)
                R = _scale_abs(R, 0.98)
                total = _vis_w(L) + min_gap + _vis_w(R)
                if total > available_width:
                    # гарантированный вариант: сжимаем так, чтобы ровно поместилось
                    k = (available_width - min_gap) / max(1, (_vis_w(L) + _vis_w(R)))
                    L = _scale_abs(L, k); R = _scale_abs(R, k)
                gap_px = max(min_gap, min(ideal_gap, available_width - (_vis_w(L) + _vis_w(R))))
                lx, yl, rx, yr, ra, rb = _place_pair(center_x, gap_px, left_limit, right_limit, virtual_floor_y)
                trips += 1

        # Рисуем строго слева-направо, перекрытий геометрически нет
        draw_with_shadow(canvas, L, lx, yl)
        draw_with_shadow(canvas, R, rx, yr)
        try:
            _draw_debug_boxes(canvas, [_rect_at(lx, yl, L), _rect_at(rx, yr, R)])
        except Exception:
            pass

        # --- CHEST-UP: мягкий туман снизу, чтобы спрятать «несуществующие» ноги ---
        if is_chest:
            fog_y = int(H * CHEST_FOG_START_FRAC)
            add_bottom_fog(canvas, fog_y, color=CHEST_FOG_COLOR, max_alpha=CHEST_FOG_MAX_ALPHA)
        elif is_waist:
            fog_y = int(H * WAIST_FOG_START_FRAC)
            add_bottom_fog(canvas, fog_y, color=CHEST_FOG_COLOR, max_alpha=WAIST_FOG_MAX_ALPHA)

    # --- метрики/сохранение ---
    # Добавляем дату и время в имя файла
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = f"uploads/start_{timestamp}_{base_id}.png"
    metrics = {"W": W, "H": H, "framing": framing_key}

    def _abs_rect(x, y, img):
        (bx, by, bx1, by1), yb = alpha_metrics(img)
        return [x + bx, y + by, x + bx1, y + by1], yb + y

    if len(cuts) == 1:
        rP, fy = _abs_rect(x, y, P)
        h_px = rP[3] - rP[1]
        w_px = rP[2] - rP[0]
        metrics["L"] = {
            "rect_abs": rP, "height_px": int(h_px), "width_px": int(w_px),
            "height_frac": float(h_px) / H,
            "center_x_frac": float((rP[0]+rP[2])/2) / W,
            "scale": float(P.width) / max(1.0, cuts[0].width),
            "floor_y": int(fy)
        }
    else:
        rL, fyl = _abs_rect(lx, yl, L)
        rR, fyr = _abs_rect(rx, yr, R)
        hL = rL[3]-rL[1]; wL = rL[2]-rL[0]
        hR = rR[3]-rR[1]; wR = rR[2]-rR[0]
        gap_px = max(0, rR[0] - rL[2])
        metrics["L"] = {
            "rect_abs": rL, "height_px": int(hL), "width_px": int(wL),
            "height_frac": float(hL)/H,
            "center_x_frac": float((rL[0]+rL[2])/2)/W,
            "scale": float(L.width)/max(1.0, cuts[0].width),
            "floor_y": int(fyl)
        }
        metrics["R"] = {
            "rect_abs": rR, "height_px": int(hR), "width_px": int(wR),
            "height_frac": float(hR)/H,
            "center_x_frac": float((rR[0]+rR[2])/2)/W,
            "scale": float(R.width)/max(1.0, cuts[1].width),
            "floor_y": int(fyr)
        }
        metrics["gap_px"]  = int(gap_px)
        metrics["gap_frac"]= float(gap_px)/W

    if OAI_DEBUG or PREVIEW_START_FRAME:
        _save_layout_debug(canvas, metrics, base_id)
    canvas.save(out, "PNG")
    print(f"[frame] saved → {out} ({canvas.width}×{canvas.height})")

    # Очистка старых стартовых кадров
    cleanup_uploads_folder()

    return out, metrics

# ---------- ПОСТ-ОБРАБОТКА через ffmpeg (wm + музыка + титр + склейка) ----------
def create_title_image(width: int, height: int, text: str, output_path: str):
    """Создает изображение с титром с автоматическим подбором размера шрифта"""
    title_img = Image.new("RGB", (width, height), (0, 0, 0))
    d = ImageDraw.Draw(title_img)

    # Автоматический подбор размера шрифта
    max_width = width - 40  # Отступ 20 пикселей с каждой стороны
    font_size = 60  # Начинаем с большого шрифта

    while font_size > 12:  # Минимальный размер шрифта
        font = _load_title_font(font_size, preferred="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", bold=True)

        # Измеряем ширину текста с текущим шрифтом
        bbox = d.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]

        if text_width <= max_width:
            break  # Шрифт подходит

        font_size -= 2  # Уменьшаем размер шрифта

    # Рисуем текст по центру
    d.text((width//2, height//2), text, fill=(255,255,255), font=font, anchor="mm")
    title_img.save(output_path)
    return output_path

def _load_title_font(size: int, preferred: str | None = None, *, bold: bool = False):
    """Returns a truetype font matching legacy DejaVu setup with fallbacks for macOS."""
    candidates = []
    if preferred:
        candidates.append(preferred)
    candidates.extend(TITLE_FONT_BOLD_CANDIDATES if bold else TITLE_FONT_REGULAR_CANDIDATES)
    for path in candidates:
        if not path:
            continue
        try:
            if os.path.isfile(path):
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()

def _fit_text_in_box(draw, text, box_w, box_h, font_path, max_size, min_size=18, line_spacing=1.15, bold=False, anchor="mm"):
    size = max_size
    while size >= min_size:
        font = _load_title_font(size, preferred=font_path, bold=bold)
        # перенос по словам
        words = text.split()
        lines, cur = [], []
        for w in words:
            test = " ".join(cur + [w]) if cur else w
            bbox = draw.textbbox((0,0), test, font=font)
            if bbox[2] - bbox[0] <= box_w:
                cur.append(w)
            else:
                if not cur:  # слово само по себе длиннее строки
                    cur = [w]
                lines.append(" ".join(cur))
                cur = [w]
        if cur:
            lines.append(" ".join(cur))

        # высота всех строк
        heights = []
        for line in lines:
            b = draw.textbbox((0,0), line, font=font)
            heights.append(b[3]-b[1])
        total_h = int(sum(heights) + (len(lines)-1) * (heights[0] if heights else 0) * (line_spacing-1))

        if total_h <= box_h:
            return font, lines, total_h
        size -= 2
    return font, [text], min(box_h, 0)

def create_memorial_title_image(width, height, fio, dates, mem_text, output_path, candle_path=None):
    # было: Image.new("RGB", (width, height), (0, 0, 0))
    img = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)

    candle = None
    if candle_path and os.path.isfile(candle_path):
        try:
            candle = Image.open(candle_path).convert("RGBA")  # уже RGBA — ок
        except Exception:
            candle = None

    pad = int(os.environ.get("TITLE_PAD", "24"))
    left, right, top, bottom = pad, width - pad, pad, height - pad

    # Размер и позиция свечи
    candle_w = int(width * CANDLE_WIDTH_FRAC) if candle else 0
    candle_h = 0
    if candle:
        k = candle_w / candle.width
        candle = candle.resize((candle_w, int(candle.height * k)), RESAMPLE.LANCZOS)
        candle_h = candle.height
        # Компонуем внизу слева
        img.alpha_composite(candle, (left, height - pad - candle.height))

    # Области под текст
    # 1) FIO (верх, по центру)
    fio_box_w = width - 2*pad
    # сдвигаем всё, что «сверху», ниже углового логотипа
    safe_top = _wm_safe_top_px()
    fio_box_h = int(height * 0.16)
    fio_y0 = top + safe_top
    fio_font, fio_lines, fio_h = _fit_text_in_box(
        d, fio, fio_box_w, fio_box_h,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", max_size=72, min_size=26, line_spacing=1.12, bold=True
    )
    y = fio_y0 + (fio_box_h - fio_h)//2
    for line in fio_lines:
        b = d.textbbox((0,0), line, font=fio_font)
        d.text((width//2, y + (b[3]-b[1])//2), line, fill=(255,255,255), font=fio_font, anchor="mm")
        y += int((b[3]-b[1]) * 1.12)

    # 2) Dates (сразу под ФИО)
    dates_box_h = int(height * 0.08)
    dates_y0 = fio_y0 + fio_box_h + int(pad*0.6)
    dates_font, dates_lines, dates_h = _fit_text_in_box(
        d, dates, fio_box_w, dates_box_h,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max_size=42, min_size=20, line_spacing=1.0
    )
    y = dates_y0 + (dates_box_h - dates_h)//2
    for line in dates_lines:
        b = d.textbbox((0,0), line, font=dates_font)
        d.text((width//2, y + (b[3]-b[1])//2), line, fill=(200,200,200), font=dates_font, anchor="mm")
        y += (b[3]-b[1])

    # 3) Memorial text (предпочтение: по всей ширине; fallback — справа от свечи)
    mem_top = max(int(height * 0.52), dates_y0 + dates_box_h + pad)  # верх памятного текста
    mem_full_left  = left
    mem_full_right = right
    mem_full_w     = mem_full_right - mem_full_left
    mem_full_h     = bottom - mem_top

    # PASS A: пробуем уместить по всей ширине
    mem_font, mem_lines, mem_h = _fit_text_in_box(
        d, mem_text, mem_full_w, mem_full_h,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max_size=40, min_size=18, line_spacing=1.18
    )
    # если уместилось — рисуем по центру всей ширины
    if mem_h <= mem_full_h:
        y = mem_top + (mem_full_h - mem_h)//2
        for line in mem_lines:
            b = d.textbbox((0,0), line, font=mem_font)
            d.text((width//2, y + (b[3]-b[1])//2), line, fill=(255,255,255), font=mem_font, anchor="mm")
            y += int((b[3]-b[1]) * 1.18)
    else:
        # PASS B: рисуем только там, где свечи нет (справа от неё)
        candle_reserved_w = (candle_w + pad) if candle else 0
        mem_left  = max(left, candle_reserved_w + pad)
        mem_right = right
        mem_box_w = max(100, mem_right - mem_left)
        mem_box_h = max(100, bottom - mem_top)

        mem_font, mem_lines, mem_h = _fit_text_in_box(
            d, mem_text, mem_box_w, mem_box_h,
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max_size=40, min_size=18, line_spacing=1.18
        )
        y = mem_top + (mem_box_h - mem_h)//2
        cx = mem_left + mem_box_w//2
        for line in mem_lines:
            b = d.textbbox((0,0), line, font=mem_font)
            d.text((cx, y + (b[3]-b[1])//2), line, fill=(255,255,255), font=mem_font, anchor="mm")
            y += int((b[3]-b[1]) * 1.18)

    img.save(output_path)
    return output_path

def postprocess_concat_ffmpeg(video_paths: List[str], music_path: str|None, title_text: str, save_as: str, bg_overlay_file: str|None = None, titles_meta: dict|None = None, candle_path: str|None = None) -> str:
    """Постобработка видео через ffmpeg (склейка + фон-анимация + водяной знак + музыка). С фолбэком, faststart и портативной копией."""
    import tempfile

    def _escape_concat_path(p: str) -> str:
        # экранируем одинарные кавычки для concat-файла
        return os.path.abspath(p).replace("'", "'\\''")

    temp_dir = "renders/temp"
    os.makedirs(temp_dir, exist_ok=True)

    # Если несколько сцен — сначала делаем промежуточную склейку с кроссфейдами,
    # а дальше работаем как с одним видео.
    if len(video_paths) > 1:
        premerged = _merge_with_fades(video_paths, fade_sec=CROSSFADE_SEC)
        video_paths = [premerged]

    # 1) Финальный титр (PNG)
    title_img_path = f"{temp_dir}/title.png"
    if titles_meta:
        create_memorial_title_image(
            720, 1280,
            titles_meta.get("fio","") or "",
            titles_meta.get("dates","") or "",
            titles_meta.get("mem","") or "",
            title_img_path,
            candle_path=candle_path or CANDLE_PATH
        )
    else:
        create_title_image(720, 1280, title_text, title_img_path)

    # 2) 2-секундный ролик из титра
    title_video_path = f"{temp_dir}/title_video.mp4"
    _run_ffmpeg([
        "ffmpeg", "-y", "-loop", "1", "-i", title_img_path,
        "-t", "2", "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        title_video_path
    ], tag="title_video", out_hint=title_video_path)

    # 3) Файл для concat
    concat_list_path = f"{temp_dir}/concat_list.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for vp in video_paths:
            f.write(f"file '{_escape_concat_path(vp)}'\n")
        f.write(f"file '{_escape_concat_path(title_video_path)}'\n")

    # 4) Склейка (попытка без перекодирования)
    concat_video_path = f"{temp_dir}/concat_video.mp4"
    try:
        _run_ffmpeg([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-c", "copy", "-movflags", "+faststart",
            concat_video_path
        ], tag="concat_copy", out_hint=concat_video_path)
    except subprocess.CalledProcessError:
        # Фолбэк: перекодирование под общий профиль
        _run_ffmpeg([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-r", "24",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-movflags", "+faststart",
            concat_video_path
        ], tag="concat_reencode", out_hint=concat_video_path)

    # 4.5) Деликатная анимация фона (если есть картинка)
    bg_anim_video_path = concat_video_path
    if bg_overlay_file and os.path.isfile(bg_overlay_file):
        try:
            bg_anim_video_path = f"{temp_dir}/with_bg_anim.mp4"
            _run_ffmpeg([
                "ffmpeg", "-y",
                "-i", concat_video_path,
                "-loop", "1", "-i", bg_overlay_file,
                "-filter_complex",
                "[1:v]scale=720:1280,boxblur=25:1,format=rgba,colorchannelmixer=aa=0.08,setsar=1[ov];"
                "[0:v][ov]overlay=x='t*2':y=0:shortest=1,format=yuv420p[v]",
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                "-movflags", "+faststart",
                bg_anim_video_path
            ], tag="bg_overlay", out_hint=bg_anim_video_path)
        except Exception as e:
            print(f"BG overlay skipped: {e}")
    else:
        print("BG overlay disabled (no file)")

    # 5) Водяной знак
    wm_video_path = bg_anim_video_path
    if os.path.isfile(WATERMARK_PATH):
        wm_video_path = f"{temp_dir}/with_watermark.mp4"
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", bg_anim_video_path, "-i", WATERMARK_PATH,
            "-filter_complex", "[1:v]scale=120:-1[wm];[0:v][wm]overlay=W-w-24:24",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            wm_video_path
        ], tag="wm_corner", out_hint=wm_video_path)

    # 6) Музыка (или просто сохранить)
    if music_path and os.path.isfile(music_path):
        # зациклить музыку и подложить под видео
        _run_ffmpeg([
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", music_path,     # бесконечная музыка
            "-i", wm_video_path,                         # видео
            "-map", "1:v", "-map", "0:a",
            "-c:v", "copy",
            "-c:a", "aac", "-ar", "44100",
            "-shortest", "-af", "volume=0.6",
            "-movflags", "+faststart",
            save_as
        ], tag="mux_music", out_hint=save_as)
    else:
        # портативная копия + faststart
        import shutil
        shutil.copyfile(wm_video_path, save_as)
        try:
            tmp_fast = f"{temp_dir}/faststart.mp4"
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", save_as, "-c", "copy", "-movflags", "+faststart", tmp_fast
            ], tag="faststart_copy", out_hint=tmp_fast)
            shutil.move(tmp_fast, save_as)
        except Exception:
            pass

    return save_as

def cleanup_dir_keep_last_n(dir_path: str, keep_n: int = 20, extensions: tuple[str, ...] = ()):
    try:
        items = []
        for name in os.listdir(dir_path):
            p = os.path.join(dir_path, name)
            if os.path.isfile(p):
                if not extensions or name.lower().endswith(extensions):
                    items.append((p, os.path.getmtime(p)))
        items.sort(key=lambda x: x[1], reverse=True)
        for p, _ in items[keep_n:]:
            try:
                os.remove(p)
            except Exception:
                pass
    except FileNotFoundError:
        pass

def cleanup_artifacts(keep_last: int = 20):
    # Полностью чистим временную папку рендеров (кроме режима отладки)
    if not OAI_DEBUG:
        shutil.rmtree("renders/temp", ignore_errors=True)
    # Оставляем только N последних оригиналов и финалов
    cleanup_dir_keep_last_n("uploads", keep_n=keep_last, extensions=(".jpg", ".jpeg", ".png", ".webp"))
    cleanup_dir_keep_last_n("renders", keep_n=keep_last, extensions=(".mp4", ".mov", ".mkv", ".webm"))
