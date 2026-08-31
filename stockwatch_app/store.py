"""Durable runtime ledger for scheduled and manual application jobs."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RuntimeStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def session(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_runs (
                    run_id TEXT PRIMARY KEY,
                    task_name TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    scheduled_for TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    summary TEXT NOT NULL DEFAULT '',
                    log_path TEXT NOT NULL,
                    pid INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS task_schedule_once
                    ON task_runs(task_name, scheduled_for)
                    WHERE scheduled_for IS NOT NULL;
                CREATE INDEX IF NOT EXISTS task_runs_recent
                    ON task_runs(started_at DESC);
                """
            )

    def interrupt_abandoned(self) -> int:
        with self.session() as connection:
            cursor = connection.execute(
                """UPDATE task_runs
                   SET status = 'interrupted', finished_at = ?,
                       summary = CASE WHEN summary = '' THEN 'Application exited during the run'
                                      ELSE summary END
                   WHERE status IN ('queued', 'running', 'stopping')""",
                (utc_now(),),
            )
            return cursor.rowcount

    def create_run(
        self,
        task_name: str,
        *,
        trigger: str,
        log_path: Path,
        scheduled_for: str | None = None,
    ) -> str | None:
        run_id = uuid.uuid4().hex
        try:
            with self.session() as connection:
                connection.execute(
                    """INSERT INTO task_runs(
                           run_id, task_name, trigger, scheduled_for, started_at,
                           status, log_path
                       ) VALUES (?, ?, ?, ?, ?, 'queued', ?)""",
                    (run_id, task_name, trigger, scheduled_for, utc_now(), str(log_path)),
                )
        except sqlite3.IntegrityError:
            return None
        return run_id

    def mark_running(self, run_id: str, pid: int | None = None) -> None:
        with self.session() as connection:
            connection.execute(
                "UPDATE task_runs SET status = 'running', pid = ? WHERE run_id = ?",
                (pid, run_id),
            )

    def update_pid(self, run_id: str, pid: int | None) -> None:
        with self.session() as connection:
            connection.execute("UPDATE task_runs SET pid = ? WHERE run_id = ?", (pid, run_id))

    def mark_stopping(self, run_id: str) -> None:
        with self.session() as connection:
            connection.execute(
                "UPDATE task_runs SET status = 'stopping' WHERE run_id = ? AND status = 'running'",
                (run_id,),
            )

    def finish(self, run_id: str, *, status: str, exit_code: int | None, summary: str) -> None:
        with self.session() as connection:
            connection.execute(
                """UPDATE task_runs
                   SET finished_at = ?, status = ?, exit_code = ?, summary = ?, pid = NULL
                   WHERE run_id = ?""",
                (utc_now(), status, exit_code, summary[:2000], run_id),
            )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.session() as connection:
            rows = connection.execute(
                "SELECT * FROM task_runs ORDER BY started_at DESC LIMIT ?", (max(1, limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_by_task(self) -> dict[str, dict[str, Any]]:
        with self.session() as connection:
            rows = connection.execute(
                """SELECT r.* FROM task_runs r
                   JOIN (
                       SELECT task_name, MAX(started_at) AS latest
                       FROM task_runs GROUP BY task_name
                   ) x ON x.task_name = r.task_name AND x.latest = r.started_at"""
            ).fetchall()
        return {row["task_name"]: dict(row) for row in rows}

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute("SELECT * FROM task_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None
