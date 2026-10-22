"""FastAPI application: REST API, MJPEG live view, web UI."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, Settings, ensure_dirs, load
from .model_manager import ensure_models_async
from .monitor import MonitoringService

log = logging.getLogger("aicam")

settings: Settings = load()
ensure_dirs(settings)


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logfile = settings.data_path("logs", "aicam.log")
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fh = RotatingFileHandler(logfile, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


_setup_logging()
service = MonitoringService(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("AI Camera starting (data=%s)", settings.data_path())
    ensure_models_async(BASE_DIR / "models")
    if settings.autostart:
        try:
            service.start()
        except Exception:
            log.exception("autostart failed")
    yield
    log.info("AI Camera shutting down")
    service.stop()


app = FastAPI(title="AI Camera", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
def _authorized(request: Request, token: Optional[str] = None) -> bool:
    expected = settings.auth_token
    if not expected:
        return True
    given = token or request.query_params.get("token") or ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    return given == expected


def _guard(request: Request, token: Optional[str] = None) -> None:
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="unauthorized")


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/favicon.svg")
async def favicon() -> Response:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#0b1220"/>'
        '<circle cx="32" cy="32" r="16" fill="none" stroke="#22d3ee" stroke-width="4"/>'
        '<circle cx="32" cy="32" r="6" fill="#22c55e"/></svg>'
    )
    return Response(svg, media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# control
# ---------------------------------------------------------------------------
@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(service.status())


@app.post("/api/monitor/start")
async def api_start(request: Request) -> JSONResponse:
    _guard(request)
    return JSONResponse(service.start())


@app.post("/api/monitor/stop")
async def api_stop(request: Request) -> JSONResponse:
    _guard(request)
    return JSONResponse(service.stop())


@app.post("/api/monitor/toggle")
async def api_toggle(request: Request) -> JSONResponse:
    _guard(request)
    return JSONResponse(service.toggle(not service.running))


@app.get("/api/config")
async def api_config() -> JSONResponse:
    return JSONResponse({
        "retention_days": settings.retention_days,
        "detect_fps": settings.detect_fps,
        "person_conf": settings.person_conf,
        "face_enabled": settings.face_enabled,
        "record_width": settings.record_width,
        "record_height": settings.record_height,
        "record_fps": settings.record_fps,
        "event_clip_seconds": settings.event_clip_seconds,
        "camera_index": settings.camera_index,
        "auth_required": bool(settings.auth_token),
    })


# ---------------------------------------------------------------------------
# live view
# ---------------------------------------------------------------------------
@app.get("/stream.mjpg")
async def stream(request: Request, token: Optional[str] = None) -> StreamingResponse:
    _guard(request, token)
    if not service.running:
        raise HTTPException(status_code=409, detail="monitoring is off")

    async def gen():
        boundary = b"--aicamframe\r\n"
        last = -1
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            jpeg, ver = await asyncio.to_thread(service.preview.wait_frame, last, 1.0)
            if jpeg is None:
                idle += 1
                if idle > 30:
                    break
                await asyncio.sleep(0.1)
                continue
            if ver == last:
                continue
            idle = 0
            last = ver
            yield boundary + b"Content-Type: image/jpeg\r\nContent-Length: " \
                + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=aicamframe",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                 "Pragma": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/snapshot.jpg")
async def api_snapshot(request: Request) -> Response:
    _guard(request)
    import cv2

    frame, ts, _ = service.camera.get_frame()
    if frame is None:
        raise HTTPException(status_code=503, detail="no frame available")
    img = frame.copy()
    service.draw_overlay(img)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise HTTPException(status_code=500, detail="encode failed")
    return Response(buf.tobytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------
@app.get("/api/events")
async def api_events(day: Optional[str] = None, limit: int = Query(200, ge=1, le=1000)) -> JSONResponse:
    rows = service.store.list(day=day, limit=limit)
    for r in rows:
        r["has_snapshot"] = bool(r.get("snapshot") and service.event_path(r["snapshot"]) and
                                 service.event_path(r["snapshot"]).exists())
        r["has_clip"] = bool(r.get("clip") and service.event_path(r["clip"]) and
                             service.event_path(r["clip"]).exists())
    return JSONResponse({"events": rows, "days": service.store.days(), "count": len(rows)})


@app.get("/api/events/{event_id}")
async def api_event(event_id: int) -> JSONResponse:
    ev = service.store.get(event_id)
    if not ev:
        raise HTTPException(status_code=404, detail="event not found")
    return JSONResponse(ev)


@app.get("/api/events/{event_id}/snapshot")
async def api_event_snapshot(event_id: int) -> FileResponse:
    ev = service.store.get(event_id)
    if not ev or not ev.get("snapshot"):
        raise HTTPException(status_code=404, detail="snapshot not found")
    p = service.event_path(ev["snapshot"])
    if not p or not p.exists():
        raise HTTPException(status_code=404, detail="snapshot missing on disk")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/events/{event_id}/clip")
async def api_event_clip(event_id: int) -> FileResponse:
    ev = service.store.get(event_id)
    if not ev or not ev.get("clip"):
        raise HTTPException(status_code=404, detail="clip not found")
    p = service.event_path(ev["clip"])
    if not p or not p.exists():
        raise HTTPException(status_code=404, detail="clip missing on disk")
    return FileResponse(p, media_type="video/mp4")


@app.delete("/api/events/{event_id}")
async def api_event_delete(event_id: int, request: Request) -> JSONResponse:
    _guard(request)
    ev = service.store.delete(event_id)
    if not ev:
        raise HTTPException(status_code=404, detail="event not found")
    for key in ("snapshot", "clip"):
        p = service.event_path(ev.get(key) or "")
        if p and p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    return JSONResponse({"deleted": event_id})


# ---------------------------------------------------------------------------
# recordings
# ---------------------------------------------------------------------------
@app.get("/api/recordings")
async def api_recordings() -> JSONResponse:
    stats = service.storage_stats()
    rec_dir = settings.data_path("recordings")
    files = []
    for p in sorted(rec_dir.glob("*.mp4"), reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append({
            "name": p.name,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "day": p.name.split("_part")[0].removesuffix(".mp4"),
        })
    return JSONResponse({"files": files, "storage": stats})


@app.get("/api/recordings/{name}")
async def api_recording(name: str) -> FileResponse:
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="bad file name")
    p = settings.data_path("recordings", name)
    if not p.exists():
        raise HTTPException(status_code=404, detail="recording not found")
    return FileResponse(p, media_type="video/mp4", filename=name)


@app.get("/api/health")
async def api_health() -> JSONResponse:
    return JSONResponse({"ok": True, "running": service.running,
                         "camera": service.camera.status,
                         "time": datetime.now().isoformat(timespec="seconds")})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port,
                log_level="info", access_log=False)
