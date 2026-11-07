"""SQLite state: tasks, per-project sessions, approvals, audit log, seen nonces.

The PreToolUse hook runs in its own process and writes audit rows too, so the database uses
WAL mode and a busy timeout. Every public method opens a short transaction.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    project TEXT NOT NULL,
    text TEXT NOT NULL,
    tier TEXT NOT NULL,
    resume INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    session_id TEXT,
    branch TEXT,
    base_ref TEXT,
    result_json TEXT,
    report_path TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, created_at);
CREATE TABLE IF NOT EXISTS sessions (
    project TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    branch TEXT,
    task_id TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, ts REAL NOT NULL);
"""

ACTIVE = ("queued", "running")
FINAL = ("done", "failed", "stopped", "interrupted", "refused")


@dataclass
class TaskRow:
    id: str
    created_at: float
    updated_at: float
    project: str
    text: str
    tier: str
    resume: bool
    status: str
    source: str
    session_id: str | None
    branch: str | None
    base_ref: str | None
    result_json: str | None
    report_path: str | None
    error: str | None

    @property
    def result(self) -> dict[str, Any] | None:
        return json.loads(self.result_json) if self.result_json else None


def new_task_id() -> str:
    # 8 lowercase hex chars: short enough to read out, plenty for one user's tasks.
    return secrets.token_hex(4)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._tx() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.executescript(SCHEMA)
            c.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------------ tasks

    def add_task(
        self,
        project: str,
        text: str,
        tier: str,
        resume: bool = False,
        source: str = "",
        status: str = "queued",
        task_id: str | None = None,
    ) -> TaskRow:
        now = time.time()
        tid = task_id or new_task_id()
        with self._tx() as c:
            c.execute(
                "INSERT INTO tasks(id, created_at, updated_at, project, text, tier, resume,"
                " status, source) VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, now, now, project, text, tier, int(resume), status, source),
            )
        task = self.get_task(tid)
        assert task is not None
        return task

    def get_task(self, task_id: str) -> TaskRow | None:
        with self._tx() as c:
            row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task(row) if row else None

    _UPDATABLE = frozenset(
        {"status", "session_id", "branch", "base_ref", "result_json", "report_path", "error"}
    )

    def update_task(self, task_id: str, **fields: Any) -> None:
        bad = set(fields) - self._UPDATABLE
        if bad:
            raise ValueError(f"cannot update {sorted(bad)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as c:
            c.execute(
                f"UPDATE tasks SET {cols}, updated_at = ? WHERE id = ?",  # noqa: S608 - keys checked
                (*fields.values(), time.time(), task_id),
            )

    def tasks(self, status: tuple[str, ...] | None = None, limit: int = 50) -> list[TaskRow]:
        with self._tx() as c:
            if status:
                marks = ",".join("?" * len(status))
                rows = c.execute(
                    f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY created_at LIMIT ?",  # noqa: S608
                    (*status, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [_task(r) for r in rows]

    def latest_task(self, project: str | None = None) -> TaskRow | None:
        with self._tx() as c:
            if project:
                row = c.execute(
                    "SELECT * FROM tasks WHERE project = ? ORDER BY created_at DESC LIMIT 1",
                    (project,),
                ).fetchone()
            else:
                row = c.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
        return _task(row) if row else None

    def mark_interrupted(self) -> list[str]:
        """On startup, tasks left 'running' by a crash become 'interrupted'."""
        with self._tx() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM tasks WHERE status = 'running'")]
            c.execute(
                "UPDATE tasks SET status = 'interrupted', updated_at = ?,"
                " error = 'daemon restarted while the task was running' WHERE status = 'running'",
                (time.time(),),
            )
        return ids

    # ------------------------------------------------------------------ sessions

    def set_session(
        self, project: str, session_id: str, branch: str | None, task_id: str | None
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO sessions(project, session_id, branch, task_id, updated_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET"
                " session_id = excluded.session_id, branch = excluded.branch,"
                " task_id = excluded.task_id, updated_at = excluded.updated_at",
                (project, session_id, branch, task_id, time.time()),
            )

    def get_session(self, project: str) -> dict[str, Any] | None:
        with self._tx() as c:
            row = c.execute("SELECT * FROM sessions WHERE project = ?", (project,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ audit

    def audit(self, kind: str, detail: dict[str, Any], task_id: str | None = None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO audit(ts, task_id, kind, detail) VALUES (?,?,?,?)",
                (time.time(), task_id, kind, json.dumps(detail, default=str)),
            )

    def audit_log(self, task_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._tx() as c:
            if task_id:
                rows = c.execute(
                    "SELECT * FROM audit WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    # ------------------------------------------------------------------ nonces

    def use_nonce(self, nonce: str, window_s: float) -> bool:
        """Record a nonce. False if it was already used inside the window (a replay)."""
        now = time.time()
        with self._tx() as c:
            c.execute("DELETE FROM nonces WHERE ts < ?", (now - 2 * window_s,))
            try:
                c.execute("INSERT INTO nonces(nonce, ts) VALUES (?, ?)", (nonce, now))
            except sqlite3.IntegrityError:
                return False
        return True


def _task(row: sqlite3.Row) -> TaskRow:
    d = dict(row)
    d["resume"] = bool(d["resume"])
    return TaskRow(**d)
