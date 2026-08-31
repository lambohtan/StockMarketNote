"""Independent task adapters and cancellable subprocess supervision."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from stockwatch_app.config import ConfigManager, TASK_NAMES
from stockwatch_app.store import RuntimeStore


TERMINAL_STATUSES = {"success", "degraded", "failed", "cancelled", "interrupted"}


@dataclass
class ActiveTask:
    task_name: str
    run_id: str
    thread: threading.Thread
    cancel: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen[str] | None = None


class TaskError(RuntimeError):
    pass


class TaskRunner:
    def __init__(
        self,
        *,
        source_root: Path,
        support_root: Path,
        config: ConfigManager,
        store: RuntimeStore,
        python_bin: Path | None = None,
    ) -> None:
        self.source_root = Path(source_root).resolve()
        self.support_root = Path(support_root).resolve()
        self.data_root = self.support_root / "data"
        self.logs_root = self.support_root / "logs"
        self.reports_root = self.support_root / "reports"
        self.pool_db = self.data_root / "pool" / "pool.sqlite3"
        self.reddit_db = self.data_root / "tradingagents" / "reddit-prefetch.sqlite3"
        self.python_bin = Path(python_bin or sys.executable).resolve()
        self.config = config
        self.store = store
        self._lock = threading.RLock()
        self._active: dict[str, ActiveTask] = {}
        for path in (self.data_root, self.logs_root, self.reports_root):
            path.mkdir(parents=True, exist_ok=True)

    def active(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                name: {
                    "run_id": active.run_id,
                    "pid": active.process.pid if active.process else None,
                    "stopping": active.cancel.is_set(),
                }
                for name, active in self._active.items()
            }

    def start(
        self, task_name: str, *, trigger: str = "manual", scheduled_for: str | None = None
    ) -> dict[str, Any]:
        if task_name not in TASK_NAMES:
            raise TaskError(f"unknown task: {task_name}")
        with self._lock:
            if task_name in self._active:
                raise TaskError(f"{task_name} is already running")
            stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
            log_path = self.logs_root / task_name / f"{stamp}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            run_id = self.store.create_run(
                task_name,
                trigger=trigger,
                scheduled_for=scheduled_for,
                log_path=log_path,
            )
            if run_id is None:
                raise TaskError(f"{task_name} was already scheduled for {scheduled_for}")
            placeholder = threading.Thread()
            active = ActiveTask(task_name=task_name, run_id=run_id, thread=placeholder)
            thread = threading.Thread(
                target=self._execute,
                args=(active, log_path),
                name=f"stockwatch-{task_name}",
                daemon=True,
            )
            active.thread = thread
            self._active[task_name] = active
            thread.start()
        return {"task_name": task_name, "run_id": run_id, "status": "queued"}

    def stop(self, task_name: str) -> bool:
        with self._lock:
            active = self._active.get(task_name)
            if not active:
                return False
            active.cancel.set()
            self.store.mark_stopping(active.run_id)
            process = active.process
        if process and process.poll() is None:
            self._terminate_process_group(process)
        return True

    def stop_all(self) -> None:
        with self._lock:
            names = list(self._active)
        for task_name in names:
            self.stop(task_name)

    def wait(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = [active.thread for active in self._active.values()]
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _execute(self, active: ActiveTask, log_path: Path) -> None:
        self.store.mark_running(active.run_id)
        status, exit_code, summary = "failed", 2, "Task did not start"
        try:
            with log_path.open("a", encoding="utf-8", buffering=1) as log:
                log.write(
                    f"StockWatch task={active.task_name} run={active.run_id} "
                    f"started={datetime.now().astimezone().isoformat()}\n"
                )
                handler: Callable[[ActiveTask, Any], tuple[str, int, str]] = getattr(
                    self, f"_run_{active.task_name}"
                )
                status, exit_code, summary = handler(active, log)
        except Exception as exc:  # noqa: BLE001 - task failures must enter the durable ledger
            summary = f"{type(exc).__name__}: {exc}"
            try:
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"\nERROR {summary}\n")
            except OSError:
                pass
        finally:
            if active.cancel.is_set() and status not in {"success", "degraded"}:
                status, summary = "cancelled", "Stopped by user"
            self.store.finish(
                active.run_id, status=status, exit_code=exit_code, summary=summary
            )
            with self._lock:
                self._active.pop(active.task_name, None)

    def _base_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "STOCKWATCH_DATA_DIR": str(self.data_root),
                "STOCKWATCH_SUPPORT_DIR": str(self.support_root),
                "STOCKWATCH_ENV_FILE": str(self.support_root / ".env"),
            }
        )
        claude = env.get("CLAUDE_CLI_BIN") or _find_claude()
        if claude:
            env["CLAUDE_CLI_BIN"] = claude
            env["PATH"] = str(Path(claude).parent) + os.pathsep + env.get("PATH", "")
        return env

    def _command(
        self, active: ActiveTask, log: Any, args: list[str], *, label: str
    ) -> int:
        if active.cancel.is_set():
            return 130
        command = [str(self.python_bin), *args]
        log.write(f"\n[{label}] {' '.join(_display_arg(part) for part in command)}\n")
        process = subprocess.Popen(
            command,
            cwd=self.source_root,
            env=self._base_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        with self._lock:
            active.process = process
            self.store.update_pid(active.run_id, process.pid)
        assert process.stdout is not None
        try:
            for line in process.stdout:
                log.write(line)
                if active.cancel.is_set() and process.poll() is None:
                    self._terminate_process_group(process)
            return process.wait()
        finally:
            with self._lock:
                active.process = None
                self.store.update_pid(active.run_id, None)

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _run_pool_update(self, active: ActiveTask, log: Any) -> tuple[str, int, str]:
        params = self.config.load()["tasks"]["pool_update"]["parameters"]
        command = [
            str(self.source_root / "build_pool.py"),
            "--top",
            str(params["top"]),
            "--min-price",
            str(params["min_price"]),
            "--min-market-cap",
            str(params["min_market_cap"]),
            "--db",
            str(self.pool_db),
            "--config",
            str(self.source_root / "config" / "pool.json"),
            "--json",
        ]
        code = self._command(active, log, command, label="pool update")
        if active.cancel.is_set():
            return "cancelled", code, "Stopped by user"
        if code == 0:
            return "success", code, "Pool snapshot updated"
        if code == 1:
            return "degraded", code, "Pool updated with one or more degraded sources"
        return "failed", code, "Pool update failed; the previous snapshot remains current"

    def _latest_pool_payload(self, limit: int) -> dict[str, Any]:
        if not self.pool_db.is_file():
            raise TaskError("No pool snapshot is available; run pool_update first")
        from lean.pool_store import load_latest_pool

        snapshot = load_latest_pool(limit=limit, db_path=self.pool_db)
        if snapshot is None:
            raise TaskError("No pool snapshot is available; run pool_update first")
        return snapshot.to_pool_payload()

    def _run_reddit_update(self, active: ActiveTask, log: Any) -> tuple[str, int, str]:
        params = self.config.load()["tasks"]["reddit_update"]["parameters"]
        payload = self._latest_pool_payload(int(params["top"]))
        inputs = self.support_root / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        pool_file = inputs / f"reddit-{active.run_id}.json"
        _atomic_json(pool_file, payload)
        rate_root = self.data_root / "tradingagents"
        command = [
            str(self.source_root / "prefetch_reddit.py"),
            "--pool-file",
            str(pool_file),
            "--analysis-date",
            str(payload["analysis_date"]),
            "--window-hours",
            str(params["window_hours"]),
            "--comments-per-post",
            str(params["comments_per_post"]),
            "--min-interval-seconds",
            str(params["min_interval_seconds"]),
            "--timeout",
            str(params["timeout"]),
            "--retention-days",
            str(params["retention_days"]),
            "--subreddits",
            str(params["subreddits"]),
            "--cache",
            str(self.reddit_db),
            "--rate-lock",
            str(rate_root / ".reddit-rate-limit.lock"),
            "--rate-state",
            str(rate_root / "reddit-rate-limit.json"),
            "--json-summary",
        ]
        code = self._command(active, log, command, label="reddit update")
        if active.cancel.is_set():
            return "cancelled", code, "Stopped by user"
        if code == 0:
            return "success", code, f"Reddit cache updated for {len(payload['tickers'])} tickers"
        if code == 1:
            return "degraded", code, "Reddit cache contains visible partial/limited results"
        return "failed", code, "Reddit prefetch could not start"

    def _run_analysis_update(self, active: ActiveTask, log: Any) -> tuple[str, int, str]:
        params = self.config.load()["tasks"]["analysis_update"]["parameters"]
        payload = self._latest_pool_payload(int(params["top"]))
        tickers = [item["ticker"] for item in payload["tickers"]]
        succeeded: list[str] = []
        failed: list[str] = []
        for index, ticker in enumerate(tickers, start=1):
            if active.cancel.is_set():
                return "cancelled", 130, f"Stopped after {len(succeeded)} of {len(tickers)} tickers"
            command = [
                str(self.source_root / "run_lean.py"),
                ticker,
                "--date",
                str(payload["analysis_date"]),
                "--model",
                str(params["model"]),
                "--deep",
                str(params["deep"]),
                "--evidence",
                str(params["evidence"]),
                "--tool-rounds",
                str(params["tool_rounds"]),
                "--analysts",
                str(params["analysts"]),
                "--rounds",
                str(params["rounds"]),
                "--judge-samples",
                str(params["judge_samples"]),
                "--lang",
                str(params["lang"]),
                "--save",
            ]
            code = self._command(active, log, command, label=f"analysis {index}/{len(tickers)} {ticker}")
            (succeeded if code == 0 else failed).append(ticker)
            if active.cancel.is_set():
                return "cancelled", code, f"Stopped after {len(succeeded)} of {len(tickers)} tickers"
        if failed and succeeded:
            return "degraded", 1, f"Completed {len(succeeded)}; failed: {', '.join(failed)}"
        if failed:
            return "failed", 2, f"All analyses failed: {', '.join(failed)}"
        return "success", 0, f"Completed lean research for {len(succeeded)} tickers"

    def _run_publish_report(self, active: ActiveTask, log: Any) -> tuple[str, int, str]:
        params = self.config.load()["tasks"]["publish_report"]["parameters"]
        analyses = scan_analyses(self.data_root, limit=int(params["analysis_limit"]))
        if not analyses:
            return "failed", 2, "No saved lean analyses are available to publish"
        stamp = datetime.now().astimezone()
        output = self.reports_root / f"stockwatch-{stamp.date().isoformat()}.md"
        lines = [
            f"# StockWatch 本地研究汇总 · {stamp.date().isoformat()}",
            "",
            f"生成时间：{stamp.isoformat(timespec='seconds')}",
            "",
            "这是本地研究材料，不是买卖指令。本文件未发送到手机。",
            "",
        ]
        for item in analyses:
            lines.extend(
                [
                    f"## {item['ticker']} · {item['created_at']}",
                    "",
                    f"- 评级：{item.get('recommendation') or '未产出'}",
                    f"- 文章：{', '.join(article['title'] for article in item['articles'])}",
                    "",
                    item.get("rationale") or "未提取理由，请在 Dashboard 打开原文。",
                    "",
                ]
            )
        temporary = output.with_suffix(".tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8")
        os.replace(temporary, output)
        log.write(f"Published local report: {output}\n")
        return "success", 0, f"Local report published: {output.name} (not sent to phone)"


def _find_claude() -> str | None:
    candidates = [
        Path.home() / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _display_arg(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=,+-]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


ARTICLE_TITLES = {
    "plan": "研究结论",
    "market": "市场分析师",
    "sentiment": "情绪分析师",
    "news": "新闻分析师",
    "fundamentals": "基本面分析师",
    "bull": "多方研究员",
    "bear": "空方研究员",
}


def scan_analyses(data_root: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    root = Path(data_root) / "tradingagents" / "lean"
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        match = re.fullmatch(r"([A-Za-z0-9._^=+-]{1,32})_(\d{8})_(\d{6})", directory.name)
        if not match:
            continue
        ticker, day, clock = match.groups()
        created_at = f"{day[:4]}-{day[4:6]}-{day[6:]} {clock[:2]}:{clock[2:4]}:{clock[4:]}"
        articles = []
        for key, title in ARTICLE_TITLES.items():
            path = directory / f"{key}.md"
            if path.is_file():
                articles.append(
                    {"id": f"{directory.name}/{path.name}", "kind": key, "title": title}
                )
        plan_path = directory / "plan.md"
        plan = _read_text(plan_path, 400_000) if plan_path.is_file() else ""
        rows.append(
            {
                "id": directory.name,
                "ticker": ticker.upper(),
                "created_at": created_at,
                "recommendation": _markdown_field(plan, "Recommendation"),
                "rationale": _markdown_field(plan, "Rationale"),
                "articles": articles,
            }
        )
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return rows[: max(1, limit)]


def read_analysis_article(data_root: Path, article_id: str) -> dict[str, str]:
    parts = Path(article_id).parts
    if len(parts) != 2 or parts[0] in {".", ".."} or parts[1] in {".", ".."}:
        raise TaskError("invalid article id")
    if not re.fullmatch(r"[A-Za-z0-9._^=+-]{1,32}_\d{8}_\d{6}", parts[0]):
        raise TaskError("invalid article directory")
    if parts[1] not in {f"{key}.md" for key in ARTICLE_TITLES}:
        raise TaskError("invalid article name")
    root = (Path(data_root) / "tradingagents" / "lean").resolve()
    path = (root / parts[0] / parts[1]).resolve()
    if path.parent.parent != root or not path.is_file():
        raise TaskError("article not found")
    kind = path.stem
    return {
        "id": article_id,
        "title": ARTICLE_TITLES[kind],
        "content": _read_text(path, 1_000_000),
    }


def _read_text(path: Path, maximum: int) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.read(maximum)


def _markdown_field(markdown: str, label: str) -> str:
    match = re.search(
        rf"\*\*{re.escape(label)}(?:\*\*:|:\*\*)\s*(.+?)(?=\n\*\*|\Z)", markdown, re.S
    )
    return match.group(1).strip() if match else ""
