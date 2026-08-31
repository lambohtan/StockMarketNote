"""Small local-wall-clock scheduler with durable per-slot deduplication."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from stockwatch_app.config import ConfigManager, TASK_NAMES
from stockwatch_app.tasks import TaskError, TaskRunner


class Scheduler:
    def __init__(self, config: ConfigManager, runner: TaskRunner) -> None:
        self.config = config
        self.runner = runner
        self.started_at = datetime.now().astimezone()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="stockwatch-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=10)

    def reload(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self.config.load()
                if payload["scheduler"]["enabled"]:
                    self._dispatch_due(payload, datetime.now().astimezone())
                wait = int(payload["scheduler"]["poll_seconds"])
            except Exception:  # keep the long-running host alive; config errors surface in the UI
                wait = 15
            self._wake.wait(wait)
            self._wake.clear()

    def _dispatch_due(self, payload: dict[str, Any], now: datetime) -> None:
        catch_up = timedelta(minutes=int(payload["scheduler"]["catch_up_minutes"]))
        for task_name in TASK_NAMES:
            task = payload["tasks"][task_name]
            if not task["enabled"] or now.weekday() not in task["schedule"]["days"]:
                continue
            hour, minute = (int(part) for part in task["schedule"]["time"].split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now <= target + catch_up:
                try:
                    self.runner.start(
                        task_name,
                        trigger="schedule",
                        scheduled_for=target.isoformat(),
                    )
                except TaskError:
                    pass

    def next_runs(self, now: datetime | None = None) -> dict[str, str | None]:
        now = now or datetime.now().astimezone()
        payload = self.config.load()
        if not payload["scheduler"]["enabled"]:
            return {task_name: None for task_name in TASK_NAMES}
        result: dict[str, str | None] = {}
        for task_name in TASK_NAMES:
            task = payload["tasks"][task_name]
            if not task["enabled"]:
                result[task_name] = None
                continue
            hour, minute = (int(part) for part in task["schedule"]["time"].split(":"))
            upcoming = None
            for offset in range(8):
                day = now + timedelta(days=offset)
                candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate.weekday() in task["schedule"]["days"] and candidate > now:
                    upcoming = candidate.isoformat()
                    break
            result[task_name] = upcoming
        return result

