"""SQLite event log (person sightings, snapshots, clips)."""
from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    duration    REAL,
    persons     INTEGER DEFAULT 1,
    faces       INTEGER DEFAULT 0,
    score       REAL,
    snapshot    TEXT,
    clip        TEXT,
    bbox        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day, started_at);
"""


class EventStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def add(
        self,
        started_at: datetime,
        persons: int,
        faces: int,
        score: float,
        snapshot: str,
        bbox: str,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events(day, started_at, persons, faces, score, snapshot, bbox)"
                " VALUES(?,?,?,?,?,?,?)",
                (started_at.date().isoformat(), started_at.isoformat(timespec="seconds"),
                 persons, faces, score, snapshot, bbox),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish(self, event_id: int, ended_at: datetime, persons: int, faces: int) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if not row:
                return
            started = datetime.fromisoformat(row["started_at"])
            self._conn.execute(
                "UPDATE events SET ended_at=?, duration=?, persons=?, faces=? WHERE id=?",
                (ended_at.isoformat(timespec="seconds"),
                 round((ended_at - started).total_seconds(), 1),
                 persons, faces, event_id),
            )
            self._conn.commit()

    def set_clip(self, event_id: int, clip: str, duration: Optional[float] = None) -> None:
        with self._lock:
            if duration is None:
                self._conn.execute("UPDATE events SET clip=? WHERE id=?", (clip, event_id))
            else:
                self._conn.execute(
                    "UPDATE events SET clip=?, duration=? WHERE id=?", (clip, duration, event_id)
                )
            self._conn.commit()

    def get(self, event_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def list(self, day: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            if day:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE day=? ORDER BY id DESC LIMIT ?", (day, limit)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def days(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, COUNT(*) AS events FROM events GROUP BY day ORDER BY day DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def counts(self, day: Optional[str] = None) -> int:
        with self._lock:
            if day:
                row = self._conn.execute("SELECT COUNT(*) c FROM events WHERE day=?", (day,)).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) c FROM events").fetchone()
        return int(row["c"]) if row else 0

    def delete(self, event_id: int) -> Optional[dict[str, Any]]:
        ev = self.get(event_id)
        if not ev:
            return None
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE id=?", (event_id,))
            self._conn.commit()
        return ev

    def purge_before(self, cutoff: date) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM events WHERE day < ?", (cutoff.isoformat(),))
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
