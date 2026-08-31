from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from stockwatch_app.config import ConfigError, ConfigManager, DEFAULT_CONFIG, validate_config
from stockwatch_app.controller import ApplicationController
from stockwatch_app.scheduler import Scheduler
from stockwatch_app.store import RuntimeStore
from stockwatch_app.tasks import TaskError, TaskRunner, read_analysis_article, scan_analyses
from stockwatch_app.web import DashboardServer


REPO_ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_default_round_trip_and_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            manager = ConfigManager(path)
            self.assertEqual(manager.load(), validate_config(DEFAULT_CONFIG))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rejects_unknown_tasks_and_invalid_time(self):
        payload = json.loads(json.dumps(DEFAULT_CONFIG))
        payload["tasks"]["pool_update"]["schedule"]["time"] = "25:00"
        with self.assertRaises(ConfigError):
            validate_config(payload)
        payload = json.loads(json.dumps(DEFAULT_CONFIG))
        payload["tasks"]["shell"] = payload["tasks"]["pool_update"]
        with self.assertRaises(ConfigError):
            validate_config(payload)


class RuntimeStoreTests(unittest.TestCase):
    def test_scheduled_slot_is_idempotent_and_abandoned_runs_are_interrupted(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RuntimeStore(Path(temporary) / "runtime.sqlite3")
            log = Path(temporary) / "run.log"
            first = store.create_run(
                "pool_update", trigger="schedule", scheduled_for="2026-08-31T06:00:00-07:00", log_path=log
            )
            second = store.create_run(
                "pool_update", trigger="schedule", scheduled_for="2026-08-31T06:00:00-07:00", log_path=log
            )
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            store.mark_running(first or "")
            self.assertEqual(store.interrupt_abandoned(), 1)
            self.assertEqual(store.get(first or "")["status"], "interrupted")


class AnalysisCatalogTests(unittest.TestCase):
    def test_catalog_and_safe_article_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            run = data / "tradingagents" / "lean" / "NVDA_20260831_070000"
            run.mkdir(parents=True)
            (run / "plan.md").write_text(
                "**Recommendation**: Hold\n**Rationale**: Evidence remains mixed.", encoding="utf-8"
            )
            (run / "news.md").write_text("# News\nplain text", encoding="utf-8")
            rows = scan_analyses(data)
            self.assertEqual(rows[0]["ticker"], "NVDA")
            self.assertEqual(rows[0]["recommendation"], "Hold")
            article = read_analysis_article(data, "NVDA_20260831_070000/news.md")
            self.assertIn("plain text", article["content"])
            with self.assertRaises(TaskError):
                read_analysis_article(data, "../.env")


class PublishTaskTests(unittest.TestCase):
    def test_publish_is_independent_and_records_local_only_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            support = Path(temporary)
            config = ConfigManager(support / "config.json")
            store = RuntimeStore(support / "runtime.sqlite3")
            runner = TaskRunner(
                source_root=REPO_ROOT,
                support_root=support,
                config=config,
                store=store,
            )
            run = support / "data" / "tradingagents" / "lean" / "AMD_20260831_070000"
            run.mkdir(parents=True)
            (run / "plan.md").write_text(
                "**Recommendation**: Hold\n**Rationale**: Local test result.", encoding="utf-8"
            )
            started = runner.start("publish_report")
            runner.wait(5)
            row = store.get(started["run_id"])
            self.assertEqual(row["status"], "success")
            self.assertIn("not sent to phone", row["summary"])
            reports = list((support / "reports").glob("*.md"))
            self.assertEqual(len(reports), 1)
            self.assertIn("未发送到手机", reports[0].read_text(encoding="utf-8"))


class SchedulerTests(unittest.TestCase):
    def test_next_run_uses_local_weekday_and_task_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            support = Path(temporary)
            manager = ConfigManager(support / "config.json")
            store = RuntimeStore(support / "runtime.sqlite3")
            runner = TaskRunner(
                source_root=REPO_ROOT, support_root=support, config=manager, store=store
            )
            scheduler = Scheduler(manager, runner)
            now = datetime.fromisoformat("2026-08-31T05:00:00-07:00")  # Monday
            next_runs = scheduler.next_runs(now)
            self.assertEqual(next_runs["pool_update"], "2026-08-31T06:00:00-07:00")
            self.assertIsNone(next_runs["analysis_update"])


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.controller = ApplicationController(
            source_root=REPO_ROOT, support_root=Path(self.temporary.name)
        )
        self.server = DashboardServer(self.controller, port=0)
        self.controller.start()
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.controller.stop()
        self.temporary.cleanup()

    def request(self, path: str, *, method: str = "GET", token: str | None = None):
        headers = {}
        body = None
        if method == "POST":
            headers["Origin"] = self.server.url.rstrip("/")
            headers["Content-Type"] = "application/json"
            if token:
                headers["X-StockWatch-Token"] = token
            body = b"{}"
        request = Request(self.server.url.rstrip("/") + path, data=body, headers=headers, method=method)
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_health_bootstrap_and_mutation_token(self):
        status, payload = self.request("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        _, bootstrap = self.request("/api/v1/bootstrap")
        self.assertEqual(len(bootstrap["tasks"]), 4)
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/v1/tasks/publish_report/run", method="POST")
        self.assertEqual(caught.exception.code, 403)
        status, accepted = self.request(
            "/api/v1/tasks/publish_report/run", method="POST", token=bootstrap["csrf_token"]
        )
        self.assertEqual(status, 202)
        self.assertEqual(accepted["task_name"], "publish_report")

    def test_static_dashboard_has_security_policy(self):
        request = Request(self.server.url)
        with urlopen(request, timeout=3) as response:
            body = response.read().decode("utf-8")
            self.assertIn("StockWatch", body)
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
