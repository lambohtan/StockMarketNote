from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TESTS))

import read_pool as read_cli
import build_pool as build_cli
from lean.pool import DEFAULT_WEIGHTS, APEWISDOM_ALL, STOCKTWITS_TRENDING, build_pool
from lean.pool_config import PoolConfig, PoolConfigError, load_config
from lean.pool_store import PoolStore, load_latest_pool
from lean.reddit_prefetch import load_pool
from test_pool import AS_OF, FakeClient, SourceFetchError, stocktwits_payload

LATER = AS_OF + timedelta(hours=6)


def report(client=None, as_of=AS_OF, top_n=20):
    return build_pool(client or FakeClient(), as_of=as_of,
                      analysis_date=as_of.date().isoformat(), top_n=top_n)


class ConfigTest(unittest.TestCase):
    def test_built_in_defaults_apply_without_a_config_file(self):
        config = load_config(path=None, env={}, config_root=Path("/nonexistent"))
        self.assertEqual(config.top_n, 20)
        self.assertEqual(config.min_price, 3.0)
        self.assertEqual(config.weights, dict(DEFAULT_WEIGHTS))

    def test_file_then_env_then_explicit_override_win_in_that_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pool.json"
            path.write_text(json.dumps({"top_n": 30, "min_price": 5.0, "keep_runs": 7}),
                            encoding="utf-8")
            from_file = load_config(path=path, env={})
            self.assertEqual((from_file.top_n, from_file.min_price, from_file.keep_runs),
                             (30, 5.0, 7))

            from_env = load_config(path=path, env={"POOL_TOP_N": "40"})
            self.assertEqual(from_env.top_n, 40)
            self.assertEqual(from_env.min_price, 5.0)

            explicit = load_config(path=path, env={"POOL_TOP_N": "40"}, overrides={"top_n": 5})
            self.assertEqual(explicit.top_n, 5)

    def test_partial_weights_merge_over_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pool.json"
            path.write_text(json.dumps({"weights": {STOCKTWITS_TRENDING: 0.5}}), encoding="utf-8")
            config = load_config(path=path, env={})
            self.assertEqual(config.weights[STOCKTWITS_TRENDING], 0.5)
            self.assertEqual(config.weights[APEWISDOM_ALL], DEFAULT_WEIGHTS[APEWISDOM_ALL])

    def test_invalid_values_are_rejected_loudly(self):
        for payload in ({"top_n": 0}, {"top_n": "many"}, {"min_price": -1},
                        {"keep_runs": 0}, {"weights": {"apewisdom:all-stocks": "high"}}):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "pool.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(PoolConfigError, msg=str(payload)):
                    load_config(path=path, env={})

    def test_explicitly_named_missing_config_file_is_an_error(self):
        with self.assertRaises(PoolConfigError):
            load_config(path=Path("/nonexistent/pool.json"), env={})

    def test_tracked_default_config_file_parses(self):
        config = load_config(path=REPO_ROOT / "config" / "pool.json", env={})
        self.assertGreaterEqual(config.top_n, 1)
        self.assertIsInstance(config, PoolConfig)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "pool.sqlite3"

    def test_snapshot_keeps_every_candidate_and_full_provenance(self):
        built = report()
        store = PoolStore(self.db)
        store.save_report(built)
        snapshot = load_latest_pool(db_path=self.db)
        self.assertEqual(snapshot.total_candidates, len(built.considered))
        self.assertEqual([e.ticker for e in snapshot.entries],
                         [e.ticker for e in built.considered])
        self.assertEqual([e.rank for e in snapshot.entries][:3], [1, 2, 3])
        top = snapshot.entries[0]
        self.assertEqual(top.market, "US")
        self.assertTrue(top.sources and top.reasons)
        self.assertEqual(top.source_health[top.sources[0]], "ok")

    def test_limit_reads_the_top_of_the_stored_ranking(self):
        PoolStore(self.db).save_report(report())
        snapshot = load_latest_pool(limit=3, db_path=self.db)
        self.assertEqual(len(snapshot.entries), 3)
        self.assertGreater(snapshot.total_candidates, 3)

    def test_pool_size_change_needs_no_new_run(self):
        built = report(top_n=2)
        PoolStore(self.db).save_report(built)
        stored = load_latest_pool(limit=len(built.considered), db_path=self.db)
        self.assertEqual(len(stored.entries), len(built.considered))
        self.assertGreater(len(stored.entries), built.top_n)

    def test_degraded_run_becomes_latest_but_stays_marked(self):
        store = PoolStore(self.db)
        store.save_report(report())
        store.save_report(report(FakeClient(stocktwits=SourceFetchError("HTTP 429")), LATER))
        snapshot = load_latest_pool(db_path=self.db)
        self.assertEqual(snapshot.status, "degraded")
        self.assertEqual(snapshot.degraded_sources, (STOCKTWITS_TRENDING,))
        self.assertEqual(
            {s.source: s.health for s in snapshot.sources}[STOCKTWITS_TRENDING], "failed")

    def test_require_complete_falls_back_to_the_last_clean_run(self):
        store = PoolStore(self.db)
        store.save_report(report())
        store.save_report(report(FakeClient(stocktwits=SourceFetchError("HTTP 429")), LATER))
        clean = load_latest_pool(require_complete=True, db_path=self.db)
        self.assertEqual(clean.status, "complete")
        self.assertEqual(clean.as_of, report().as_of)

    def test_require_complete_returns_none_when_no_clean_run_exists(self):
        PoolStore(self.db).save_report(report(FakeClient(stocktwits=SourceFetchError("x"))))
        self.assertIsNone(load_latest_pool(require_complete=True, db_path=self.db))

    def test_empty_source_run_still_counts_as_complete(self):
        PoolStore(self.db).save_report(report(FakeClient(stocktwits=stocktwits_payload([]))))
        self.assertEqual(load_latest_pool(db_path=self.db).status, "complete")

    def test_reading_a_missing_database_returns_none_and_creates_nothing(self):
        missing = Path(self._tmp.name) / "absent.sqlite3"
        self.assertIsNone(load_latest_pool(db_path=missing))
        self.assertFalse(missing.exists())

    def test_staleness_is_reported_not_guessed(self):
        PoolStore(self.db).save_report(report())
        snapshot = load_latest_pool(db_path=self.db)
        fresh = datetime.fromisoformat(snapshot.written_at.replace("Z", "+00:00"))
        self.assertLess(snapshot.age_hours(fresh + timedelta(hours=1)), 1.01)
        self.assertFalse(snapshot.is_stale(36.0, fresh + timedelta(hours=1)))
        self.assertTrue(snapshot.is_stale(36.0, fresh + timedelta(hours=40)))

    def test_runs_history_is_newest_first_and_pruned_to_keep_runs(self):
        store = PoolStore(self.db)
        for hours in range(4):
            store.save_report(report(as_of=AS_OF + timedelta(hours=hours)))
        store.prune(keep_runs=2)
        runs = store.runs()
        self.assertEqual(len(runs), 2)
        self.assertGreater(runs[0].as_of, runs[1].as_of)
        self.assertEqual(load_latest_pool(db_path=self.db).as_of, runs[0].as_of)

    def test_pruning_never_drops_the_latest_complete_pointer_target(self):
        store = PoolStore(self.db)
        store.save_report(report())
        for hours in (1, 2, 3):
            store.save_report(report(FakeClient(stocktwits=SourceFetchError("x")),
                                     AS_OF + timedelta(hours=hours)))
        store.prune(keep_runs=1)
        self.assertIsNotNone(load_latest_pool(require_complete=True, db_path=self.db))

    def test_exclusions_and_source_counts_round_trip(self):
        PoolStore(self.db).save_report(report())
        snapshot = load_latest_pool(db_path=self.db)
        self.assertTrue(any(e.reason == "not_us_listed" for e in snapshot.exclusions))
        self.assertEqual(len(snapshot.sources), 4)

    def test_saved_run_is_a_single_transaction(self):
        store = PoolStore(self.db)
        store.save_report(report())
        with sqlite3.connect(self.db) as connection:
            runs = connection.execute("SELECT COUNT(*) FROM pool_runs").fetchone()[0]
            entries = connection.execute("SELECT COUNT(*) FROM pool_entries").fetchone()[0]
        self.assertEqual(runs, 1)
        self.assertEqual(entries, len(report().considered))


class BuildCliStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "pool.sqlite3"

    def run_cli(self, argv, client=None):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = build_cli.main([*argv, "--db", str(self.db)], client=client or FakeClient())
        return code, buffer.getvalue()

    def test_update_writes_the_snapshot_into_the_database(self):
        code, _ = self.run_cli(["--as-of", "2026-08-31T14:00:00Z", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(load_latest_pool(db_path=self.db).status, "complete")

    def test_failed_listing_leaves_the_previous_pool_intact(self):
        self.run_cli(["--as-of", "2026-08-31T14:00:00Z", "--json"])
        before = load_latest_pool(db_path=self.db)
        code, _ = self.run_cli(["--as-of", "2026-09-01T14:00:00Z", "--json"],
                               client=FakeClient(listings={"nasdaq": SourceFetchError("HTTP 403")}))
        after = load_latest_pool(db_path=self.db)
        self.assertEqual(code, 2)
        self.assertEqual(after.run_id, before.run_id)

    def test_no_store_flag_skips_the_database(self):
        code, _ = self.run_cli(["--as-of", "2026-08-31T14:00:00Z", "--json", "--no-store"])
        self.assertEqual(code, 0)
        self.assertIsNone(load_latest_pool(db_path=self.db))


class ReadCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "pool.sqlite3"

    def seed(self, built=None, written_at=None):
        PoolStore(self.db).save_report(built or report(), written_at=written_at)

    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = read_cli.main([*argv, "--db", str(self.db)])
        return code, buffer.getvalue()

    def test_tickers_format_is_one_symbol_per_line(self):
        self.seed()
        code, printed = self.run_cli(["--top", "3", "--format", "tickers"])
        self.assertEqual(code, 0)
        self.assertEqual(len(printed.strip().splitlines()), 3)

    def test_pool_file_format_feeds_reddit_prefetch(self):
        self.seed()
        _, printed = self.run_cli(["--top", "4", "--format", "pool-file"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pool.json"
            path.write_text(printed, encoding="utf-8")
            self.assertEqual(len(load_pool(path)), 4)

    def test_json_format_exposes_status_and_age(self):
        self.seed()
        _, printed = self.run_cli(["--format", "json", "--top", "2"])
        payload = json.loads(printed)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["degraded_sources"], [])
        self.assertIn("age_hours", payload)
        self.assertEqual(len(payload["pool"]), 2)

    def test_degraded_pool_is_served_but_flagged(self):
        self.seed(report(FakeClient(stocktwits=SourceFetchError("HTTP 429"))))
        code, printed = self.run_cli(["--format", "json"])
        payload = json.loads(printed)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["degraded_sources"], [STOCKTWITS_TRENDING])

    def test_stale_pool_exits_one_but_still_returns_the_list(self):
        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
        self.seed(written_at=old)
        code, printed = self.run_cli(["--format", "json", "--max-age-hours", "36"])
        payload = json.loads(printed)
        self.assertEqual(code, 1)
        self.assertTrue(payload["stale"])
        self.assertTrue(payload["pool"], "陈旧也要把池子交出来，由调用方决定")

    def test_missing_pool_exits_two(self):
        code, _ = self.run_cli(["--format", "json"])
        self.assertEqual(code, 2)

    def test_require_complete_without_a_clean_run_exits_two(self):
        self.seed(report(FakeClient(stocktwits=SourceFetchError("HTTP 429"))))
        code, _ = self.run_cli(["--format", "json", "--require-complete"])
        self.assertEqual(code, 2)

    def test_top_defaults_to_config_and_all_returns_everything(self):
        built = report()
        self.seed(built)
        _, default_out = self.run_cli(["--format", "tickers"])
        _, limited_out = self.run_cli(["--format", "tickers", "--top", "2"])
        _, all_out = self.run_cli(["--format", "tickers", "--all"])
        expected_default = min(load_config(env={}).top_n, len(built.considered))
        self.assertEqual(len(default_out.strip().splitlines()), expected_default)
        self.assertEqual(len(limited_out.strip().splitlines()), 2)
        self.assertEqual(len(all_out.strip().splitlines()), len(built.considered))


if __name__ == "__main__":
    unittest.main()
