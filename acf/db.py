"""Lapisan database SQLite (pengganti Postgres untuk v1) — padanan Fase 1 Â§11.

Dua tabel sederhana: projects dan clips. Cukup untuk melacak status & hasil.
File DB disimpan di <projects_dir>/acf.db.
"""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    status      TEXT,
    source_path TEXT,
    created     TEXT,
    updated     TEXT
);
CREATE TABLE IF NOT EXISTS clips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT,
    idx         INTEGER,
    start       REAL,
    end         REAL,
    score       REAL,
    reason      TEXT,
    render_path TEXT,
    status      TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
"""


class DB:
    def __init__(self, projects_dir: str):
        os.makedirs(projects_dir, exist_ok=True)
        self.path = os.path.join(projects_dir, "acf.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---- projects ----
    def add_project(self, pid: str, name: str, source_path: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT OR REPLACE INTO projects (id,name,status,source_path,created,updated) "
            "VALUES (?,?,?,?,?,?)",
            (pid, name, "NEW", source_path, now, now),
        )
        self.conn.commit()

    def set_status(self, pid: str, status: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE projects SET status=?, updated=? WHERE id=?", (status, now, pid)
        )
        self.conn.commit()

    # ---- clips ----
    def add_clip(self, pid: str, idx: int, start: float, end: float,
                 score: float, reason: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO clips (project_id,idx,start,end,score,reason,status) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, idx, start, end, score, reason, "PLANNED"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_clip_render(self, clip_id: int, render_path: str, status: str) -> None:
        self.conn.execute(
            "UPDATE clips SET render_path=?, status=? WHERE id=?",
            (render_path, status, clip_id),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
