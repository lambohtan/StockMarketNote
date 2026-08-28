"""
SQLite 存储层。

核心原则（架构决策 4）：写入即快照，只追加不覆盖。
每天的原始数据带日期入库，永不删除历史 —— 这样半年后才有可能回测
"Reddit 信号到底有没有用"这类问题。ApeWisdom 不提供历史，今天不存就永远没有。
"""
import sqlite3, json, time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 持仓快照：每次上传 Fidelity CSV 存一份，永不覆盖历史
CREATE TABLE IF NOT EXISTS positions (
  snapshot_date TEXT, ticker TEXT, description TEXT, quantity REAL,
  last_price REAL, market_value REAL, cost_basis_total REAL,
  avg_cost REAL, total_gain REAL, asset_type TEXT, account TEXT,
  loaded_at TEXT,
  PRIMARY KEY (snapshot_date, account, ticker)
);

CREATE TABLE IF NOT EXISTS prices (
  d TEXT, ticker TEXT, close REAL, volume REAL,
  PRIMARY KEY (d, ticker)
);

CREATE TABLE IF NOT EXISTS meta (
  ticker TEXT PRIMARY KEY, sector TEXT, industry TEXT,
  name TEXT, market_cap REAL, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS fundamentals (
  d TEXT, ticker TEXT, payload_json TEXT,
  PRIMARY KEY (d, ticker)
);

-- Reddit 热度：只存不算，变化率在分析阶段算
CREATE TABLE IF NOT EXISTS reddit_rank (
  d TEXT, source TEXT, ticker TEXT, rank INTEGER, mentions INTEGER,
  upvotes INTEGER, rank_24h_ago INTEGER, mentions_24h_ago INTEGER,
  PRIMARY KEY (d, source, ticker)
);

CREATE TABLE IF NOT EXISTS edgar_filings (
  accession TEXT PRIMARY KEY, filed_at TEXT, ticker TEXT, cik TEXT,
  form TEXT, items TEXT, url TEXT, raw_json TEXT, seen_at TEXT
);

-- 信号触发记录 + 后续表现追踪（⭐ 让系统能自我评估的基础）
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  d TEXT, ticker TEXT, signal_type TEXT, score REAL, payload_json TEXT,
  UNIQUE(d, ticker, signal_type)
);
CREATE TABLE IF NOT EXISTS signal_outcomes (
  signal_id INTEGER, horizon_days INTEGER, return_pct REAL,
  bench_return_pct REAL, computed_at TEXT,
  PRIMARY KEY (signal_id, horizon_days)
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  d TEXT, ticker TEXT, level TEXT, category TEXT, body_md TEXT, pushed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reports (
  d TEXT, kind TEXT, body_md TEXT, created_at TEXT,
  PRIMARY KEY (d, kind)
);

CREATE TABLE IF NOT EXISTS llm_calls (
  ts TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, purpose TEXT
);

-- 数据源健康：yfinance 是非官方接口，静默失败比报错更危险
CREATE TABLE IF NOT EXISTS source_health (
  ts TEXT, source TEXT, ok INTEGER, latency_ms INTEGER, detail TEXT
);

CREATE INDEX IF NOT EXISTS ix_prices_ticker ON prices(ticker);
CREATE INDEX IF NOT EXISTS ix_reddit_ticker ON reddit_rank(ticker);
CREATE INDEX IF NOT EXISTS ix_filings_ticker ON edgar_filings(ticker, filed_at);
CREATE INDEX IF NOT EXISTS ix_signals_ticker ON signals(ticker, d);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- 写入 ----------
    def upsert_many(self, table, cols, rows):
        if not rows:
            return 0
        ph = ",".join("?" * len(cols))
        sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({ph})"
        with self.tx() as c:
            c.executemany(sql, rows)
        return len(rows)

    def log_health(self, source, ok, latency_ms, detail=""):
        with self.tx() as c:
            c.execute(
                "INSERT INTO source_health (ts,source,ok,latency_ms,detail) VALUES (?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), source,
                 1 if ok else 0, int(latency_ms), str(detail)[:500]))

    # ---------- 读取 ----------
    def q(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def latest_positions(self):
        r = self.conn.execute("SELECT MAX(snapshot_date) FROM positions").fetchone()[0]
        if not r:
            return []
        return self.q("SELECT * FROM positions WHERE snapshot_date=?", (r,))

    def price_history(self, tickers, start=None):
        if not tickers:
            return []
        ph = ",".join("?" * len(tickers))
        sql = f"SELECT d,ticker,close FROM prices WHERE ticker IN ({ph})"
        args = list(tickers)
        if start:
            sql += " AND d>=?"
            args.append(start)
        return self.q(sql + " ORDER BY d", args)

    def stats(self):
        out = {}
        for t in ["positions", "prices", "meta", "reddit_rank",
                  "edgar_filings", "signals", "alerts", "reports", "source_health"]:
            try:
                out[t] = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                out[t] = -1
        return out

    def close(self):
        self.conn.close()
