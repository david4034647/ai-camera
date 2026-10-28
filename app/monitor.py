"""Monitoring service: capture fan-out, detection loop, events, retention."""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .camera import CameraManager
from .config import Settings, ensure_dirs
from .detector import Detection, FaceDetector, PersonDetector
from .recorder import ClipRecorder, DailyRecorder, merge_day_segments
from .storage import EventStore

log = logging.getLogger("aicam.monitor")

GREEN = (80, 220, 90)
RED = (60, 60, 230)
CYAN = (230, 230, 60)
WHITE = (245, 245, 245)


@dataclass
class Snapshot:
    persons: int = 0
    faces: int = 0
    detections: list[Detection] = field(default_factory=list)
    face_boxes: list[Detection] = field(default_factory=list)
    ts: float = 0.0


class PreviewStreamer:
    """Encodes one annotated JPEG per tick and shares it with all viewers."""

    def __init__(self, camera: CameraManager, service: "MonitoringService", fps: int, quality: int) -> None:
        self.camera = camera
        self.service = service
        self.fps = max(1, fps)
        self.quality = quality
        self._lock = threading.Lock()
        self._jpeg: Optional[bytes] = None
        self._version = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_encode_ms = 0.0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="preview", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        with self._lock:
            self._jpeg = None

    def wait_frame(self, after_version: int = -1, timeout: float = 2.0) -> tuple[Optional[bytes], int]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._jpeg is not None and self._version != after_version:
                    return self._jpeg, self._version
            time.sleep(0.02)
        with self._lock:
            return self._jpeg, self._version

    def _run(self) -> None:
        interval = 1.0 / self.fps
        last_seq = -1
        while not self._stop.is_set():
            t0 = time.time()
            frame, ts, seq = self.camera.get_frame()
            if frame is not None and seq != last_seq:
                last_seq = seq
                img = frame.copy()
                self.service.draw_overlay(img)
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                if ok:
                    with self._lock:
                        self._jpeg = buf.tobytes()
                        self._version += 1
                    self.last_encode_ms = round((time.time() - t0) * 1000, 1)
            dt = time.time() - t0
            self._stop.wait(max(0.005, interval - dt))


class Retention:
    """Deletes recordings/events older than the retention window."""

    def __init__(self, settings: Settings, store: EventStore) -> None:
        self.s = settings
        self.store = store
        self.cutoff: date = self.compute_cutoff()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_run: float = 0.0

    def compute_cutoff(self) -> date:
        return date.today() - timedelta(days=max(1, self.s.retention_days) - 1)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="retention", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.cleanup()
            except Exception:
                log.exception("retention cleanup failed")
            self._stop.wait(300)

    def cleanup(self) -> dict:
        self.cutoff = self.compute_cutoff()
        removed_files = 0
        freed = 0

        rec_dir = self.s.data_path("recordings")
        for p in sorted(rec_dir.glob("*.mp4")):
            day = _parse_day(p.name)
            if day is not None and day < self.cutoff:
                freed += p.stat().st_size
                p.unlink(missing_ok=True)
                removed_files += 1

        ev_dir = self.s.data_path("events")
        for d in sorted(ev_dir.iterdir()) if ev_dir.exists() else []:
            if not d.is_dir():
                continue
            try:
                day = datetime.strptime(d.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < self.cutoff:
                for f in d.iterdir():
                    freed += f.stat().st_size
                    removed_files += 1
                shutil.rmtree(d, ignore_errors=True)

        purged = self.store.purge_before(self.cutoff)
        freed += self._emergency_cleanup()

        self.last_run = time.time()
        if removed_files or purged:
            log.info("retention: removed %d files (%d MB), %d event rows",
                     removed_files, freed // (1024 * 1024), purged)
        return {"removed_files": removed_files, "freed_bytes": freed,
                "purged_events": purged, "cutoff": self.cutoff.isoformat()}

    def _emergency_cleanup(self) -> int:
        """Delete the oldest day if the disk is nearly full."""
        free_gb = shutil.disk_usage(self.s.data_path("recordings")).free / 1024 ** 3
        if free_gb > getattr(self.s, "min_free_gb", 3):
            return 0
        freed = 0
        rec_dir = self.s.data_path("recordings")
        days = sorted({_parse_day(p.name) for p in rec_dir.glob("*.mp4")} - {None})
        for day in days:
            if day >= date.today():
                continue
            for p in rec_dir.glob(f"{day.isoformat()}*.mp4"):
                freed += p.stat().st_size
                p.unlink(missing_ok=True)
            shutil.rmtree(self.s.data_path("events", day.isoformat()), ignore_errors=True)
            log.warning("low disk space: dropped %s", day)
            if shutil.disk_usage(rec_dir).free / 1024 ** 3 > getattr(self.s, "min_free_gb", 3):
                break
        return freed


class MonitoringService:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        ensure_dirs(settings)
        # 启动时（录制尚未开始）把历史分段合并回每天一个文件，今天的也一并合并，
        # 这样"每天一个视频"在多次重启后依然成立。
        merge_day_segments(settings.data_path("recordings"))

        self.store = EventStore(settings.data_path("events.db"))
        self.camera = CameraManager(
            index=settings.camera_index,
            width=settings.capture_width,
            height=settings.capture_height,
            fps=settings.capture_fps,
            fourcc=settings.fourcc,
        )
        self.detector = PersonDetector(
            Path(__file__).resolve().parent.parent / "models",
            conf=settings.person_conf,
            size=settings.detect_size,
        )
        self.faces = FaceDetector(
            Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx",
            conf=settings.face_conf,
        )
        self.preview = PreviewStreamer(self.camera, self, settings.stream_fps, settings.stream_quality)
        self.retention = Retention(settings, self.store)

        self._lock = threading.RLock()
        self._running = False
        self.started_at: float = 0.0
        self.recorder: Optional[DailyRecorder] = None
        self._clip: Optional[ClipRecorder] = None
        self._detect_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self.snapshot = Snapshot()
        self._active_event: Optional[int] = None
        self._event_started: float = 0.0
        self._event_last_seen: float = 0.0
        self._event_max_persons = 0
        self._event_max_faces = 0
        self._event_best_score = 0.0
        self.events_today_cache = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> dict:
        with self._lock:
            if self._running:
                return self.status()
            self._stop.clear()
            self.camera.subscribe(self._on_frame)
            self.camera.start()
            self.recorder = DailyRecorder(
                self.s.data_path("recordings"),
                self.s.record_width or self.s.capture_width,
                self.s.record_height or self.s.capture_height,
                self.s.record_fps,
                crf=self.s.record_crf,
                maxrate=self.s.record_maxrate,
                max_file_mb=self.s.record_max_file_mb,
            )
            self.recorder.start()
            self.preview.start()
            self.retention.start()
            self._detect_thread = threading.Thread(target=self._detect_loop, name="detect", daemon=True)
            self._detect_thread.start()
            self._running = True
            self.started_at = time.time()
            log.info("monitoring started")
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            if not self._running:
                return self.status()
            self._running = False
            self._stop.set()
        if self._detect_thread:
            self._detect_thread.join(timeout=5)
            self._detect_thread = None
        self._close_event(reason="stop")
        self.preview.stop()
        self.retention.stop()
        if self.recorder:
            self.recorder.stop()
            self.recorder = None
        self.camera.unsubscribe(self._on_frame)
        self.camera.stop()
        self.snapshot = Snapshot()
        self.started_at = 0.0
        log.info("monitoring stopped")
        return self.status()

    def toggle(self, on: bool) -> dict:
        return self.start() if on else self.stop()

    @property
    def running(self) -> bool:
        return self._running

    # -- capture fan-out --------------------------------------------------
    def _on_frame(self, frame: np.ndarray) -> None:
        rec = self.recorder
        if rec is not None:
            rec.submit(frame)
        clip = self._clip
        if clip is not None:
            clip.submit(frame)

    # -- detection loop ---------------------------------------------------
    def _detect_loop(self) -> None:
        interval = 1.0 / max(0.5, self.s.detect_fps)
        last_seq = -1
        cycle = 0
        while not self._stop.is_set():
            t0 = time.time()
            frame, ts, seq = self.camera.get_frame()
            if frame is not None and seq != last_seq:
                last_seq = seq
                cycle += 1
                try:
                    persons = self.detector.detect(frame)
                    # face detection is the slower model: run it only when a
                    # person is in frame, and at half rate when the CPU is busy
                    if self.s.face_enabled and persons:
                        slow = self.detector.latency_ms > interval * 1000 * 0.6
                        if not (slow and cycle % 2):
                            faces = self.faces.detect(frame)
                        else:
                            faces = self.snapshot.face_boxes
                    elif self.s.face_enabled:
                        faces = []
                    else:
                        faces = []
                except Exception:
                    log.exception("detection cycle failed")
                    persons, faces = [], []
                if persons:
                    self.snapshot = Snapshot(len(persons), len(faces), persons, faces, time.time())
                else:
                    self.snapshot = Snapshot(0, 0, [], [], time.time())
                self._tick_event(persons, faces, frame)
            elapsed = time.time() - t0
            self._stop.wait(max(0.01, interval - elapsed))

    def _tick_event(self, persons: list[Detection], faces: list[Detection], frame: np.ndarray) -> None:
        now = time.time()
        with self._lock:
            if persons:
                self._event_last_seen = now
                if self._active_event is None:
                    self._start_event(persons, faces, frame, now)
                else:
                    self._event_max_persons = max(self._event_max_persons, len(persons))
                    self._event_max_faces = max(self._event_max_faces, len(faces))
                    self._event_best_score = max(
                        self._event_best_score, max(p.score for p in persons)
                    )
            elif self._active_event is not None:
                if now - self._event_last_seen >= self.s.event_gap_seconds:
                    self._close_event(reason="gone")

    def _start_event(self, persons: list[Detection], faces: list[Detection],
                     frame: np.ndarray, now: float) -> None:
        stamp = datetime.fromtimestamp(now)
        day = stamp.date().isoformat()
        base = stamp.strftime("%H%M%S")
        ev_dir = self.s.data_path("events", day)
        ev_dir.mkdir(parents=True, exist_ok=True)

        annotated = frame.copy()
        for d in persons:
            draw_box(annotated, d, GREEN, 0.55)
        for d in faces:
            draw_box(annotated, d, CYAN, 0.5, label_prefix="face ")
        path = ev_dir / f"snap_{base}.jpg"
        cv2.imwrite(str(path), annotated, [cv2.IMWRITE_JPEG_QUALITY, 88])

        rel_snap = str(path.relative_to(self.s.data_path()))
        bbox = ", ".join(f"{int(d.x1)},{int(d.y1)},{int(d.x2)},{int(d.y2)}" for d in persons[:6])
        event_id = self.store.add(
            started_at=stamp,
            persons=len(persons),
            faces=len(faces),
            score=max((p.score for p in persons), default=0.0),
            snapshot=rel_snap,
            bbox=bbox,
        )
        self._active_event = event_id
        self._event_started = now
        self._event_last_seen = now
        self._event_max_persons = len(persons)
        self._event_max_faces = len(faces)
        self._event_best_score = max((p.score for p in persons), default=0.0)
        log.info("event #%d started: %d person(s), %d face(s)", event_id, len(persons), len(faces))

        if self.s.event_clip_seconds > 0:
            clip_path = ev_dir / f"clip_{base}.mp4"
            clip = ClipRecorder(
                clip_path,
                self.s.record_width or self.s.capture_width,
                self.s.record_height or self.s.capture_height,
                self.s.record_fps,
                duration=float(self.s.event_clip_seconds),
                on_done=lambda p, dur, eid=event_id: self._on_clip_done(eid, p, dur),
            )
            self._clip = clip
            clip.start()
            self.store.set_clip(event_id, str(clip_path.relative_to(self.s.data_path())))

    def _on_clip_done(self, event_id: int, path: Path, duration: float) -> None:
        try:
            rel = str(path.relative_to(self.s.data_path()))
        except ValueError:
            rel = str(path)
        self.store.set_clip(event_id, rel, round(duration, 1))
        log.info("event #%d clip saved (%.1fs, %d KB)", event_id, duration,
                 path.stat().st_size // 1024 if path.exists() else 0)

    def _close_event(self, reason: str = "gone") -> None:
        with self._lock:
            eid, self._active_event = self._active_event, None
            started = self._event_started
            self._event_started = 0.0
            if eid is None:
                return
            now = time.time()
            duration = now - started
            self.store.finish(eid, datetime.fromtimestamp(now),
                              self._event_max_persons, self._event_max_faces)
            clip, self._clip = self._clip, None
        if clip is not None:
            # keep a 1.5 s tail so people walking out are still in the clip
            timer = threading.Timer(1.5, clip.stop)
            timer.daemon = True
            timer.start()
        log.info("event #%d closed (%s, %.1fs)", eid, reason, duration)

    # -- drawing -----------------------------------------------------------
    def draw_overlay(self, img: np.ndarray) -> None:
        snap = self.snapshot
        fresh = snap.ts and (time.time() - snap.ts) < 2.0
        h, w = img.shape[:2]
        if fresh:
            for d in snap.detections:
                if (d.x2 - d.x1) < 4:
                    continue
                draw_box(img, d, GREEN, 0.55)
            for d in snap.face_boxes:
                draw_box(img, d, CYAN, 0.5, label_prefix="face ")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _text(img, f"AICAM  {ts}", (12, 28), WHITE, 0.62)
        status = "REC" if self.recorder else "IDLE"
        _text(img, f"{status}  {w}x{h}", (12, 54), RED if status == "REC" else WHITE, 0.55)
        if snap.ts:
            counts = f"persons {snap.persons}   faces {snap.faces}"
            _text(img, counts, (12, 80), CYAN, 0.55)
        if self._active_event is not None:
            _text(img, f"EVENT #{self._active_event}", (12, h - 18), RED, 0.58)

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        rec = self.recorder
        cam = self.camera
        today = date.today().isoformat()
        return {
            "running": self._running,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds")
            if self.started_at else None,
            "uptime_seconds": int(time.time() - self.started_at) if self.started_at else 0,
            "camera": {
                "status": cam.status,
                "index": cam.index,
                "width": cam.actual_width,
                "height": cam.actual_height,
                "fps": round(cam.fps, 1),
                "frames": cam.frames_total,
                "error": cam.last_error,
            },
            "detector": {
                **self.detector.info(),
                "face": self.faces.info(),
                "persons": self.snapshot.persons,
                "faces": self.snapshot.faces,
                "detect_fps": self.s.detect_fps,
            },
            "recording": {
                "file": _rel(self.s, rec.current_file) if rec else None,
                "segment_bytes": rec.today_size() if rec else 0,
                "dropped_frames": rec.dropped if rec else 0,
                "event_active": self._active_event is not None,
                "active_event_id": self._active_event,
            },
            "events_today": self.store.counts(today),
            "storage": self.storage_stats(),
            "preview": {"fps": self.s.stream_fps, "encode_ms": self.preview.last_encode_ms},
        }

    def storage_stats(self) -> dict:
        rec_dir = self.s.data_path("recordings")
        ev_dir = self.s.data_path("events")
        days: dict[str, dict] = {}
        total = 0
        for p in rec_dir.glob("*.mp4"):
            day = _parse_day(p.name)
            if not day:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                # 文件可能刚好被保留期清理删掉，跳过即可
                continue
            total += size
            d = days.setdefault(day.isoformat(), {"date": day.isoformat(), "bytes": 0, "files": 0})
            d["bytes"] += size
            d["files"] += 1
        events = {row["day"]: row["events"] for row in self.store.days()}
        for day, cnt in events.items():
            d = days.setdefault(day, {"date": day, "bytes": 0, "files": 0})
            d["events"] = cnt
        for d in days.values():
            d.setdefault("events", 0)
        usage = shutil.disk_usage(rec_dir)
        return {
            "retention_days": self.s.retention_days,
            "used_bytes": total,
            "disk_free_bytes": usage.free,
            "disk_total_bytes": usage.total,
            "days": sorted(days.values(), key=lambda x: x["date"], reverse=True),
            "cutoff": self.retention.cutoff.isoformat(),
            "last_cleanup": datetime.fromtimestamp(self.retention.last_run).isoformat(timespec="seconds")
            if self.retention.last_run else None,
        }

    def event_path(self, rel: str) -> Optional[Path]:
        if not rel:
            return None
        p = (self.s.data_path() / rel).resolve()
        root = self.s.data_path().resolve()
        if root not in p.parents:
            return None
        return p


def _rel(settings: Settings, path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return str(path.relative_to(settings.data_path()))
    except ValueError:
        return str(path)


def _parse_day(name: str) -> Optional[date]:
    try:
        return datetime.strptime(name.split("_part")[0].removesuffix(".mp4"), "%Y-%m-%d").date()
    except ValueError:
        return None


def draw_box(img: np.ndarray, d: Detection, color: tuple[int, int, int],
             alpha: float = 0.55, label_prefix: str = "") -> None:
    x1, y1, x2, y2 = int(d.x1), int(d.y1), int(d.x2), int(d.y2)
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
    label = f"{label_prefix}{d.score:.2f}"
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    top = max(0, y1 - th - 8)
    cv2.rectangle(img, (x1, top), (x1 + tw + 8, y1), color, -1)
    cv2.putText(img, label, (x1 + 4, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (20, 20, 20), 1, cv2.LINE_AA)


def _text(img: np.ndarray, text: str, org: tuple[int, int],
          color: tuple[int, int, int], scale: float) -> None:
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)
