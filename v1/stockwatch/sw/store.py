"""
SQLite 存储层。

核心原则（架构决策 4）：写入即快照，只追加不覆盖。正常 Store 会执行
增量 schema migration；只读 Store 只打开已有数据库，不执行 WAL、建表或
迁移，供命令行 dry-run 使用。
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


# 只有正常 Store 初始化才执行这个脚本；read_only 路径完全不会触碰它。
SCHEMA = """
PRAGMA journal_mode=WAL;

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

CREATE TABLE IF NOT EXISTS reddit_rank (
  d TEXT, source TEXT, ticker TEXT, rank INTEGER, mentions INTEGER,
  upvotes INTEGER, rank_24h_ago INTEGER, mentions_24h_ago INTEGER,
  PRIMARY KEY (d, source, ticker)
);

CREATE TABLE IF NOT EXISTS edgar_filings (
  accession TEXT PRIMARY KEY, filed_at TEXT, ticker TEXT, cik TEXT,
  form TEXT, items TEXT, url TEXT, raw_json TEXT, seen_at TEXT
);

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
  d TEXT, ticker TEXT, level TEXT, category TEXT, body_md TEXT,
  pushed INTEGER DEFAULT 0,
  logical_date TEXT, event_key TEXT
);

CREATE TABLE IF NOT EXISTS reports (
  d TEXT, kind TEXT, body_md TEXT, created_at TEXT,
  logical_date TEXT,
  PRIMARY KEY (d, kind)
);

CREATE TABLE IF NOT EXISTS llm_calls (
  ts TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, purpose TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
  ts TEXT, source TEXT, ok INTEGER, latency_ms INTEGER, detail TEXT
);

-- 每个 logical_date 是一次可恢复的计算生命周期；completed 是最后写入的标记。
CREATE TABLE IF NOT EXISTS runs (
  logical_date TEXT PRIMARY KEY,
  market_date TEXT,
  status TEXT NOT NULL,
  started_at TEXT,
  grace_until TEXT,
  completed_at TEXT,
  attempt_count INTEGER DEFAULT 0,
  last_error TEXT
);

-- 推送队列：logical_date/event_key 做持久化幂等身份；claim lease 不占用 sent_at。
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT,
  logical_date TEXT,
  event_key TEXT,
  kind TEXT,
  priority TEXT,
  title TEXT,
  body TEXT,
  sent_at TEXT,
  claim_token TEXT,
  claimed_at TEXT,
  attempts INTEGER DEFAULT 0,
  last_error TEXT
);

-- P4：财报正文缓存。按 accession + section 唯一，读过的申报不再下载。
CREATE TABLE IF NOT EXISTS filing_texts (
  accession   TEXT NOT NULL,
  ticker      TEXT NOT NULL,
  form        TEXT NOT NULL,
  filed_at    TEXT NOT NULL,
  section     TEXT NOT NULL,
  content     TEXT NOT NULL,
  fetched_at  TEXT NOT NULL,
  PRIMARY KEY (accession, section)
);
CREATE INDEX IF NOT EXISTS idx_filing_texts_ticker ON filing_texts(ticker, filed_at);

-- P4：深读结果快照，只追加。带模型名，换模型时能区分是模型变了还是公司变了。
-- 这也是 P7 信号有效性追踪的数据基础，从第一天就要写。
CREATE TABLE IF NOT EXISTS deepread_results (
  d                     TEXT NOT NULL,
  ticker                TEXT NOT NULL,
  source                TEXT NOT NULL,
  py_hits               TEXT NOT NULL,
  llm_hits              TEXT NOT NULL,
  disagreements         INTEGER NOT NULL,
  disagreements_detail  TEXT NOT NULL DEFAULT '[]',
  score_hit             INTEGER NOT NULL,
  score_total           INTEGER NOT NULL,
  label                 TEXT NOT NULL,
  narrative             TEXT NOT NULL,
  model                 TEXT NOT NULL,
  created_at            TEXT NOT NULL,
  PRIMARY KEY (d, ticker)
);
"""


def _read_only_hint(path, exc):
    """把只读打开失败翻译成可行动的说明。

    最常见的原因不是「文件不存在」，而是**上一个写入进程留下了陈旧的 WAL
    边车文件**（`-wal` / `-shm`）：SQLite 的只读连接无法执行 WAL 恢复
    （恢复要写 `-shm`），于是抛出与「文件不存在」一模一样的
    `unable to open database file`，让人往错的方向查。

    这里只改报错文案，**不自动退回读写打开** —— 干跑的「严格只读、零副作用」
    是 P3/P4 两轮修复才立起来的不变式，为了省一次手工操作而破坏它，
    代价远大于收益。
    """
    p = Path(path)
    if not p.exists():
        return (f"数据库不存在：{p}\n"
                f"先跑一次 run_ingest.py 建库，或用 STOCKWATCH_DB 指向已有库。")
    stale = [s for s in (f"{p}-wal", f"{p}-shm") if Path(s).exists()]
    if stale:
        return (f"只读打开失败：{p}\n"
                f"原因：存在 WAL 边车文件 {', '.join(Path(s).name for s in stale)}，"
                f"很可能是上一个写入进程被中断留下的陈旧状态。\n"
                f"只读连接无法执行 WAL 恢复（恢复需要写 -shm），所以报了这个错。\n"
                f"处理：用读写方式打开一次让它自行恢复即可 ——\n"
                f"  python3 -c \"import sqlite3;sqlite3.connect('{p}').close()\"\n"
                f"（干跑不会自动这么做：那会破坏「严格只读」的保证。）\n"
                f"原始错误：{exc}")
    return f"只读打开失败：{p}\n原始错误：{exc}"


class Store:
    """数据库连接。

    ``read_only=True`` 只允许查询。它不创建父目录、不执行 schema script、
    不执行 journal/WAL pragma，因此可以作为严格 dry-run 的数据库入口。
    """

    def __init__(self, path, read_only=False):
        self.path = Path(path)
        self.read_only = bool(read_only)
        if self.read_only:
            # URI mode=ro 会在数据库不存在时直接失败，也不会创建空库。
            uri = f"file:{self.path.resolve()}?mode=ro"
            try:
                self.conn = sqlite3.connect(uri, uri=True)
            except sqlite3.OperationalError as exc:
                raise sqlite3.OperationalError(_read_only_hint(self.path, exc)) from exc
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        # P4 在 06:45 插进一个会跑几分钟的新写入者（run_pool），夹在 weekly
        # 06:30 与 retry 07:00 之间；sqlite3 默认 busy_timeout 是 0，撞锁
        # 立即抛 "database is locked"，不会等对方提交。30 秒足够盖过一次
        # 正常事务的写入时长，又不会让调用方无限期挂起。
        self.conn.execute("PRAGMA busy_timeout=30000")
        if not self.read_only:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()
        self._refresh_columns()

    @classmethod
    def open_read_only(cls, path):
        """打开已有数据库的只读连接，不触发迁移或写 pragma。"""
        return cls(path, read_only=True)

    def _refresh_columns(self):
        self._columns = {}
        for table in ("outbox", "alerts", "reports", "runs"):
            try:
                self._columns[table] = {
                    r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")
                }
            except sqlite3.Error:
                self._columns[table] = set()

    def has_column(self, table, column):
        return column in self._columns.get(table, set())

    def _migrate(self):
        """增量迁移旧 P3 数据库，保留所有旧行。"""
        migrations = {
            "outbox": [
                ("logical_date", "TEXT"),
                ("event_key", "TEXT"),
                ("claim_token", "TEXT"),
                ("claimed_at", "TEXT"),
            ],
            "alerts": [("logical_date", "TEXT"), ("event_key", "TEXT")],
            "reports": [("logical_date", "TEXT")],
            # I5 修复：老库（本分支早期 CREATE TABLE 落地过 deepread_results
            # 之后）不会自动补上后加的列——CREATE TABLE IF NOT EXISTS 遇到
            # 已存在的表直接跳过，跟 Task 2 那条 deferred minor 是同一个机制。
            "deepread_results": [("disagreements_detail", "TEXT")],
        }
        for table, cols in migrations.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

        # 旧行没有独立 logical_date，只能用历史 created_at/d 回填一次；新行
        # 的身份由显式列保存，不再依赖挂钟时间判断幂等。
        self.conn.execute(
            "UPDATE outbox SET logical_date=substr(created_at,1,10) "
            "WHERE logical_date IS NULL AND created_at IS NOT NULL")
        self.conn.execute(
            "UPDATE alerts SET logical_date=d WHERE logical_date IS NULL")
        self.conn.execute(
            "UPDATE reports SET logical_date=d WHERE logical_date IS NULL")

        # 旧行的兼容键只用于保留历史，不改变它们的发送状态。
        self.conn.execute(
            "UPDATE outbox SET event_key=kind || ':legacy:' || id "
            "WHERE event_key IS NULL")
        self.conn.execute(
            "UPDATE alerts SET event_key='alert:legacy:' || id "
            "WHERE event_key IS NULL")

        # 旧索引名沿用，但定义升级为显式 logical_date；只在列存在后建。
        self.conn.execute("DROP INDEX IF EXISTS ix_outbox_unsent")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_outbox_unsent "
            "ON outbox(sent_at, logical_date, created_at)")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_outbox_event "
            "ON outbox(logical_date, kind, event_key) WHERE event_key IS NOT NULL")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_event "
            "ON alerts(logical_date, event_key) WHERE event_key IS NOT NULL")

        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_prices_ticker ON prices(ticker)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_reddit_ticker ON reddit_rank(ticker)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_filings_ticker ON edgar_filings(ticker, filed_at)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_signals_ticker ON signals(ticker, d)")

    @contextmanager
    def tx(self):
        if self.read_only:
            raise sqlite3.OperationalError("只读 Store 不允许事务写入")
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- 写入 ----------
    def upsert_many(self, table, cols, rows):
        if self.read_only:
            raise sqlite3.OperationalError("只读 Store 不允许写入")
        if not rows:
            return 0
        ph = ",".join("?" * len(cols))
        sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({ph})"
        with self.tx() as c:
            c.executemany(sql, rows)
        return len(rows)

    def insert_ignore_many(self, table, cols, rows):
        """INSERT OR IGNORE，不覆盖已有 EDGAR 完整行。"""
        if self.read_only:
            raise sqlite3.OperationalError("只读 Store 不允许写入")
        if not rows:
            return 0
        ph = ",".join("?" * len(cols))
        sql = f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})"
        with self.tx() as c:
            c.executemany(sql, rows)
        return len(rows)

    def log_health(self, source, ok, latency_ms, detail=""):
        if self.read_only:
            raise sqlite3.OperationalError("只读 Store 不允许写入健康记录")
        with self.tx() as c:
            c.execute(
                "INSERT INTO source_health (ts,source,ok,latency_ms,detail) VALUES (?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), source,
                 1 if ok else 0, int(latency_ms), str(detail)[:500]))

    def begin_run(self, logical_date, market_date=None, grace_seconds=900, now=None,
                  force=False):
        """持久化一次计算开始，供 notify watchdog 识别 grace 窗口。"""
        if self.read_only:
            raise sqlite3.OperationalError("只读 Store 不允许创建 run")
        now = now or datetime.now()
        started = now.isoformat(timespec="seconds")
        grace = (now + timedelta(seconds=max(0, int(grace_seconds)))).isoformat(
            timespec="seconds")
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs(logical_date,market_date,status,started_at,grace_until,"
                "attempt_count) VALUES (?,?,?,?,?,1) "
                "ON CONFLICT(logical_date) DO UPDATE SET "
                "market_date=COALESCE(excluded.market_date,runs.market_date), "
                "status=CASE WHEN runs.status='completed' AND ?=0 THEN runs.status ELSE 'running' END, "
                "started_at=CASE WHEN runs.status='completed' AND ?=0 THEN runs.started_at ELSE excluded.started_at END, "
                "grace_until=CASE WHEN runs.status='completed' AND ?=0 THEN runs.grace_until ELSE excluded.grace_until END, "
                "attempt_count=CASE WHEN runs.status='completed' AND ?=0 THEN runs.attempt_count ELSE runs.attempt_count+1 END",
                (logical_date, market_date, "running", started, grace,
                 int(bool(force)), int(bool(force)), int(bool(force)), int(bool(force))))

    def run_info(self, logical_date):
        try:
            rows = self.q("SELECT * FROM runs WHERE logical_date=?", (logical_date,))
        except sqlite3.Error:
            # 只读打开尚未迁移的旧库时，watchdog 仍可读取 outbox 做兼容判断。
            return None
        return rows[0] if rows else None

    def complete_run(self, logical_date, market_date=None, conn=None):
        """写入最终 completed 标记；可在已有事务中调用。"""
        if self.read_only:
            raise sqlite3.OperationalError("只读 Store 不允许完成 run")
        now = datetime.now().isoformat(timespec="seconds")
        if conn is not None:
            conn.execute(
                "UPDATE runs SET status='completed', market_date=COALESCE(?,market_date), "
                "completed_at=?, last_error=NULL WHERE logical_date=?",
                (market_date, now, logical_date))
            return
        with self.tx() as c:
            self.complete_run(logical_date, market_date, conn=c)

    def mark_run_failed(self, logical_date, error):
        if self.read_only:
            raise sqlite3.OperationalError("只读 Store 不允许更新 run")
        with self.tx() as c:
            c.execute("UPDATE runs SET last_error=? WHERE logical_date=?",
                      (str(error)[:500], logical_date))

    # ---------- 读取 ----------
    def q(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def latest_positions(self):
        r = self.conn.execute("SELECT MAX(snapshot_date) FROM positions").fetchone()[0]
        if not r:
            return []
        return self.q("SELECT * FROM positions WHERE snapshot_date=?", (r,))

    def price_history(self, tickers, start=None):
        """读取指定股票的历史收盘价，保留旧分析模块的 Store API。"""
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
                  "edgar_filings", "signals", "alerts", "reports", "runs",
                  "outbox", "source_health"]:
            try:
                out[t] = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                out[t] = -1
        return out

    def close(self):
        self.conn.close()
