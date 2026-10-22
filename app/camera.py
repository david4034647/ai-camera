"""V4L2 camera capture with a fan-out to subscribers.

The capture thread owns the device.  Frames handed to subscribers are
immutable by convention (never draw on them in place) which lets us avoid
copying 720p buffers several times per frame.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

import cv2
import numpy as np

log = logging.getLogger("aicam.camera")

FrameCallback = Callable[[np.ndarray], None]


class CameraManager:
    def __init__(
        self,
        index: int = 0,
        width: int = 1280,
        height: int = 720,
        fps: int = 15,
        fourcc: str = "MJPG",
    ) -> None:
        self.index = index
        self.req_width = width
        self.req_height = height
        self.req_fps = fps
        self.fourcc = fourcc

        self._lock = threading.RLock()
        self._frame_lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._frame_ts: float = 0.0
        self._seq: int = 0

        self._subs: list[FrameCallback] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cap: Optional[cv2.VideoCapture] = None

        self._fps_times: deque[float] = deque(maxlen=40)
        self._frames_total = 0
        self.status = "off"          # off | connecting | running | reconnecting | error
        self.last_error: str = ""
        self.started_at: float = 0.0
        self.actual_width = 0
        self.actual_height = 0

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self.status = "connecting"
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 4.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout)
        self._thread = None
        self._release()
        with self._frame_lock:
            self._frame = None
            self._frame_ts = 0.0
        self.status = "off"

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self.status == "running")

    # -- subscription ---------------------------------------------------
    def subscribe(self, cb: FrameCallback) -> None:
        with self._lock:
            if cb not in self._subs:
                self._subs.append(cb)

    def unsubscribe(self, cb: FrameCallback) -> None:
        with self._lock:
            if cb in self._subs:
                self._subs.remove(cb)

    # -- frames ---------------------------------------------------------
    def get_frame(self) -> tuple[Optional[np.ndarray], float, int]:
        with self._frame_lock:
            return self._frame, self._frame_ts, self._seq

    @property
    def fps(self) -> float:
        if len(self._fps_times) < 2:
            return 0.0
        span = self._fps_times[-1] - self._fps_times[0]
        if span <= 0:
            return 0.0
        return (len(self._fps_times) - 1) / span

    @property
    def frames_total(self) -> int:
        return self._frames_total

    # -- internals ------------------------------------------------------
    def _open(self) -> Optional[cv2.VideoCapture]:
        try:
            cap = cv2.VideoCapture(self.index, cv2.CAP_V4L2)
            if not cap.isOpened():
                self.last_error = f"cannot open /dev/video{self.index}"
                cap.release()
                return None
            if self.fourcc:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.req_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.req_height)
            cap.set(cv2.CAP_PROP_FPS, self.req_fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            ok, frame = cap.read()
            if not ok or frame is None:
                self.last_error = "camera opened but returned no frame"
                cap.release()
                return None
            self.actual_height, self.actual_width = frame.shape[:2]
            log.info(
                "camera %d opened: %dx%d @ %.1ffps (fourcc %s)",
                self.index, self.actual_width, self.actual_height,
                cap.get(cv2.CAP_PROP_FPS), self.fourcc,
            )
            self.last_error = ""
            return cap
        except Exception as exc:  # pragma: no cover - hardware dependent
            self.last_error = str(exc)
            log.exception("camera open failed")
            return None

    def _release(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            cap = self._open()
            if cap is None:
                self.status = "reconnecting"
                if self._stop.wait(2.0):
                    break
                continue

            self._cap = cap
            self.status = "running"
            failures = 0
            while not self._stop.is_set():
                try:
                    ok, frame = cap.read()
                except Exception:
                    ok, frame = False, None
                if not ok or frame is None:
                    failures += 1
                    if failures >= 15:
                        self.last_error = "camera stopped delivering frames"
                        break
                    time.sleep(0.05)
                    continue
                failures = 0
                self._dispatch(frame)

            if self._cap is cap:
                self._cap = None
            try:
                cap.release()
            except Exception:
                pass
            if not self._stop.is_set():
                self.status = "reconnecting"
                self._stop.wait(1.5)

    def _dispatch(self, frame: np.ndarray) -> None:
        now = time.time()
        with self._frame_lock:
            self._frame = frame
            self._frame_ts = now
            self._seq += 1
        self._frames_total += 1
        self._fps_times.append(now)

        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(frame)
            except Exception:  # never let a subscriber kill capture
                log.exception("frame subscriber failed")
