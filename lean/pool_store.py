"""Durable SQLite store for candidate stock pools.

The pool must outlive any single run: other applications read "the current
pool" at any time, while updates happen on whatever schedule the operator
chooses.  Each update writes one immutable snapshot in a single transaction,
then moves the ``latest`` pointer, so a reader never sees a half-written pool
and a failed update leaves the previous pool untouched.

Every ranked candidate of a run is stored, not just the configured Top N.
Reading applies the size limit, which means changing the pool size takes effect
on pools that already exist.

A degraded run (one source failed) is stored and does become the latest pool,
but it stays marked: ``status`` is ``degraded`` and ``degraded_sources`` names
what failed, so no reader can mistake a fetch failure for a quiet market.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from lean.pool import PoolReport

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "local-data" / "pool" / "pool.sqlite3"

STATUS_COMPLETE = "complete"
STATUS_DEGRADED = "degraded"

_LATEST = "latest_run_id"
_LATEST_COMPLETE = "latest_complete_run_id"


@dataclass(frozen=True)
class PoolEntry:
    rank: int
    ticker: str
    name: str
    market: str
    exchange: str
    country: str
    score: float
    sources: tuple[str, ...]
    reasons: tuple[str, ...]
    observed_at: str
    freshness: tuple[str, ...]
    source_health: Mapping[str, str]
    mentions: int | None
    momentum: float | None
    price: float | None
    market_cap: float | None


@dataclass(frozen=True)
class SourceRow:
    source: str
    health: str
    count: int
    note: str


@dataclass(frozen=True)
class ExclusionRow:
    ticker: str
    source: str
    reason: str


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    as_of: str
    analysis_date: str
    written_at: str
    status: str
    degraded_sources: tuple[str, ...]
    candidate_count: int


@dataclass(frozen=True)
class PoolSnapshot:
    run_id: str
    as_of: str
    analysis_date: str
    written_at: str
    status: str
    degraded_sources: tuple[str, ...]
    total_candidates: int
    entries: tuple[PoolEntry, ...]
    sources: tuple[SourceRow, ...]
    exclusions: tuple[ExclusionRow, ...]
    thresholds: Mapping[str, float]
    config: Mapping[str, object]

    @property
    def complete(self) -> bool:
        return self.status == STATUS_COMPLETE

    def age_hours(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        written = datetime.fromisoformat(self.written_at.replace("Z", "+00:00"))
        return (now - written).total_seconds() / 3600.0

    def is_stale(self, max_age_hours: float, now: datetime | None = None) -> bool:
        """Staleness is only ever reported, never repaired by fetching."""

        return max_age_hours > 0 and self.age_hours(now) > max_age_hours

    def tickers(self) -> list[str]:
        return [entry.ticker for entry in self.entries]

    def to_pool_payload(self, aliases: bool = True) -> dict:
        """Shape consumed by ``prefetch_reddit.py --pool-file``."""

        return {
            "as_of": self.as_of,
            "analysis_date": self.analysis_date,
            "status": self.status,
            "degraded_sources": list(self.degraded_sources),
            "tickers": [
                {"ticker": entry.ticker, "aliases": _alias_list(entry) if aliases else []}
                for entry in self.entries
            ],
        }


def _alias_list(entry: PoolEntry) -> list[str]:
    from lean.pool import RankedTicker, _aliases_for

    return _aliases_for(RankedTicker(
        ticker=entry.ticker, name=entry.name, market=entry.market, exchange=entry.exchange,
        country=entry.country, score=entry.score, sources=entry.sources,
        reasons=entry.reasons, observed_at=entry.observed_at, freshness=entry.freshness))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PoolStore:
    """Snapshot store; writers use this class, readers may use it read-only."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self, read_only: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        return connection

    @contextmanager
    def session(self, read_only: bool = False):
        connection = self.connect(read_only=read_only)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.session() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS pool_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pool_runs (
                    run_id TEXT PRIMARY KEY,
                    as_of TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    written_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    degraded_sources TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    configured_top_n INTEGER NOT NULL,
                    thresholds_json TEXT NOT NULL,
                    config_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pool_entries (
                    run_id TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    country TEXT NOT NULL,
                    score REAL NOT NULL,
                    sources_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    freshness_json TEXT NOT NULL,
                    source_health_json TEXT NOT NULL,
                    mentions INTEGER,
                    momentum REAL,
                    price REAL,
                    market_cap REAL,
                    PRIMARY KEY (run_id, ticker)
                );
                CREATE TABLE IF NOT EXISTS pool_sources (
                    run_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    health TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    note TEXT NOT NULL,
                    PRIMARY KEY (run_id, source)
                );
                CREATE TABLE IF NOT EXISTS pool_exclusions (
                    run_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    source TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pool_entries_rank
                    ON pool_entries (run_id, rank);
                CREATE INDEX IF NOT EXISTS pool_runs_written
                    ON pool_runs (written_at DESC);
            """)
            connection.execute(
                "INSERT OR IGNORE INTO pool_meta (key, value) VALUES ('schema_version', ?)",
                (str(self.SCHEMA_VERSION),))

    def save_report(self, report: PoolReport, config: Mapping[str, object] | None = None,
                    keep_runs: int | None = None, written_at: str | None = None) -> str:
        """Write one snapshot atomically and move the latest pointers."""

        run_id = uuid.uuid4().hex
        stamp = written_at or _utc_now()
        status = STATUS_COMPLETE if report.complete else STATUS_DEGRADED
        health = {outcome.source: outcome.health for outcome in report.outcomes}

        with self.session() as connection:
            connection.execute(
                """INSERT INTO pool_runs (run_id, as_of, analysis_date, written_at, status,
                                          degraded_sources, candidate_count, configured_top_n,
                                          thresholds_json, config_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, report.as_of, report.analysis_date, stamp, status,
                 json.dumps(list(report.degraded_sources)), len(report.considered),
                 report.top_n, json.dumps(dict(report.thresholds)),
                 json.dumps(dict(config or {}), default=str)))
            connection.executemany(
                """INSERT INTO pool_entries (run_id, rank, ticker, name, market, exchange,
                                             country, score, sources_json, reasons_json,
                                             observed_at, freshness_json, source_health_json,
                                             mentions, momentum, price, market_cap)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(run_id, rank, e.ticker, e.name, e.market, e.exchange, e.country, e.score,
                  json.dumps(list(e.sources)), json.dumps(list(e.reasons)), e.observed_at,
                  json.dumps(list(e.freshness)),
                  json.dumps({s: health.get(_unit(s), "unknown") for s in e.sources}),
                  e.mentions, e.momentum, e.price, e.market_cap)
                 for rank, e in enumerate(report.considered, start=1)])
            connection.executemany(
                """INSERT INTO pool_sources (run_id, source, health, count, note)
                   VALUES (?, ?, ?, ?, ?)""",
                [(run_id, o.source, o.health, o.count, o.note) for o in report.outcomes])
            connection.executemany(
                """INSERT INTO pool_exclusions (run_id, ticker, source, reason)
                   VALUES (?, ?, ?, ?)""",
                [(run_id, x.ticker, x.source, x.reason) for x in report.excluded])
            _set_meta(connection, _LATEST, run_id)
            if status == STATUS_COMPLETE:
                _set_meta(connection, _LATEST_COMPLETE, run_id)

        if keep_runs:
            self.prune(keep_runs)
        return run_id

    def runs(self, limit: int = 20) -> list[RunSummary]:
        with self.session(read_only=True) as connection:
            rows = connection.execute(
                """SELECT * FROM pool_runs ORDER BY written_at DESC, rowid DESC LIMIT ?""",
                (limit,)).fetchall()
        return [RunSummary(
            run_id=row["run_id"], as_of=row["as_of"], analysis_date=row["analysis_date"],
            written_at=row["written_at"], status=row["status"],
            degraded_sources=tuple(json.loads(row["degraded_sources"])),
            candidate_count=row["candidate_count"]) for row in rows]

    def prune(self, keep_runs: int) -> int:
        """Drop old snapshots, never the ones the latest pointers refer to."""

        with self.session() as connection:
            protected = {row["value"] for row in connection.execute(
                "SELECT value FROM pool_meta WHERE key IN (?, ?)",
                (_LATEST, _LATEST_COMPLETE)).fetchall()}
            ordered = [row["run_id"] for row in connection.execute(
                "SELECT run_id FROM pool_runs ORDER BY written_at DESC, rowid DESC").fetchall()]
            doomed = [r for r in ordered[max(0, keep_runs):] if r not in protected]
            for run_id in doomed:
                for table in ("pool_entries", "pool_sources", "pool_exclusions", "pool_runs"):
                    connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        return len(doomed)

    def latest(self, limit: int | None = None,
               require_complete: bool = False) -> PoolSnapshot | None:
        with self.session(read_only=True) as connection:
            key = _LATEST_COMPLETE if require_complete else _LATEST
            row = connection.execute(
                "SELECT value FROM pool_meta WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            return _read_snapshot(connection, row["value"], limit)


def _unit(source: str) -> str:
    from lean.pool import _fetch_unit

    return _fetch_unit(source)


def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """INSERT INTO pool_meta (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (key, value))


def _read_snapshot(connection: sqlite3.Connection, run_id: str,
                   limit: int | None) -> PoolSnapshot | None:
    run = connection.execute("SELECT * FROM pool_runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        return None
    query = "SELECT * FROM pool_entries WHERE run_id = ? ORDER BY rank"
    params: tuple = (run_id,)
    if limit is not None:
        query += " LIMIT ?"
        params = (run_id, limit)
    entries = tuple(PoolEntry(
        rank=row["rank"], ticker=row["ticker"], name=row["name"], market=row["market"],
        exchange=row["exchange"], country=row["country"], score=row["score"],
        sources=tuple(json.loads(row["sources_json"])),
        reasons=tuple(json.loads(row["reasons_json"])),
        observed_at=row["observed_at"], freshness=tuple(json.loads(row["freshness_json"])),
        source_health=json.loads(row["source_health_json"]), mentions=row["mentions"],
        momentum=row["momentum"], price=row["price"], market_cap=row["market_cap"],
    ) for row in connection.execute(query, params).fetchall())
    sources = tuple(SourceRow(row["source"], row["health"], row["count"], row["note"])
                    for row in connection.execute(
                        "SELECT * FROM pool_sources WHERE run_id = ? ORDER BY source",
                        (run_id,)).fetchall())
    exclusions = tuple(ExclusionRow(row["ticker"], row["source"], row["reason"])
                       for row in connection.execute(
                           "SELECT * FROM pool_exclusions WHERE run_id = ? ORDER BY ticker",
                           (run_id,)).fetchall())
    return PoolSnapshot(
        run_id=run["run_id"], as_of=run["as_of"], analysis_date=run["analysis_date"],
        written_at=run["written_at"], status=run["status"],
        degraded_sources=tuple(json.loads(run["degraded_sources"])),
        total_candidates=run["candidate_count"], entries=entries, sources=sources,
        exclusions=exclusions, thresholds=json.loads(run["thresholds_json"]),
        config=json.loads(run["config_json"]))


def load_latest_pool(limit: int | None = None, *, require_complete: bool = False,
                     db_path: Path | str | None = None) -> PoolSnapshot | None:
    """Read the current pool.  Never fetches, never writes, never creates the DB.

    Returns ``None`` when no pool has been built yet (or, with
    ``require_complete``, when no run had every source healthy).  Callers must
    treat ``None`` as "no pool", never as "no interesting stocks today".
    """

    path = Path(db_path or DEFAULT_DB_PATH)
    if not path.is_file():
        return None
    store = PoolStore.__new__(PoolStore)
    store.path = path
    return store.latest(limit=limit, require_complete=require_complete)
