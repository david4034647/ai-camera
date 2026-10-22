"""Unit tests that do not need a camera."""
from __future__ import annotations

import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from app.config import Settings
from app.detector import Detection, _nms
from app.monitor import _parse_day
from app.recorder import DailyRecorder, _day_path, _next_segment_index, merge_day_segments
from app.storage import EventStore


_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


# --------------------------------------------------------------------------- #
# helpers / paths
# --------------------------------------------------------------------------- #
def test_parse_day():
    assert _parse_day("2026-10-22.mp4") == date(2026, 10, 22)
    assert _parse_day("2026-10-22_part3.mp4") == date(2026, 10, 22)
    assert _parse_day("nonsense.mp4") is None


def test_day_path_and_index(tmp_path: Path):
    day = date(2026, 1, 2)
    assert _day_path(tmp_path, day, 1).name == "2026-01-02.mp4"
    assert _day_path(tmp_path, day, 2).name == "2026-01-02_part2.mp4"
    assert _next_segment_index(tmp_path, day) == 1
    (tmp_path / "2026-01-02.mp4").write_bytes(b"x")
    assert _next_segment_index(tmp_path, day) == 2
    (tmp_path / "2026-01-02_part2.mp4").write_bytes(b"x")
    assert _next_segment_index(tmp_path, day) == 3


@needs_ffmpeg
def test_daily_recorder_frame_rate(tmp_path: Path):
    """The writer must emit a constant frame rate regardless of input bursts."""
    rec = DailyRecorder(tmp_path, 320, 180, fps=10, crf=30, maxrate="300k")
    rec.start()
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    for i in range(15):          # 1.5 s of wall clock
        rec.submit(frame)
        import time

        time.sleep(0.1)
    rec.stop()
    files = list(tmp_path.glob("*.mp4"))
    assert files, "no recording produced"
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(files[0])],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    duration = float(out)
    assert 1.0 <= duration <= 2.2, f"unexpected duration {duration}"
    assert rec.frames_written > 5


# --------------------------------------------------------------------------- #
# event store
# --------------------------------------------------------------------------- #
def test_event_store(tmp_path: Path):
    store = EventStore(tmp_path / "events.db")
    eid = store.add(datetime(2026, 10, 22, 13, 0, 0), 2, 1, 0.91, "events/x.jpg", "1,2,3,4")
    assert eid == 1
    store.finish(eid, datetime(2026, 10, 22, 13, 0, 9), 2, 2)
    store.set_clip(eid, "events/x.mp4", 9.0)
    ev = store.get(eid)
    assert ev["duration"] == 9.0
    assert ev["persons"] == 2 and ev["faces"] == 2
    assert store.counts("2026-10-22") == 1
    assert store.days()[0]["day"] == "2026-10-22"
    assert store.list("2026-10-22")[0]["id"] == eid

    # purge
    store.add(datetime(2026, 1, 1, 1, 0, 0), 1, 0, 0.5, "events/old.jpg", "")
    assert store.purge_before(date(2026, 10, 1)) == 1
    assert store.counts() == 1

    deleted = store.delete(eid)
    assert deleted["id"] == eid
    assert store.get(eid) is None
    store.close()


# --------------------------------------------------------------------------- #
# retention
# --------------------------------------------------------------------------- #
def test_retention_cleanup(tmp_path: Path):
    from app.monitor import Retention

    settings = Settings(data_dir=str(tmp_path))
    for sub in ("recordings", "events"):
        (tmp_path / sub).mkdir(parents=True)

    today = date.today()
    old = today - timedelta(days=settings.retention_days + 1)
    recent = today - timedelta(days=1)

    (tmp_path / "recordings" / f"{old.isoformat()}.mp4").write_bytes(b"x" * 100)
    (tmp_path / "recordings" / f"{recent.isoformat()}.mp4").write_bytes(b"y" * 100)

    (tmp_path / "events" / old.isoformat()).mkdir()
    (tmp_path / "events" / old.isoformat() / "snap.jpg").write_bytes(b"z" * 50)
    (tmp_path / "events" / recent.isoformat()).mkdir()
    (tmp_path / "events" / recent.isoformat() / "snap.jpg").write_bytes(b"z" * 50)

    store = EventStore(tmp_path / "events.db")
    store.add(datetime.combine(old, datetime.min.time()), 1, 0, 0.9, "a", "")
    store.add(datetime.combine(recent, datetime.min.time()), 1, 0, 0.9, "b", "")

    retention = Retention(settings, store)
    result = retention.cleanup()

    assert result["removed_files"] >= 2
    assert not (tmp_path / "recordings" / f"{old.isoformat()}.mp4").exists()
    assert (tmp_path / "recordings" / f"{recent.isoformat()}.mp4").exists()
    assert not (tmp_path / "events" / old.isoformat()).exists()
    assert (tmp_path / "events" / recent.isoformat()).exists()
    assert store.counts() == 1
    store.close()


# --------------------------------------------------------------------------- #
# segmentation merge
# --------------------------------------------------------------------------- #
def _make_video(path: Path, seconds: int = 1) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=160x96:rate=10:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


@needs_ffmpeg
def test_merge_day_segments(tmp_path: Path):
    day = "2026-01-01"
    _make_video(tmp_path / f"{day}.mp4", 1)
    _make_video(tmp_path / f"{day}_part2.mp4", 1)
    merge_day_segments(tmp_path)
    assert (tmp_path / f"{day}.mp4").exists()
    assert not (tmp_path / f"{day}_part2.mp4").exists()
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(tmp_path / f"{day}.mp4")], capture_output=True, text=True, check=True,
    ).stdout.strip())
    assert 1.6 <= dur <= 2.4, f"merged duration {dur}"


# --------------------------------------------------------------------------- #
# detector maths
# --------------------------------------------------------------------------- #
def test_nms_keeps_best_overlapping():
    a = Detection(0, 0, 100, 100, 0.9)
    b = Detection(5, 5, 100, 100, 0.8)
    c = Detection(300, 300, 400, 400, 0.7)
    kept = _nms([a, b, c], 0.45)
    assert len(kept) == 2
    assert kept[0].score == pytest.approx(0.9)
    assert kept[1] is c
