from __future__ import annotations

import asyncio
import io
import os
import threading
import uuid
import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .. import assets, state
from ..config import (
    FREE_HUGS_LIMIT,
    FREE_HUGS_SCENE,
    FREE_HUGS_WM_ALPHA,
    FREE_HUGS_WM_GRID_COLS,
    FREE_HUGS_WM_GRID_MARGIN,
    FREE_HUGS_WM_GRID_ROWS,
    FREE_HUGS_WM_MODE,
    FREE_HUGS_WM_ROTATE,
    FREE_HUGS_WM_SCALE,
    PAYMENT_GATE_ENABLED,
    WATERMARK_PATH,
    CANDLE_PATH,
    ADMIN_CHAT_ID,
    ensure_directories,
)
from ..media.storage import save_upload_image_bytes
from ..render.pipeline import (
    make_start_frame as pipeline_make_start_frame,
    web_render_video,
    _abs_project_path,
)
from ..payment.tochka import create_payment_link, get_payment_status, is_paid_status, TochkaError

ensure_directories()
Path("renders/temp").mkdir(parents=True, exist_ok=True)

router = APIRouter(prefix="/v1")

# FS paths
ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = ROOT
UPLOADS_DIR = ROOT / "uploads"
RENDERS_DIR = ROOT / "renders"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
RENDERS_DIR.mkdir(parents=True, exist_ok=True)

# in-memory store for render jobs
RENDER_JOBS: Dict[str, Dict[str, Any]] = {}
PAYMENT_SESSIONS: Dict[str, Dict[str, Any]] = {}

_ALLOWED_ORIGINS = [
    "https://memoryforever.ru",
    "https://www.memoryforever.ru",
    "https://memoryforever.onrender.com",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
_ALLOWED_ORIGIN_REGEX = r"^https://([a-z0-9-]+\.)?memoryforever\.ru$|^https://.*\.creatium\.app$|^http://localhost(:\d+)?$|^http://127\.0\.0\.1(:\d+)?$"

DEFAULT_TITLE_TEXT = "Memory Forever — Память навсегда с вами"

JOB_STATUS_AWAITING_PHOTOS = "awaiting_photos"
JOB_STATUS_READY_FOR_START = "ready_for_start_frame"
JOB_STATUS_AWAITING_APPROVAL = "awaiting_approval"
JOB_STATUS_APPROVED = "approved"
JOB_STATUS_RENDERING = "rendering"
JOB_STATUS_RENDERED = "rendered"
JOB_STATUS_ERROR = "error"

SESSION_STATUS_AWAITING_PHOTOS = "awaiting_photos"
SESSION_STATUS_READY_FOR_START = "ready_for_start_frame"
SESSION_STATUS_AWAITING_APPROVAL = "awaiting_approval"
SESSION_STATUS_READY_FOR_GENERATION = "ready_for_generation"
SESSION_STATUS_PROCESSING = "processing"
SESSION_STATUS_FINISHED = "finished"
SESSION_STATUS_ERROR = "error"

sessions: Dict[str, Dict[str, Any]] = {}
sessions_lock = threading.Lock()


class StartSessionRequest(BaseModel):
    scenes: List[str] = Field(..., description="Ordered list of scene keys")
    format: str = Field(..., description="Framing key")
    background: str = Field(..., description="Background key or __CUSTOM__")
    music: Optional[str] = Field(None, description="Music key or __CUSTOM__")
    titles_mode: Optional[str] = Field("none", description="none|custom")
    titles_fio: Optional[str] = None
    titles_dates: Optional[str] = None
    titles_text: Optional[str] = None
    user_id: Optional[int] = Field(None, description="External user identifier for quota tracking")


class GenericSessionRequest(BaseModel):
    session_id: str


class SceneActionRequest(GenericSessionRequest):
    scene_index: Optional[int] = Field(None, ge=0)
    scene_key: Optional[str] = None


class GenerateRequest(GenericSessionRequest):
    pass


class StatusResponse(BaseModel):
    status: str
    message: Optional[str] = None
    progress: float = 0.0
    scenes: List[Dict[str, Any]]
    state: Dict[str, Any]
    result_path: Optional[str] = None


class RenderRequest(BaseModel):
    format_key: str
    scene_key: str
    background_key: str
    music_key: str
    title: Optional[str] = ""
    subtitle: Optional[str] = ""
    photos: List[str]
    user: Optional[str] = None


class RenderPaidResponse(BaseModel):
    status: str
    job_id: Optional[str] = None
    status_url: Optional[str] = None
    payment_url: Optional[str] = None
    payment_id: Optional[str] = None
    payment_key: Optional[str] = None
    price_rub: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


class StartFrameRequest(BaseModel):
    scene_key: str
    format_key: str
    background_key: str
    photos: List[str]


class SupportRequest(BaseModel):
    text: str
    user_contact: Optional[str] = None


def _validate_keys(req: StartSessionRequest) -> None:
    for key in req.scenes:
        if key not in assets.SCENES:
            raise HTTPException(status_code=400, detail=f"Unknown scene key: {key}")
    if req.format not in assets.FORMATS:
        raise HTTPException(status_code=400, detail=f"Unknown format key: {req.format}")
    if req.background != assets.CUSTOM_BG_KEY and req.background not in assets.BG_FILES:
        raise HTTPException(status_code=400, detail=f"Unknown background key: {req.background}")
    music_key = req.music
    if music_key and music_key not in assets.MUSIC and music_key != assets.CUSTOM_MUSIC_KEY:
        raise HTTPException(status_code=400, detail=f"Unknown music key: {music_key}")


def _build_scene_jobs(scenes: List[str]) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    for key in scenes:
        meta = assets.SCENES[key]
        jobs.append(
            {
                "scene_key": key,
                "people": meta["people"],
                "photos": [],
                "start_frame": None,
                "duration": meta["duration"],
                "prompt": assets.SCENE_PROMPTS.get(meta.get("kind"), ""),
                "video_path": None,
                "approved": False,
                "status": JOB_STATUS_AWAITING_PHOTOS,
                "layout_metrics": None,
                "error": None,
            }
        )
    return jobs


def _ensure_session(session_id: str) -> Dict[str, Any]:
    with sessions_lock:
        session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _select_job(session: Dict[str, Any], *, scene_index: Optional[int], scene_key: Optional[str]) -> Tuple[Dict[str, Any], int]:
    jobs = session["scene_jobs"]
    if scene_index is not None:
        if 0 <= scene_index < len(jobs):
            return jobs[scene_index], scene_index
        raise HTTPException(status_code=400, detail="scene_index out of range")
    if scene_key:
        for idx, job in enumerate(jobs):
            if job["scene_key"] == scene_key:
                return job, idx
        raise HTTPException(status_code=400, detail="scene_key not found in session")
    raise HTTPException(status_code=400, detail="scene selector missing")


def _public_state(st: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "scenes": list(st.get("scenes", [])),
        "format": st.get("format"),
        "background": st.get("bg"),
        "music": st.get("music"),
        "titles_mode": st.get("titles_mode"),
        "titles_fio": st.get("titles_fio"),
        "titles_dates": st.get("titles_dates"),
        "titles_text": st.get("titles_text"),
    }


def _serialize_scene(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "scene_key": job["scene_key"],
        "people": job["people"],
        "photos": list(job.get("photos", [])),
        "start_frame": job.get("start_frame"),
        "duration": job.get("duration"),
        "prompt": job.get("prompt"),
        "video_path": job.get("video_path"),
        "approved": bool(job.get("approved")),
        "status": job.get("status", JOB_STATUS_AWAITING_PHOTOS),
        "metrics": job.get("layout_metrics"),
        "error": job.get("error"),
    }


def _serialize_session(session: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": session.get("status"),
        "message": session.get("message"),
        "progress": float(session.get("progress", 0.0)),
        "scenes": [_serialize_scene(job) for job in session.get("scene_jobs", [])],
        "result_path": session.get("result_path"),
        "state": _public_state(session["state"]),
    }


def _update_session_status(session: Dict[str, Any]) -> None:
    current_status = session.get("status")
    if current_status in {SESSION_STATUS_PROCESSING, SESSION_STATUS_FINISHED, SESSION_STATUS_ERROR}:
        return
    jobs = session.get("scene_jobs", [])
    if any(job.get("status") == JOB_STATUS_ERROR for job in jobs):
        session["status"] = SESSION_STATUS_ERROR
        return
    if any(_scene_requires_more_photos(job) for job in jobs):
        session["status"] = SESSION_STATUS_AWAITING_PHOTOS
        return
    if any(job.get("status") == JOB_STATUS_READY_FOR_START for job in jobs):
        session["status"] = SESSION_STATUS_READY_FOR_START
        return
    if any(job.get("status") == JOB_STATUS_AWAITING_APPROVAL for job in jobs):
        session["status"] = SESSION_STATUS_AWAITING_APPROVAL
        return
    if any(not job.get("approved") for job in jobs):
        session["status"] = SESSION_STATUS_READY_FOR_START
        return
    if session.get("result_path"):
        session["status"] = SESSION_STATUS_FINISHED
    else:
        session["status"] = SESSION_STATUS_READY_FOR_GENERATION


def _save_upload(file_bytes: bytes, suffix: str = ".jpg") -> str:
    Path("uploads").mkdir(parents=True, exist_ok=True)
    file_path = Path("uploads") / f"web_{uuid.uuid4().hex}{suffix}"
    file_path.write_bytes(file_bytes)
    return str(file_path)


def _scene_requires_more_photos(job: Dict[str, Any]) -> bool:
    return len(job["photos"]) < int(job["people"])


def _resolve_music_path(session: Dict[str, Any]) -> Optional[str]:
    st = session["state"]
    music_key = st.get("music")
    if not music_key or music_key == "none":
        return None
    if music_key == assets.CUSTOM_MUSIC_KEY:
        return st.get("custom_music_path")
    return assets.MUSIC.get(music_key)


def _resolve_background_path(session: Dict[str, Any]) -> Optional[str]:
    st = session["state"]
    if st.get("bg") == assets.CUSTOM_BG_KEY:
        return st.get("bg_custom_path")
    return assets.BG_FILES.get(st.get("bg"))


def _generate_scene_segment(session: Dict[str, Any], job: Dict[str, Any]) -> str:
    st = session["state"]
    uid = session["quota_uid"]
    scene_key = job["scene_key"]
    start_frame = job.get("start_frame")
    if not job.get("approved"):
        raise RuntimeError("SCENE_NOT_APPROVED")
    if _scene_requires_more_photos(job):
        raise RuntimeError("SCENE_PHOTOS_INCOMPLETE")
    if not start_frame or not os.path.isfile(start_frame):
        raise RuntimeError(f"Scene {scene_key}: start frame missing")

    if (
        scene_key == FREE_HUGS_SCENE
        and state.get_free_hugs_count(uid) >= FREE_HUGS_LIMIT
        and not state.is_free_hugs_whitelisted(uid)
    ):
        raise RuntimeError("FREE_HUGS_LIMIT_REACHED")

    send_path = ensure_jpeg_copy(start_frame)
    data_uri, used_path = ensure_runway_datauri_under_limit(send_path)
    if not data_uri:
        raise RuntimeError("EMPTY_START_FRAME_DATA")

    start_resp = runway_start(data_uri, job.get("prompt", ""), int(job.get("duration", 10)))
    task_id = start_resp.get("id") or start_resp.get("task", {}).get("id")
    if not task_id:
        raise RuntimeError("RUNWAY_NO_TASK_ID")

    poll = runway_poll(task_id)
    status = (poll or {}).get("status")
    if status != "SUCCEEDED":
        raise RuntimeError(f"RUNWAY_STATUS_{status}")

    output = poll.get("output") or []
    url: Optional[str] = None
    if output:
        first = output[0]
        if isinstance(first, str):
            url = first
        else:
            url = first.get("url")
    if not url:
        raise RuntimeError("RUNWAY_NO_URL")

    Path("renders").mkdir(parents=True, exist_ok=True)
    timestamp = session["created_at"]
    seg_path = Path("renders") / f"web_{uid}_{timestamp}_{uuid.uuid4().hex}.mp4"
    download(url, str(seg_path))

    if state.is_free_hugs(scene_key) and state.is_free_hugs_whitelisted(uid) is False:
        apply_fullscreen_watermark(
            in_video=str(seg_path),
            out_video=str(seg_path),
            wm_path=WATERMARK_PATH,
            mode=FREE_HUGS_WM_MODE,
            alpha=FREE_HUGS_WM_ALPHA,
            grid_cols=FREE_HUGS_WM_GRID_COLS,
            grid_rows=FREE_HUGS_WM_GRID_ROWS,
            grid_margin=FREE_HUGS_WM_GRID_MARGIN,
            scale=FREE_HUGS_WM_SCALE,
            rotate=FREE_HUGS_WM_ROTATE,
        )

    if state.is_free_hugs(scene_key) and not state.is_free_hugs_whitelisted(uid):
        state.inc_free_hugs_count(uid)

    return str(seg_path)


def _run_generation(session_id: str) -> None:
    session = _ensure_session(session_id)
    try:
        session["status"] = SESSION_STATUS_PROCESSING
        session["message"] = None
        session["progress"] = 0.0
        jobs = session["scene_jobs"]
        total = max(1, len(jobs))
        segments: List[str] = []
        for idx, job in enumerate(jobs, start=1):
            try:
                job["status"] = JOB_STATUS_RENDERING
                job["error"] = None
                seg = _generate_scene_segment(session, job)
            except Exception as exc:  # noqa: BLE001
                job["status"] = JOB_STATUS_ERROR
                job["error"] = str(exc)
                raise
            else:
                job["video_path"] = seg
                job["status"] = JOB_STATUS_RENDERED
                segments.append(seg)
            finally:
                session["progress"] = idx / total

        bg_overlay = _resolve_background_path(session)
        music_path = _resolve_music_path(session)
        title_text = DEFAULT_TITLE_TEXT
        titles_meta = None
        st = session["state"]
        if st.get("titles_mode") == "custom":
            titles_meta = {
                "fio": (st.get("titles_fio") or "").strip(),
                "dates": (st.get("titles_dates") or "").strip(),
                "mem": (st.get("titles_text") or "").strip(),
            }
        final_path = Path("renders") / f"web_{session['quota_uid']}_{uuid.uuid4().hex}_FINAL.mp4"
        postprocess_concat_ffmpeg(
            segments,
            music_path,
            title_text,
            str(final_path),
            bg_overlay_file=bg_overlay,
            titles_meta=titles_meta,
            candle_path=CANDLE_PATH,
        )
        session["result_path"] = str(final_path)
        session["status"] = SESSION_STATUS_FINISHED
        session["progress"] = 1.0
    except Exception as exc:  # noqa: BLE001
        session["status"] = SESSION_STATUS_ERROR
        session["message"] = str(exc)
    finally:
        session.pop("worker", None)
        _update_session_status(session)


@router.get("/catalog")
def get_catalog() -> Dict[str, Any]:
    return JSONResponse(
        content=assets.CATALOG,
        media_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.head("/catalog")
async def head_catalog():
    return PlainTextResponse("", status_code=200)


@router.post("/start-frame")
async def start_frame(req: StartFrameRequest):
    try:
        scene = assets.SCENES.get(req.scene_key)
        if not scene:
            raise HTTPException(status_code=400, detail="Unknown scene_key")

        bg_rel = assets.BG_FILES.get(req.background_key)
        if not bg_rel:
            raise HTTPException(status_code=400, detail="Unknown background_key")

        if not req.photos:
            raise HTTPException(status_code=400, detail="No photos provided")

        abs_photos: List[str] = []
        for rel in req.photos:
            rel_path = rel.lstrip("/")
            abs_path = (BASE_DIR / rel_path).resolve()
            if not abs_path.exists():
                raise HTTPException(status_code=400, detail=f"photo not found: {rel}")
            abs_photos.append(str(abs_path))

        bg_abs = _abs_project_path(bg_rel)
        if not os.path.isfile(bg_abs):
            raise HTTPException(status_code=400, detail="Background file not found")

        start_path, metrics = pipeline_make_start_frame(abs_photos, req.format_key, bg_abs, layout=None)
        rel_url = "/" + str(Path(start_path).as_posix())
        return {"start_frame_url": rel_url, "metrics": metrics, "width": 720, "height": 1280}
    except HTTPException:
        # прокидываем как есть, CORS middleware добавит заголовки
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[WEB] start-frame failed: {exc}")
        raise HTTPException(status_code=500, detail=f"start frame failed: {exc}") from exc


@router.post("/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    saved: List[str] = []
    for upload in files:
        contents = await upload.read()
        if not contents:
            raise HTTPException(status_code=400, detail="Empty file uploaded")

        ctype = (upload.content_type or "").lower()
        if not ctype.startswith("image/"):
            raise HTTPException(status_code=400, detail="Uploaded file is not an image")

        ext = Path(upload.filename or "").suffix.lower() or ".jpg"
        saved_path = save_upload_image_bytes(contents, owner_label="web", ext_hint=ext)
        saved.append("/" + Path(saved_path).as_posix())
    return {"files": saved}


@router.options("/start-frame")
async def options_start_frame():
    return PlainTextResponse("", status_code=200)


@router.options("/render/start")
async def options_render_start():
    return PlainTextResponse("", status_code=200)


@router.options("/render/start_paid")
async def options_render_start_paid():
    return PlainTextResponse("", status_code=200)


@router.post("/support")
async def support_message(req: SupportRequest):
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="Text is required")
    if not ADMIN_CHAT_ID:
        raise HTTPException(status_code=400, detail="Support channel is not configured")
    msg = f"🛟 Веб-запрос в поддержку\n{req.text.strip()}"
    if req.user_contact:
        msg += f"\n\nКонтакт: {req.user_contact.strip()}"
    try:
        from ..app import bot  # локальный импорт, чтобы не ломать ранний старт
        bot.send_message(int(ADMIN_CHAT_ID), msg)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to send support message: {exc}") from exc
    return {"ok": True}


@router.post("/render/start")
async def render_start(payload: RenderRequest):
    return await _enqueue_render(payload)


@router.post("/render/start_paid", response_model=RenderPaidResponse)
async def render_start_paid(payload: RenderRequest):
    try:
        price = _scene_price(payload.scene_key)
        print(f"[WEB_PAID] start_paid request: scene={payload.scene_key} price={price}", flush=True)

        if price <= 0:
            queued = await _enqueue_render(payload)
            print(f"[WEB_PAID] render_started (free): job_id={queued.get('job_id')}", flush=True)
            return RenderPaidResponse(status="render_started", **queued)

        payment_key = _payment_key_from_payload(payload)
        payment = PAYMENT_SESSIONS.get(payment_key)

        if not payment:
            try:
                pay_id, pay_url = create_payment_link(price, purpose=f"Memory Forever: {payload.scene_key}")
            except TochkaError as exc:
                print(f"[WEB_PAID] ERROR create_payment: {repr(exc)}", flush=True)
                return JSONResponse(
                    {"status": "error", "message": "payment_create_failed", "detail": str(exc)}, status_code=500
                )
            payment = {
                "payment_id": pay_id,
                "payment_url": pay_url,
                "status": "need_payment",
                "price_rub": price,
                "payload": payload.model_dump(),
            }
            PAYMENT_SESSIONS[payment_key] = payment
            payment_payload = {"@context": "https://schema.org/Payment", "id": pay_id, "url": pay_url}
            print(f"[WEB_PAID] need_payment: payment_id={pay_id} url={pay_url}", flush=True)
            return RenderPaidResponse(
                status="need_payment",
                payment_url=pay_url,
                payment_id=pay_id,
                payment_key=payment_key,
                price_rub=price,
                payment=payment_payload,  # type: ignore[arg-type]
                message="Счёт создан, требуется оплата.",
            )

        # Если платёж уже существует, проверим статус
        if payment.get("status") != "paid":
            try:
                status_json = get_payment_status(payment["payment_id"])
            except Exception as exc:  # noqa: BLE001
                print(f"[WEB_PAID] ERROR payment status: {repr(exc)}", flush=True)
                return JSONResponse(
                    {"status": "error", "message": "payment_status_failed", "detail": str(exc)}, status_code=500
                )

            if is_paid_status(status_json):
                payment["status"] = "paid"
            else:
                pay_url = payment.get("payment_url")
                pay_id = payment.get("payment_id")
                payment_payload = {"@context": "https://schema.org/Payment", "id": pay_id, "url": pay_url}
                print(f"[WEB_PAID] need_payment (pending): payment_id={pay_id} url={pay_url}", flush=True)
                return RenderPaidResponse(
                    status="need_payment",
                    payment_url=pay_url,
                    payment_id=pay_id,
                    payment_key=payment_key,
                    price_rub=price,
                    payment=payment_payload,  # type: ignore[arg-type]
                    message="Платёж не подтверждён. Попробуйте ещё раз через пару секунд.",
                )

        # Оплачено и подтверждено
        if payment.get("status") == "paid" and not payment.get("job_id"):
            queued = await _enqueue_render(payload)
            payment["job_id"] = queued["job_id"]
            payment["status"] = "rendering"
            PAYMENT_SESSIONS[payment_key] = payment
            print(f"[WEB_PAID] Payment confirmed → starting render for job_id={queued.get('job_id')}", flush=True)
            return RenderPaidResponse(
                status="render_started",
                job_id=queued["job_id"],
                status_url=queued.get("status_url"),
                payment_key=payment_key,
            )

        # Оплачено (возможно, рендер уже запускался)
        if payment.get("job_id"):
            job_id = payment["job_id"]
            job = RENDER_JOBS.get(job_id) or {}
            if job.get("status") == "done":
                result = job.get("result")
                print(f"[WEB_PAID] done: job_id={job_id} video_url={(result or {}).get('video_url')}", flush=True)
                return RenderPaidResponse(status="done", job_id=job_id, result=result, payment_key=payment_key)
            print(f"[WEB_PAID] render_started (existing): job_id={job_id}", flush=True)
            return RenderPaidResponse(
                status="render_started",
                job_id=job_id,
                status_url=f"/v1/render/status/{job_id}",
                payment_key=payment_key,
            )

        queued = await _enqueue_render(payload)
        payment["job_id"] = queued["job_id"]
        payment["status"] = "rendering"
        PAYMENT_SESSIONS[payment_key] = payment
        print(f"[WEB_PAID] render_started: job_id={queued.get('job_id')}", flush=True)
        return RenderPaidResponse(
            status="render_started",
            job_id=queued["job_id"],
            status_url=queued.get("status_url"),
            payment_key=payment_key,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[WEB_PAID] ERROR start_paid: {repr(exc)}", flush=True)
        return JSONResponse(
            {"status": "error", "message": "internal_error", "detail": str(exc)},
            status_code=500,
        )


@router.get("/render/status/{job_id}")
async def render_status(job_id: str):
    data = RENDER_JOBS.get(job_id)
    if not data:
        return JSONResponse({"error": "not found"}, status_code=404)
    return data


async def _run_render(job_id: str, payload: RenderRequest) -> None:
    job = RENDER_JOBS.get(job_id, {})
    job.update({"status": "processing", "progress": 5})
    RENDER_JOBS[job_id] = job

    try:
        if not payload.photos:
            raise ValueError("No photos provided")

        abs_photos: List[str] = []
        for rel_path in payload.photos:
            rel = rel_path.lstrip("/")
            abs_path = (BASE_DIR / rel).resolve()
            if not abs_path.exists():
                raise FileNotFoundError(f"photo not found: {abs_path}")
            abs_photos.append(str(abs_path))

        job["progress"] = 40
        RENDER_JOBS[job_id] = job
        print(f"[WEB_DEBUG] job {job_id} payload.photos = {payload.photos}")
        print(f"[WEB_DEBUG] job {job_id} abs_photos = {abs_photos}")

        video_path = web_render_video(
            format_key=payload.format_key,
            scene_key=payload.scene_key,
            background_key=payload.background_key,
            music_key=payload.music_key,
            title=payload.title or "",
            subtitle=payload.subtitle or "",
            photo_paths=abs_photos,
            session_id=payload.user,
        )

        job["status"] = "done"
        job["progress"] = 100
        job["result"] = {
            "video_path": video_path,
            "video_url": f"/renders/{Path(video_path).name}",
        }
        RENDER_JOBS[job_id] = job
        print(f"[WEB_DEBUG] job {job_id} completed: {video_path}")

    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(exc)
        RENDER_JOBS[job_id] = job
        print(f"[WEB_DEBUG] error for job {job_id}: {exc!r}")


@router.post("/session/start")
def start_session(req: StartSessionRequest) -> Dict[str, Any]:
    _validate_keys(req)
    session_id = uuid.uuid4().hex
    st = state.new_state()
    st["scenes"] = req.scenes
    st["format"] = req.format
    st["bg"] = req.background
    st["music"] = req.music or "none"
    st["titles_mode"] = req.titles_mode or "none"
    st["titles_fio"] = req.titles_fio
    st["titles_dates"] = req.titles_dates
    st["titles_text"] = req.titles_text

    jobs = _build_scene_jobs(req.scenes)

    session = {
        "state": st,
        "scene_jobs": jobs,
        "status": SESSION_STATUS_AWAITING_PHOTOS,
        "progress": 0.0,
        "message": None,
        "result_path": None,
        "created_at": uuid.uuid4().hex,
        "quota_uid": int(req.user_id) if req.user_id is not None else uuid.uuid4().int % 10**9,
    }
    with sessions_lock:
        sessions[session_id] = session
    _update_session_status(session)
    snapshot = _serialize_session(session)
    snapshot["session_id"] = session_id
    return snapshot


@router.post("/upload_photo")
async def upload_photo(
    session_id: str = Form(...),
    scene_index: Optional[int] = Form(None),
    scene_key: Optional[str] = Form(None),
    file: UploadFile = File(...),
) -> Dict[str, Any]:
    session = _ensure_session(session_id)
    if scene_index is None and not scene_key:
        raise HTTPException(status_code=400, detail="scene_index or scene_key is required")
    job, _ = _select_job(session, scene_index=scene_index, scene_key=scene_key)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    suffix = os.path.splitext(file.filename or "")[1] or ".jpg"
    path = _save_upload(content, suffix=suffix)
    job["photos"].append(path)
    job["status"] = JOB_STATUS_AWAITING_PHOTOS
    if not _scene_requires_more_photos(job):
        job["status"] = JOB_STATUS_READY_FOR_START
    job["error"] = None
    _update_session_status(session)
    return {
        "stored_path": path,
        "uploaded": len(job["photos"]),
        "required": job["people"],
        "scene_status": job["status"],
    }


@router.post("/make_start_frame")
def make_start_frame(req: SceneActionRequest) -> Dict[str, Any]:
    session = _ensure_session(req.session_id)
    if req.scene_index is None and not req.scene_key:
        raise HTTPException(status_code=400, detail="scene_index or scene_key is required")
    job, _ = _select_job(session, scene_index=req.scene_index, scene_key=req.scene_key)
    st = session["state"]
    if _scene_requires_more_photos(job):
        raise HTTPException(status_code=400, detail="Not enough photos uploaded")

    bg_path = _resolve_background_path(session)
    if not bg_path or not os.path.isfile(bg_path):
        raise HTTPException(status_code=400, detail="Background file not found")

    start_path, metrics = pipeline_make_start_frame(job["photos"], st["format"], bg_path, layout=None)
    job["start_frame"] = start_path
    job["layout_metrics"] = metrics
    job["status"] = JOB_STATUS_AWAITING_APPROVAL
    job["error"] = None
    _update_session_status(session)
    return {"start_frame": start_path, "metrics": metrics}


@router.post("/approve_start")
def approve_start(req: SceneActionRequest) -> Dict[str, Any]:
    session = _ensure_session(req.session_id)
    if req.scene_index is None and not req.scene_key:
        raise HTTPException(status_code=400, detail="scene_index or scene_key is required")
    job, _ = _select_job(session, scene_index=req.scene_index, scene_key=req.scene_key)
    if not job.get("start_frame"):
        raise HTTPException(status_code=400, detail="Start frame missing")
    job["approved"] = True
    if job.get("status") != JOB_STATUS_RENDERED:
        job["status"] = JOB_STATUS_APPROVED
    job["error"] = None
    _update_session_status(session)
    return {"approved": True, "scene_status": job["status"]}


@router.post("/generate")
def trigger_generation(req: GenerateRequest) -> Dict[str, Any]:
    session = _ensure_session(req.session_id)
    if session.get("worker"):
        raise HTTPException(status_code=400, detail="Generation already in progress")
    jobs = session["scene_jobs"]
    if not jobs:
        raise HTTPException(status_code=400, detail="No scenes configured")
    if any(_scene_requires_more_photos(job) for job in jobs):
        raise HTTPException(status_code=400, detail="Not all scenes have required photos")
    if any(not job.get("start_frame") for job in jobs):
        raise HTTPException(status_code=400, detail="Not all scenes have start frames")
    if any(not job.get("approved") for job in jobs):
        raise HTTPException(status_code=400, detail="Not all scenes approved")
    session["result_path"] = None
    session["progress"] = 0.0
    session["status"] = SESSION_STATUS_PROCESSING
    session["message"] = None
    worker = threading.Thread(target=_run_generation, args=(req.session_id,), daemon=True)
    session["worker"] = worker
    worker.start()
    return {"status": "started", "session_id": req.session_id}


@router.get("/status", response_model=StatusResponse)
def get_status(session_id: str) -> StatusResponse:
    session = _ensure_session(session_id)
    snapshot = _serialize_session(session)
    return StatusResponse(**snapshot)


@router.get("/result")
def get_result(session_id: str):
    session = _ensure_session(session_id)
    result_path = session.get("result_path")
    if not result_path or not os.path.isfile(result_path):
        raise HTTPException(status_code=404, detail="Result not ready")
    return FileResponse(result_path, media_type="video/mp4", filename=Path(result_path).name)

async def _enqueue_render(payload: RenderRequest) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    RENDER_JOBS[job_id] = {"status": "queued", "photos": payload.photos, "payload": payload.model_dump()}
    asyncio.create_task(_run_render(job_id, payload))
    return {"job_id": job_id, "status": "queued", "status_url": f"/v1/render/status/{job_id}"}

def _scene_price(scene_key: str) -> int:
    meta = assets.SCENES.get(scene_key) or {}
    return int(meta.get("price_rub", 0) or 0)

def _payment_key_from_payload(payload: RenderRequest) -> str:
    base = payload.model_dump()
    base["photos"] = payload.photos
    raw = json.dumps(base, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Memory Forever API",
        version="0.1.0",
        description="HTTP API surface for Memory Forever web integration.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_origin_regex=_ALLOWED_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def root() -> PlainTextResponse:
        return PlainTextResponse(
            "Memory Forever API is up. See /v1/catalog",
            media_type="text/plain; charset=utf-8",
        )

    @app.head("/", include_in_schema=False)
    async def head_root():
        return PlainTextResponse("", status_code=200)

    app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
    app.mount("/renders", StaticFiles(directory=str(RENDERS_DIR)), name="renders")
    app.mount("/assets", StaticFiles(directory=str(ROOT / "assets")), name="assets")

    return app
