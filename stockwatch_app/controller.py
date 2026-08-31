"""Application facade used by the scheduler, HTTP API, and tests."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from stockwatch_app.config import ConfigManager, TASK_NAMES, default_support_root
from stockwatch_app.scheduler import Scheduler
from stockwatch_app.store import RuntimeStore
from stockwatch_app.tasks import (
    TaskError,
    TaskRunner,
    read_analysis_article,
    scan_analyses,
)


TASK_LABELS = {
    "pool_update": "候选股票池",
    "reddit_update": "Reddit 预抓取",
    "analysis_update": "Lean 分析师",
    "publish_report": "本地报告发布",
}


class ApplicationController:
    def __init__(
        self,
        *,
        source_root: Path,
        support_root: Path | None = None,
        python_bin: Path | None = None,
    ) -> None:
        self.source_root = Path(source_root).resolve()
        self.support_root = Path(support_root or default_support_root()).resolve()
        self.support_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.support_root, 0o700)
        self.config = ConfigManager(self.support_root / "config.json")
        self.store = RuntimeStore(self.support_root / "runtime.sqlite3")
        self.interrupted_on_start = self.store.interrupt_abandoned()
        self.runner = TaskRunner(
            source_root=self.source_root,
            support_root=self.support_root,
            config=self.config,
            store=self.store,
            python_bin=python_bin,
        )
        self.scheduler = Scheduler(self.config, self.runner)
        self.started_monotonic = time.monotonic()

    def start(self) -> None:
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.stop()
        self.runner.stop_all()
        self.runner.wait(timeout=15)

    def bootstrap(self) -> dict[str, Any]:
        configuration = self.config.load()
        latest = self.store.latest_by_task()
        active = self.runner.active()
        next_runs = self.scheduler.next_runs()
        tasks = []
        for task_name in TASK_NAMES:
            tasks.append(
                {
                    "name": task_name,
                    "label": TASK_LABELS[task_name],
                    "enabled": configuration["tasks"][task_name]["enabled"],
                    "active": active.get(task_name),
                    "latest": latest.get(task_name),
                    "next_run_at": next_runs.get(task_name),
                    "external_delivery": False,
                }
            )
        return {
            "service": {
                "status": "running",
                "uptime_seconds": int(time.monotonic() - self.started_monotonic),
                "scheduler_enabled": configuration["scheduler"]["enabled"],
                "interrupted_on_start": self.interrupted_on_start,
                "python": sys.version.split()[0],
                "claude_available": bool(_claude_path()),
                "phone_delivery": "not_implemented",
            },
            "tasks": tasks,
            "runs": self.store.recent(20),
        }

    def run_task(self, task_name: str) -> dict[str, Any]:
        return self.runner.start(task_name, trigger="manual")

    def stop_task(self, task_name: str) -> dict[str, Any]:
        if task_name not in TASK_NAMES:
            raise TaskError(f"unknown task: {task_name}")
        stopped = self.runner.stop(task_name)
        return {"task_name": task_name, "stopping": stopped}

    def load_config(self) -> dict[str, Any]:
        return self.config.load()

    def save_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        saved = self.config.save(payload)
        self.scheduler.reload()
        return saved

    def pool(self, limit: int = 100) -> dict[str, Any]:
        if not self.runner.pool_db.is_file():
            return {"available": False, "pool": [], "message": "股票池还没有建立"}
        from lean.pool_store import load_latest_pool

        snapshot = load_latest_pool(
            limit=max(1, min(limit, 500)), db_path=self.runner.pool_db
        )
        if snapshot is None:
            return {"available": False, "pool": [], "message": "股票池还没有建立"}
        return {
            "available": True,
            "run_id": snapshot.run_id,
            "analysis_date": snapshot.analysis_date,
            "as_of": snapshot.as_of,
            "written_at": snapshot.written_at,
            "status": snapshot.status,
            "degraded_sources": list(snapshot.degraded_sources),
            "age_hours": round(snapshot.age_hours(), 2),
            "total_candidates": snapshot.total_candidates,
            "sources": [vars(source) for source in snapshot.sources],
            "pool": [
                {
                    "rank": entry.rank,
                    "ticker": entry.ticker,
                    "name": entry.name,
                    "exchange": entry.exchange,
                    "score": round(entry.score, 6),
                    "sources": list(entry.sources),
                    "reasons": list(entry.reasons),
                    "mentions": entry.mentions,
                    "momentum": entry.momentum,
                    "price": entry.price,
                    "market_cap": entry.market_cap,
                }
                for entry in snapshot.entries
            ],
        }

    def analyses(self, limit: int = 100) -> dict[str, Any]:
        items = scan_analyses(self.runner.data_root, limit=max(1, min(limit, 500)))
        return {"available": bool(items), "items": items}

    def analysis_article(self, article_id: str) -> dict[str, str]:
        return read_analysis_article(self.runner.data_root, article_id)

    def reddit(self, limit: int = 50) -> dict[str, Any]:
        path = self.runner.reddit_db
        if not path.is_file():
            return {"available": False, "items": [], "message": "Reddit 缓存还没有建立"}
        uri = f"file:{path.resolve()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT r.* FROM raw_snapshots r
                   JOIN (
                       SELECT ticker, MAX(fetched_at) AS latest
                       FROM raw_snapshots GROUP BY ticker
                   ) x ON x.ticker = r.ticker AND x.latest = r.fetched_at
                   ORDER BY r.fetched_at DESC LIMIT ?""",
                (max(1, min(limit, 200)),),
            ).fetchall()
        except sqlite3.Error as exc:
            return {"available": False, "items": [], "message": f"Reddit 缓存不可读：{exc}"}
        finally:
            if "connection" in locals():
                connection.close()
        items = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            posts = []
            for post in payload.get("posts", [])[:8]:
                posts.append(
                    {
                        "title": str(post.get("title") or ""),
                        "subreddit": str(post.get("subreddit") or ""),
                        "permalink": _reddit_url(post.get("permalink")),
                        "published_at": post.get("published_at"),
                        "body": str(post.get("body") or "")[:1200],
                    }
                )
            items.append(
                {
                    "ticker": row["ticker"],
                    "analysis_date": row["analysis_date"],
                    "fetched_at": row["fetched_at"],
                    "status": row["status"],
                    "posts_stored": row["posts_stored"],
                    "comments_stored": row["comments_stored"],
                    "errors": payload.get("errors", []),
                    "posts": posts,
                }
            )
        return {"available": bool(items), "items": items}

    def runs(self, limit: int = 50) -> dict[str, Any]:
        return {"items": self.store.recent(max(1, min(limit, 200)))}

    def log(self, run_id: str, maximum: int = 200_000) -> dict[str, Any]:
        row = self.store.get(run_id)
        if not row:
            raise TaskError("run not found")
        log_root = self.runner.logs_root.resolve()
        path = Path(row["log_path"]).resolve()
        if log_root not in path.parents or not path.is_file():
            return {"run": row, "content": ""}
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(maximum)
        return {"run": row, "content": content}


def _claude_path() -> str | None:
    configured = os.environ.get("CLAUDE_CLI_BIN")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("claude")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "claude"
    return str(candidate) if candidate.is_file() else None


def _reddit_url(raw: Any) -> str:
    value = str(raw or "")
    if value.startswith(("https://www.reddit.com/", "https://reddit.com/")):
        return value
    return ""
