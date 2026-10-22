"""Configuration for AI Camera.

Settings live in ``config.json`` at the project root (created on first run
from the defaults below).  Environment variables are not used, so the file
is the single source of truth and can be edited while the service is down.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("AICAM_CONFIG", BASE_DIR / "config.json"))
MODELS_DIR = BASE_DIR / "models"


@dataclass
class Settings:
    # --- server -------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    auth_token: str = ""          # optional shared token; empty = open LAN access

    # --- camera -------------------------------------------------------
    camera_index: int = 0
    capture_width: int = 1280
    capture_height: int = 720
    capture_fps: int = 30        # requested device fps (OpenCV mode selection)
    fourcc: str = "MJPG"

    # --- recording ----------------------------------------------------
    record_width: int = 960        # downscaled for storage; 0 = keep source size
    record_height: int = 540
    record_fps: int = 12
    record_crf: int = 30           # higher = smaller file, lower quality
    record_maxrate: str = "600k"   # hard bitrate ceiling
    record_max_file_mb: int = 4096
    retention_days: int = 3
    min_free_gb: float = 3.0

    # --- detection ----------------------------------------------------
    detect_fps: float = 4.0
    detect_size: int = 320         # inference input (square, letterboxed)
    person_conf: float = 0.45
    face_enabled: bool = True
    face_conf: float = 0.6
    track_iou: float = 0.30
    event_gap_seconds: float = 2.5   # no person for this long -> close event
    event_clip_seconds: int = 10     # 0 disables event clips
    event_min_seconds: float = 0.4

    # --- preview ------------------------------------------------------
    stream_fps: int = 10
    stream_quality: int = 72

    # --- misc ---------------------------------------------------------
    autostart: bool = True
    data_dir: str = "data"

    # ---------------------------------------------------------------
    def data_path(self, *parts: str) -> Path:
        p = Path(self.data_dir)
        if not p.is_absolute():
            p = BASE_DIR / p
        for part in parts:
            p = p / part
        return p


_lock = threading.Lock()


def load() -> Settings:
    """Load settings, filling missing keys with defaults."""
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text("utf-8"))
        except Exception:
            raw = {}
        known = {f.name for f in fields(Settings)}
        data: dict[str, Any] = {k: v for k, v in raw.items() if k in known}
        s = Settings(**data)
    else:
        s = Settings()
        save(s)
    return s


def save(s: Settings) -> None:
    with _lock:
        CONFIG_PATH.write_text(
            json.dumps(asdict(s), indent=2, ensure_ascii=False) + "\n", "utf-8"
        )


def ensure_dirs(s: Settings) -> None:
    for sub in ("", "recordings", "events", "snapshots", "logs"):
        s.data_path(sub).mkdir(parents=True, exist_ok=True)
