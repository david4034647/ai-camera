"""Continuous recording to disk.

Design notes
------------
The writer runs on its own fixed-rate clock (``record_fps``) and always picks
the most recent camera frame.  That keeps the file timeline locked to wall
clock even when the camera delivers frames irregularly – a frame-arrival based
writer would make the video play faster than reality whenever capture fps
drops below the recording fps.

Fragmented MP4 (``-movflags +frag_keyframe+empty_moov``) is used so a file is
still playable after a crash or power loss, which matters for a 24/7 monitor.

Two writers share the plumbing:

* ``DailyRecorder`` – one MP4 per calendar day, rotated at midnight.
* ``ClipRecorder``  – a short MP4 capturing a detection event.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("aicam.recorder")

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


class FfmpegSegment:
    """A single rawvideo -> H.264 MP4 process fed through stdin."""

    def __init__(
        self,
        path: Path,
        width: int,
        height: int,
        fps: int,
        crf: int = 30,
        maxrate: str = "900k",
        preset: str = "veryfast",
    ) -> None:
        self.path = path
        self.width = width
        self.height = height
        self.fps = max(1, int(fps))
        self.crf = crf
        self.maxrate = maxrate
        self.preset = preset
        self.proc: Optional[subprocess.Popen] = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._drain: Optional[threading.Thread] = None
        self.bytes_written = 0
        self.started_at = 0.0
        self._broken = False

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self.width}x{self.height}", "-r", str(self.fps),
            "-i", "pipe:0",
            "-an",
            "-c:v", "libx264", "-preset", self.preset, "-tune", "zerolatency",
            "-crf", str(self.crf),
            "-maxrate", self.maxrate, "-bufsize", _double_rate(self.maxrate),
            "-pix_fmt", "yuv420p", "-g", str(self.fps * 2), "-sc_threshold", "0",
            "-flush_packets", "1",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "-y", str(self.path),
        ]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.started_at = time.time()
        self._drain = threading.Thread(target=self._drain_stderr, name="ffmpeg-stderr", daemon=True)
        self._drain.start()
        log.info("recording -> %s (%dx%d @ %dfps)", self.path, self.width, self.height, self.fps)

    def _drain_stderr(self) -> None:
        proc = self.proc
        if not proc or not proc.stderr:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr.append(line)
        except Exception:
            pass

    def write(self, frame: np.ndarray) -> bool:
        proc = self.proc
        if proc is None or proc.poll() is not None or self._broken:
            return False
        h, w = frame.shape[:2]
        if (w, h) != (self.width, self.height):
            frame = _resize(frame, self.width, self.height)
        try:
            proc.stdin.write(memoryview(frame).cast("B"))
            self.bytes_written += frame.nbytes
            return True
        except (BrokenPipeError, ValueError, OSError):
            self._broken = True
            return False

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None and not self._broken)

    def kill(self) -> None:
        """Force-kill the encoder so a blocked stdin write fails fast."""
        self._broken = True
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if proc.returncode not in (0, None) and self._stderr:
            log.warning("ffmpeg exit %s for %s: %s", proc.returncode, self.path.name,
                        " | ".join(list(self._stderr)[-3:]))
        if not self.path.exists() or self.path.stat().st_size == 0:
            log.warning("empty recording file %s", self.path)


def _double_rate(rate: str) -> str:
    try:
        if rate.endswith("k"):
            return f"{int(float(rate[:-1]) * 2)}k"
        if rate.endswith("m"):
            return f"{int(float(rate[:-1]) * 2)}m"
        return str(int(rate) * 2)
    except Exception:
        return "1800k"


def _resize(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    import cv2

    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)


class _FrameSlot:
    """Latest-frame holder shared between the capture thread and a writer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._ts = 0.0
        self.count = 0

    def put(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
            self._ts = time.time()
            self.count += 1

    def get(self) -> tuple[Optional[np.ndarray], float]:
        with self._lock:
            return self._frame, self._ts


class DailyRecorder:
    """Writes one MP4 per day at a fixed frame rate."""

    def __init__(
        self,
        root: Path,
        width: int,
        height: int,
        fps: int,
        crf: int = 30,
        maxrate: str = "900k",
        max_file_mb: int = 4096,
    ) -> None:
        self.root = root
        self.width, self.height, self.fps = width, height, max(1, int(fps) if fps else 12)
        self.crf, self.maxrate, self.max_file_mb = crf, maxrate, max_file_mb

        self._slot = _FrameSlot()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._seg: Optional[FfmpegSegment] = None
        self._seg_date: Optional[date] = None
        self._seg_index = 0
        self._lock = threading.Lock()

        self.frames_written = 0
        self.skipped = 0
        self.error: str = ""

    # -- public ---------------------------------------------------------
    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="daily-recorder", daemon=True)
        self._thread.start()
        self._watchdog = threading.Thread(target=self._watch, name="recorder-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)
        self._thread = None
        self._close_segment()

    def _watch(self) -> None:
        """Kill a wedged ffmpeg so the writer thread unblocks and restarts."""
        last_seen = -1
        last_change = time.time()
        while not self._stop.wait(5):
            written = self.frames_written
            if written != last_seen:
                last_seen = written
                last_change = time.time()
                continue
            frame, ts = self._slot.get()
            stale = (time.time() - ts) > 2.0
            if not stale and time.time() - last_change > 20 and self._seg is not None:
                log.error("recorder stalled, restarting encoder")
                self._seg.kill()
                self._close_segment()
                last_change = time.time()

    def submit(self, frame: np.ndarray) -> None:
        """Called from the capture thread; never blocks."""
        self._slot.put(frame)

    @property
    def current_file(self) -> Optional[Path]:
        return self._seg.path if self._seg else None

    def today_size(self) -> int:
        seg = self._seg
        if seg is None or not seg.path.exists():
            return 0
        try:
            return seg.path.stat().st_size
        except OSError:
            return 0

    @property
    def dropped(self) -> int:  # kept for API compatibility with the status page
        return self.skipped

    # -- internals -------------------------------------------------------
    def _run(self) -> None:
        interval = 1.0 / self.fps
        next_tick = time.time()
        while not self._stop.is_set():
            next_tick += interval
            frame, ts = self._slot.get()
            if frame is None:
                self._stop.wait(0.05)
                continue
            if time.time() - ts > 5.0:
                # camera stalled – do not stretch the timeline with stale frames
                self.skipped += 1
            else:
                try:
                    if self._ensure_segment(frame) and self._seg and self._seg.write(frame):
                        self.frames_written += 1
                    else:
                        self._close_segment()
                except Exception as exc:
                    self.error = str(exc)
                    log.exception("recorder write failed")
                    self._close_segment()
            delay = next_tick - time.time()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_tick = time.time()  # fell behind, resync instead of bursting

    def _ensure_segment(self, frame: np.ndarray) -> bool:
        today = date.today()
        rotate = False
        if self._seg is None:
            rotate = True
        elif self._seg_date != today:
            log.info("midnight rotation")
            rotate = True
        elif not self._seg.alive():
            rotate = True
        elif self.max_file_mb and self._seg.path.exists() and \
                self._seg.path.stat().st_size > self.max_file_mb * 1024 * 1024:
            log.info("size rotation at %dMB", self.max_file_mb)
            rotate = True

        if rotate:
            previous = self._seg_date
            self._close_segment()
            if self._seg_index == 0 or self._seg_date != today:
                self._seg_index = _next_segment_index(self.root, today)
            self._seg_date = today
            self._seg = FfmpegSegment(
                _day_path(self.root, today, self._seg_index),
                self.width, self.height, self.fps, self.crf, self.maxrate,
            )
            self._seg.start()
            self._seg_index += 1
            if previous is not None and previous != today:
                # a new day started: fold yesterday's restart segments together
                threading.Thread(
                    target=merge_day_segments, args=(self.root, today), daemon=True
                ).start()
        return self._seg is not None

    def _close_segment(self) -> None:
        with self._lock:
            seg, self._seg = self._seg, None
        if seg is not None:
            seg.close()


def _day_path(root: Path, day: date, index: int) -> Path:
    if index <= 1:
        return root / f"{day.isoformat()}.mp4"
    return root / f"{day.isoformat()}_part{index}.mp4"


def _next_segment_index(root: Path, day: date) -> int:
    base = root / f"{day.isoformat()}.mp4"
    if not base.exists():
        return 1
    i = 2
    while (root / f"{day.isoformat()}_part{i}.mp4").exists():
        i += 1
    return i


class ClipRecorder:
    """Short fixed-rate clip for one detection event."""

    def __init__(
        self,
        path: Path,
        width: int,
        height: int,
        fps: int,
        duration: float,
        crf: int = 26,
        maxrate: str = "1400k",
        on_done: Optional[Callable[[Path, float], None]] = None,
    ) -> None:
        self.path = path
        self.width, self.height, self.fps = width, height, max(1, int(fps))
        self.duration = duration
        self.crf, self.maxrate = crf, maxrate
        self.on_done = on_done
        self._seg = FfmpegSegment(path, width, height, self.fps, crf, maxrate)
        self._slot = _FrameSlot()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="clip-recorder", daemon=True)
        self._started = 0.0

    def start(self) -> None:
        self._started = time.time()
        self._seg.start()
        self._thread.start()

    def submit(self, frame: np.ndarray) -> None:
        self._slot.put(frame)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        interval = 1.0 / self.fps
        deadline = self._started + self.duration
        next_tick = time.time()
        while not self._stop.is_set() and time.time() < deadline:
            next_tick += interval
            frame, ts = self._slot.get()
            if frame is not None and time.time() - ts < 2.0 and not self._seg.write(frame):
                break
            delay = next_tick - time.time()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_tick = time.time()
        self._seg.close()
        written = time.time() - self._started
        if self.on_done:
            try:
                self.on_done(self.path, written)
            except Exception:
                log.exception("clip on_done callback failed")


def merge_day_segments(root: Path, skip: Optional[date] = None) -> None:
    """Merge ``YYYY-MM-DD_partN.mp4`` segments back into one file per day.

    Called at startup so a restart in the middle of the day still ends up as a
    single daily file once the day is over.
    """
    try:
        groups: dict[str, list[Path]] = {}
        for p in root.glob("*_part*.mp4"):
            stem = p.name.split("_part")[0]
            try:
                day = datetime.strptime(stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if skip and day >= skip:
                continue
            groups.setdefault(stem, []).append(p)
        for stem, parts in groups.items():
            base = root / f"{stem}.mp4"
            parts.sort(key=lambda p: int(p.name.split("_part")[1].split(".")[0]))
            if base.exists() and base.stat().st_size < 1024:
                base.unlink(missing_ok=True)
            inputs = ([base] if base.exists() else []) + parts
            if len(inputs) > 1:
                _concat(inputs, base)
    except Exception:
        log.exception("merge_day_segments failed")


def _concat(inputs: list[Path], out: Path) -> None:
    listfile = out.parent / f".concat_{out.stem}.txt"
    try:
        listfile.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in inputs) + "\n", "utf-8"
        )
        tmp = out.with_suffix(".merge.mp4")
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-c", "copy", "-movflags", "+faststart", str(tmp),
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=600)
        if res.returncode != 0:
            log.warning("concat failed: %s", res.stderr.decode("utf-8", "replace")[-300:])
            tmp.unlink(missing_ok=True)
            return
        tmp.replace(out)
        for p in inputs:
            if p != out:
                p.unlink(missing_ok=True)
        log.info("merged %d segments -> %s", len(inputs), out.name)
    finally:
        listfile.unlink(missing_ok=True)
