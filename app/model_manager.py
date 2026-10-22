"""Fetch the small offline models on first run (one-time, ~23 MB)."""
from __future__ import annotations

import logging
import threading
import urllib.request
from pathlib import Path

log = logging.getLogger("aicam.models")

BASE = "https://raw.githubusercontent.com"
FILES = {
    "MobileNetSSD_deploy.prototxt": (
        f"{BASE}/chuanqi305/MobileNet-SSD/master/deploy.prototxt", 40_000
    ),
    "mobilenet_iter_73000.caffemodel": (
        f"{BASE}/chuanqi305/MobileNet-SSD/master/mobilenet_iter_73000.caffemodel", 20_000_000
    ),
    "face_detection_yunet_2023mar.onnx": (
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx", 200_000
    ),
}

_status: dict[str, str] = {name: "unknown" for name in FILES}


def status() -> dict[str, str]:
    return dict(_status)


def ensure_models(models_dir: Path, timeout: float = 120) -> list[str]:
    """Download missing model files. Returns the list of files fetched."""
    models_dir.mkdir(parents=True, exist_ok=True)
    fetched: list[str] = []
    for name, (url, min_size) in FILES.items():
        dst = models_dir / name
        if dst.exists() and dst.stat().st_size >= min_size:
            _status[name] = "ok"
            continue
        try:
            log.info("downloading %s", name)
            tmp = dst.with_suffix(dst.suffix + ".part")
            req = urllib.request.Request(url, headers={"User-Agent": "ai-camera/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
            if tmp.stat().st_size < min_size:
                tmp.unlink(missing_ok=True)
                raise IOError(f"downloaded file too small ({name})")
            tmp.replace(dst)
            _status[name] = "ok"
            fetched.append(name)
        except Exception as exc:
            _status[name] = f"failed: {exc}"
            log.warning("could not download %s: %s", name, exc)
    return fetched


def ensure_models_async(models_dir: Path) -> threading.Thread:
    t = threading.Thread(target=ensure_models, args=(models_dir,), name="model-download", daemon=True)
    t.start()
    return t
