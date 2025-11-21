from __future__ import annotations

import asyncio
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from moviepy import ImageClip, AudioFileClip, concatenate_videoclips, CompositeVideoClip, ColorClip
import moviepy.audio.fx.all as afx

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
    ensure_directories,
)
from ..render.pipeline import (
    ensure_jpeg_copy,
    ensure_runway_datauri_under_limit,
    runway_start,
    runway_poll,
    download,
    apply_fullscreen_watermark,
    postprocess_concat_ffmpeg,
)

ensure_directories()
Path("renders/temp").mkdir(parents=True, exist_ok=True)

router = APIRouter(prefix="/v1")

# FS paths
ROOT = Path(__file__).resolve().parents[2]
UPLOADS_DIR = ROOT / "uploads"
RENDERS_DIR = ROOT / "renders"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
RENDERS_DIR.mkdir(parents=True, exist_ok=True)

# in-memory store for render jobs
RENDER_JOBS: Dict[str, Dict[str, Any]] = {}

_ALLOWED_ORIGINS = [
    "https://memoryforever.ru",
    "https://www.memoryforever.ru",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

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


@router.post("/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    saved: List[str] = []
    for upload in files:
        ext = Path(upload.filename or "").suffix.lower()
        name = f"{uuid.uuid4().hex}{ext}"
        dest = UPLOADS_DIR / name
        with dest.open("wb") as out:
            shutil.copyfileobj(upload.file, out)
        saved.append(f"/uploads/{name}")
    return {"files": saved}


@router.post("/render/start")
async def render_start(payload: RenderRequest):
    job_id = uuid.uuid4().hex
    RENDER_JOBS[job_id] = {"status": "queued"}
    asyncio.create_task(_run_render(job_id, payload))
    return {"job_id": job_id, "status": "queued", "status_url": f"/v1/render/status/{job_id}"}


@router.get("/render/status/{job_id}")
async def render_status(job_id: str):
    data = RENDER_JOBS.get(job_id)
    if not data:
        return JSONResponse({"error": "not found"}, status_code=404)
    return data


async def _run_render(job_id: str, payload: RenderRequest) -> None:
    """
    Минимальный рендер:
    - слайд-шоу из payload.photos (5с на фото), вписываем в 1920x1080 на чёрном фоне
    - музыка по payload.music_key, если нашлась в CATALOG
    - сохраняем renders/<job_id>.mp4 и обновляем RENDER_JOBS
    """
    try:
        job = RENDER_JOBS.get(job_id, {"status": "queued"})
        job.update({"status": "processing", "progress": 10})
        RENDER_JOBS[job_id] = job

        abs_photos: list[str] = []
        for p in payload.photos:
            if p.startswith("/uploads/"):
                rel = p.split("/uploads/")[1]
                abs_photos.append(str((UPLOADS_DIR / rel).resolve()))
            else:
                abs_photos.append(str((ROOT / p).resolve()))

        for ph in abs_photos:
            if not Path(ph).exists():
                raise FileNotFoundError(f"photo not found: {ph}")

        clips = []
        for ph in abs_photos:
            # MoviePy 2.x: resized / with_duration / with_position
            c = ImageClip(ph).resized(height=1080).with_duration(5)
            if c.w != 1920 or c.h != 1080:
                bg = ColorClip(size=(1920, 1080), color=(0, 0, 0), duration=c.duration)
                c = CompositeVideoClip([bg, c.with_position("center")])
            clips.append(c)

        video = concatenate_videoclips(clips, method="compose") if len(clips) > 1 else clips[0]
        job.update({"progress": 60})
        RENDER_JOBS[job_id] = job

        music_path = None
        for m in assets.CATALOG.get("music", []):
            if m.get("key") == payload.music_key:
                music_path = str((ROOT / m["path"]).resolve())
                break

        if music_path and Path(music_path).exists():
            audio = AudioFileClip(music_path)
            target_duration = video.duration
            audio = audio.with_effects(
                [
                    afx.AudioLoop(duration=target_duration),
                    afx.AudioFadeOut(min(1.0, target_duration)),
                ]
            )
            video = video.with_audio(audio)

        out_path = RENDERS_DIR / f"{job_id}.mp4"
        job.update({"progress": 90})
        RENDER_JOBS[job_id] = job

        video.write_videofile(
            str(out_path),
            codec="libx264",
            audio_codec="aac",
            fps=30,
            preset="ultrafast",
            threads=2,
            verbose=False,
            logger=None,
        )
        video.close()

        job.update(
            {
                "status": "done",
                "progress": 100,
                "result": {"video_url": f"/renders/{job_id}.mp4"},
            }
        )
        RENDER_JOBS[job_id] = job

    except Exception as e:  # noqa: BLE001
        RENDER_JOBS[job_id] = {"status": "error", "error": str(e)}


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

    from ..render.pipeline import make_start_frame as _make_start_frame

    start_path, metrics = _make_start_frame(job["photos"], st["format"], bg_path, layout=None)
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


def create_app() -> FastAPI:
    app = FastAPI(
        title="Memory Forever API",
        version="0.1.0",
        description="HTTP API surface for Memory Forever web integration.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_origin_regex=r"^https://.*\.creatium\.app$",
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

    return app
