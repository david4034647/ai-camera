"""Offline person / face detection on CPU.

Two interchangeable person backends:

* ``ssd``   – MobileNet-SSD (VOC person class) through OpenCV DNN, no extra
              runtime, ~55 ms per frame on a 4-core laptop CPU.
* ``yolo``  – any YOLOv5/v8/v11 ONNX export dropped into ``models/``
              (``yolov8n.onnx`` etc.), better accuracy, still CPU friendly.

Face detection uses YuNet (232 KB ONNX, ~20 ms at 640 px wide) and is optional.
No cloud calls, no identification – only "is there a person / a face here".
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("aicam.detector")

# Vocabulary of the 21-class MobileNet-SSD used here (VOC + background).
_VOC_PERSON_ID = 15
_COCO_PERSON_ID = 0


@dataclass
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    label: str = "person"

    @property
    def box(self) -> tuple[float, float, float, float]:
        return self.x1, self.y1, self.x2, self.y2

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


def _clamp(d: Detection, w: int, h: int) -> Detection:
    d.x1 = float(np.clip(d.x1, 0, w - 1))
    d.y1 = float(np.clip(d.y1, 0, h - 1))
    d.x2 = float(np.clip(d.x2, 0, w - 1))
    d.y2 = float(np.clip(d.y2, 0, h - 1))
    return d


class PersonDetector:
    """Person detector with an auto-selected backend."""

    def __init__(self, models_dir: Path, conf: float = 0.45, size: int = 320) -> None:
        self.models_dir = Path(models_dir)
        self.conf = conf
        self.size = size
        self.backend = "none"
        self.model_name = ""
        self._net = None
        self._onnx = None
        self._onnx_input = 640
        self._onnx_yolov5 = False
        self._lock = threading.Lock()
        self._latency = 0.0
        self._load()

    # -- loading --------------------------------------------------------
    def _load(self) -> None:
        onnx = self._find_yolo()
        if onnx is not None:
            try:
                import onnxruntime as ort

                opts = ort.SessionOptions()
                opts.intra_op_num_threads = 2
                opts.inter_op_num_threads = 1
                opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                self._onnx = ort.InferenceSession(str(onnx), opts, providers=["CPUExecutionProvider"])
                inp = self._onnx.get_inputs()[0]
                self._onnx_input = int(inp.shape[2]) if inp.shape[2] else 640
                self.backend = "yolo"
                self.model_name = onnx.name
                log.info("person detector: YOLO ONNX %s (%dpx)", onnx.name, self._onnx_input)
                return
            except Exception:
                log.exception("failed to load YOLO ONNX, falling back to SSD")

        proto = self.models_dir / "MobileNetSSD_deploy.prototxt"
        weights = self.models_dir / "mobilenet_iter_73000.caffemodel"
        if proto.exists() and weights.exists():
            self._net = cv2.dnn.readNetFromCaffe(str(proto), str(weights))
            self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            self.backend = "ssd"
            self.model_name = "MobileNet-SSD (VOC person)"
            log.info("person detector: MobileNet-SSD")
            return
        log.warning("no person model found in %s", self.models_dir)

    def _find_yolo(self) -> Optional[Path]:
        for name in ("yolov8n.onnx", "yolo11n.onnx", "yolov5n.onnx", "yolov8s.onnx"):
            p = self.models_dir / name
            if p.exists() and p.stat().st_size > 1024:
                return p
        return None

    # -- inference -------------------------------------------------------
    def detect(self, frame: np.ndarray) -> list[Detection]:
        if self._net is None and self._onnx is None:
            return []
        h, w = frame.shape[:2]
        t0 = time.perf_counter()
        try:
            if self.backend == "ssd":
                dets = self._detect_ssd(frame)
            else:
                dets = self._detect_yolo(frame)
        except Exception:
            log.exception("person inference failed")
            dets = []
        dt = (time.perf_counter() - t0) * 1000
        self._latency = dt if self._latency == 0 else self._latency * 0.8 + dt * 0.2
        return [_clamp(d, w, h) for d in dets if d.score >= self.conf]

    def _detect_ssd(self, frame: np.ndarray) -> list[Detection]:
        blob = cv2.dnn.blobFromImage(
            frame, 0.007843, (300, 300), (127.5, 127.5, 127.5), swapRB=False, crop=False
        )
        with self._lock:
            self._net.setInput(blob)
            out = self._net.forward()
        h, w = frame.shape[:2]
        res: list[Detection] = []
        for i in range(out.shape[2]):
            conf = float(out[0, 0, i, 2])
            if conf < self.conf:
                continue
            if int(out[0, 0, i, 1]) != _VOC_PERSON_ID:
                continue
            box = out[0, 0, i, 3:7] * np.array([w, h, w, h])
            x1, y1, x2, y2 = box
            if x2 - x1 < 8 or y2 - y1 < 8:
                continue
            res.append(Detection(float(x1), float(y1), float(x2), float(y2), conf))
        return _nms(res, 0.45)

    def _detect_yolo(self, frame: np.ndarray) -> list[Detection]:
        size = self._onnx_input
        img, ratio, (dw, dh) = _letterbox(frame, size)
        blob = img[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        feeds = {self._onnx.get_inputs()[0].name: np.ascontiguousarray(blob)}
        out = self._onnx.run(None, feeds)[0]
        pred = out[0]
        if pred.shape[0] < pred.shape[1]:      # (84, 8400) -> (8400, 84)
            pred = pred.T
        ch = pred.shape[1]
        obj = None
        if ch == 85:                            # YOLOv5: cx cy w h obj cls...
            obj = pred[:, 4]
            cls_scores = pred[:, 5:]
        elif ch >= 5:
            cls_scores = pred[:, 4:]
        else:
            return []
        cls_ids = np.argmax(cls_scores, axis=1)
        confs = cls_scores[np.arange(len(pred)), cls_ids]
        if obj is not None:
            confs = confs * obj
        keep = (cls_ids == _COCO_PERSON_ID) & (confs >= self.conf)
        if not np.any(keep):
            return []
        p = pred[keep]
        c = confs[keep]
        cx, cy, bw, bh = p[:, 0], p[:, 1], p[:, 2], p[:, 3]
        x1 = (cx - bw / 2 - dw) / ratio
        y1 = (cy - bh / 2 - dh) / ratio
        x2 = (cx + bw / 2 - dw) / ratio
        y2 = (cy + bh / 2 - dh) / ratio
        dets = [Detection(float(a), float(b), float(cc), float(d), float(s))
                for a, b, cc, d, s in zip(x1, y1, x2, y2, c)]
        return _nms(dets, 0.45)

    @property
    def latency_ms(self) -> float:
        return round(self._latency, 1)

    def info(self) -> dict:
        return {"backend": self.backend, "model": self.model_name,
                "latency_ms": self.latency_ms, "conf": self.conf}


class FaceDetector:
    """YuNet face detector (optional, used for head-count + snapshots)."""

    def __init__(self, model_path: Path, conf: float = 0.6) -> None:
        self.conf = conf
        self.model_path = Path(model_path)
        self.available = self.model_path.exists()
        self._det: Optional[cv2.FaceDetectorYN] = None
        self._size: tuple[int, int] = (0, 0)
        self._lock = threading.Lock()
        self._latency = 0.0
        if self.available:
            try:
                self._det = cv2.FaceDetectorYN.create(
                    str(self.model_path), "", (320, 320), conf, 0.3, 5000
                )
                log.info("face detector: YuNet")
            except Exception:
                log.exception("YuNet init failed")
                self.available = False

    def detect(self, frame: np.ndarray) -> list[Detection]:
        if not self.available or self._det is None:
            return []
        h, w = frame.shape[:2]
        scale = 640.0 / w if w > 640 else 1.0
        if scale != 1.0:
            small = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        else:
            small = frame
        sh, sw = small.shape[:2]
        t0 = time.perf_counter()
        try:
            with self._lock:
                if self._size != (sw, sh):
                    self._det.setInputSize((sw, sh))
                    self._size = (sw, sh)
                _, faces = self._det.detect(small)
        except Exception:
            log.exception("face inference failed")
            return []
        dt = (time.perf_counter() - t0) * 1000
        self._latency = dt if self._latency == 0 else self._latency * 0.8 + dt * 0.2
        if faces is None:
            return []
        out: list[Detection] = []
        for f in faces:
            x, y, fw, fh = f[:4]
            score = float(f[-1])
            if score < self.conf:
                continue
            out.append(Detection(
                float(x / scale), float(y / scale),
                float((x + fw) / scale), float((y + fh) / scale),
                score, "face",
            ))
        return out

    @property
    def latency_ms(self) -> float:
        return round(self._latency, 1)

    def info(self) -> dict:
        return {"available": self.available, "latency_ms": self.latency_ms,
                "conf": self.conf, "model": "YuNet" if self.available else "disabled"}


def _letterbox(frame: np.ndarray, size: int) -> tuple[np.ndarray, float, tuple[int, int]]:
    h, w = frame.shape[:2]
    ratio = min(size / w, size / h)
    nw, nh = int(round(w * ratio)), int(round(h * ratio))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    dw, dh = (size - nw) // 2, (size - nh) // 2
    canvas[dh:dh + nh, dw:dw + nw] = resized
    return canvas, ratio, (dw, dh)


def _nms(dets: list[Detection], iou_thr: float) -> list[Detection]:
    if len(dets) <= 1:
        return dets
    dets = sorted(dets, key=lambda d: d.score, reverse=True)
    keep: list[Detection] = []
    while dets:
        best = dets.pop(0)
        keep.append(best)
        dets = [d for d in dets if _iou(best, d) < iou_thr]
    return keep


def _iou(a: Detection, b: Detection) -> float:
    x1, y1 = max(a.x1, b.x1), max(a.y1, b.y1)
    x2, y2 = min(a.x2, b.x2), min(a.y2, b.y2)
    iw, ih = max(0.0, x2 - x1), max(0.0, y2 - y1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0
