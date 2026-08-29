#!/usr/bin/env python3
"""P3 final fix wave 的聚焦、可直接运行验收测试。"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sw.store import Store
from sw import alerts as AL
from sw import daily_report as DR
from sw import llm as LM
from sw import notify as NT
from sw import outbox as OB
from sw import schedule as SC
from sw.analysis import causes as CS
from sw.analysis import watchlist as WL
from sw.sources.base import SourceResult
from sw.sources import edgar_src as E
from sw.sources import news as News
import run_daily as RD
import run_notify as RN

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: {got!r} / {want!r}")
    if not ok:
        FAIL.append(name)


def fresh_store():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Store(f.name)


class Cfg:
    ntfy_url = "https://example.invalid/fake-topic"

    def __init__(self, **values):
        self.values = {"schedule.failure_grace_seconds": 900,
                       "llm.provider": "claude_cli",
                       "llm.claude_bin": "/fake/claude"}
        self.values.update(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


def _empty_ctx(logical, market):
    return {"d": market, "market_date": market, "logical_date": logical,
            "attributions": [], "causes_by_ticker": {}, "alerts": [],
            "watchlist_events": [], "market": {}, "portfolio_weights": {},
            "n_holdings": 0}


def test_logical_date_and_boundary():
    print("\nlogical_date 与 market_date 分离、跨周末和时区边界")
    st = fresh_store()
    ctx = _empty_ctx("2026-08-31", "2026-08-28")  # 周一调度、周五市场数据
    RD.emit(st, Cfg(), ctx)
    row = st.q("SELECT logical_date,created_at FROM outbox WHERE kind='daily'")[0]
    check("日报 outbox 保留逻辑日", row["logical_date"], "2026-08-31")
    check("created_at 与逻辑身份分列持久化", "created_at" in row and row["logical_date"] == "2026-08-31", True)
    report = st.q("SELECT d,logical_date FROM reports WHERE kind='daily'")[0]
    check("报告 d 是市场日", report["d"], "2026-08-28")
    check("报告持久化逻辑日", report["logical_date"], "2026-08-31")
    check("临时 DB 的报告落在临时目录",
          (st.path.parent / "reports" / "daily_2026-08-28.md").exists(), True)

    real_send = NT.send
    NT.send = lambda *a, **k: (True, "fake")
    try:
        result = NT.drain(st, Cfg(), logical_date="2026-08-31",
                          now=datetime(2026, 8, 31, 8, 0))
    finally:
        NT.send = real_send
    check("市场日是周五但逻辑日已完成，不误报 failure", result["failure_reported"], False)
    check("通知只发送日报", result["sent"], 1)

    weekend = fresh_store()
    RD.emit(weekend, Cfg(), _empty_ctx("2026-08-30", "2026-08-28"))
    real_send = NT.send
    NT.send = lambda *a, **k: (True, "fake")
    try:
        weekend_result = NT.drain(weekend, Cfg(), logical_date="2026-08-30",
                                  now=datetime(2026, 8, 30, 8, 0))
    finally:
        NT.send = real_send
    check("周末逻辑日沿用已完成的周五日报", weekend_result["failure_reported"], False)
    weekend.close()

    la = ZoneInfo("America/Los_Angeles")
    check("本机 23:30 的逻辑日", RD.local_logical_date(datetime(2026, 8, 28, 23, 30, tzinfo=la)),
          "2026-08-28")
    check("本机午夜后的逻辑日", RD.local_logical_date(datetime(2026, 8, 29, 0, 30, tzinfo=la)),
          "2026-08-29")
    et = ZoneInfo("America/New_York")
    check("ET 午夜仍按本机前一日归属", RD.local_logical_date(
        datetime(2026, 8, 29, 0, 30, tzinfo=et)), "2026-08-28")
    st.close()


def test_process_dry_run_is_read_only():
    print("\nrun_daily --dry-run 进程入口：只读、无 LLM/ntfy/写入")
    st = fresh_store()
    OB.enqueue(st, "daily", "合成标题", "合成正文 -1.0%",
               logical_date="2026-08-31", created_at="2026-08-28T06:00:00")
    path = st.path
    st.close()
    before_bytes = path.read_bytes()
    report_dir = path.parent / "reports"
    before_reports = sorted(report_dir.glob("**/*")) if report_dir.exists() else []

    class ProcessCfg(Cfg):
        db_path = str(path)
        ntfy_url = "https://example.invalid/process-fake"

    old_cfg, old_build, old_llm, old_drain = RD.CFG, RD.build_context, RD.LM.summarize, RD.NT.drain
    old_argv = sys.argv
    calls = {"llm": 0, "ntfy": 0}

    def forbidden_llm(*args, **kwargs):
        calls["llm"] += 1
        raise AssertionError("dry-run 不得调用 LLM")

    def forbidden_drain(*args, **kwargs):
        calls["ntfy"] += 1
        raise AssertionError("dry-run 不得调用 ntfy drain")

    def fake_context(store, cfg, **kwargs):
        if not store.read_only:
            raise AssertionError("dry-run 必须使用 read-only Store")
        return _empty_ctx("2026-08-31", "2026-08-28")

    RD.CFG = ProcessCfg()
    RD.build_context = fake_context
    RD.LM.summarize = forbidden_llm
    RD.NT.drain = forbidden_drain
    try:
        sys.argv = ["run_daily.py", "--dry-run", "--skip-ingest"]
        rc = RD.main()
    finally:
        RD.CFG, RD.build_context, RD.LM.summarize, RD.NT.drain = (
            old_cfg, old_build, old_llm, old_drain)
        sys.argv = old_argv

    after_reports = sorted(report_dir.glob("**/*")) if report_dir.exists() else []
    check("dry-run 进程正常退出", rc, 0)
    check("进程入口未调 LLM/ntfy", calls, {"llm": 0, "ntfy": 0})
    check("进程入口不改 DB 字节", path.read_bytes(), before_bytes)
    check("进程入口不改报告路径", after_reports, before_reports)

    old_notify_cfg, old_notify_drain = RN.CFG, RN.NT.drain
    notify_calls = []

    def fake_notify_drain(store, cfg, **kwargs):
        notify_calls.append((store.read_only, kwargs.get("dry_run")))
        return {"sent": 1, "failed": 0, "failure_reported": False}

    RN.CFG = ProcessCfg()
    RN.NT.drain = fake_notify_drain
    try:
        sys.argv = ["run_notify.py", "--dry-run"]
        notify_rc = RN.main()
    finally:
        RN.CFG, RN.NT.drain = old_notify_cfg, old_notify_drain
        sys.argv = old_argv
    check("run_notify dry-run 进程正常退出", notify_rc, 0)
    check("run_notify 使用只读 Store", notify_calls, [(True, True)])
    check("run_notify dry-run 不改 DB 字节", path.read_bytes(), before_bytes)


def test_read_only_open_and_notify_preview():
    print("\n只读 Store 与 notify dry-run 不写数据库")
    st = fresh_store()
    OB.enqueue(st, "daily", "标题", "正文 -1.0%",
               logical_date="2026-08-31", created_at="2026-08-28T06:00:00")
    path = st.path
    before_tables = st.q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    st.close()
    before = path.read_bytes()
    ro = Store.open_read_only(path)
    check("read-only 标记", ro.read_only, True)
    check("只读读取旧条目", len(ro.q("SELECT * FROM outbox")), 1)
    try:
        ro.log_health("x", False, 0, "x")
    except sqlite3.OperationalError:
        write_blocked = True
    else:
        write_blocked = False
    check("只读拒绝 health 写入", write_blocked, True)
    with contextlib.redirect_stdout(io.StringIO()):
        result = NT.drain(ro, Cfg(), dry_run=True, logical_date="2026-08-31")
    check("dry-run 预览发送计数", result["sent"], 1)
    ro.close()
    check("只读/preview 后数据库字节不变", path.read_bytes(), before)
    reopened = Store.open_read_only(path)
    check("表集合不变", reopened.q(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"), before_tables)
    reopened.close()


def test_prechange_schema_migration_preserves_rows():
    print("\n旧版 schema 增量迁移：保留 pending/sent 行与历史日期")
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    path = Path(f.name)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE outbox (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, kind TEXT,
          priority TEXT, title TEXT, body TEXT, sent_at TEXT,
          attempts INTEGER DEFAULT 0, last_error TEXT
        );
        CREATE TABLE alerts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, d TEXT, ticker TEXT, level TEXT,
          category TEXT, body_md TEXT, pushed INTEGER DEFAULT 0
        );
        CREATE TABLE reports (
          d TEXT, kind TEXT, body_md TEXT, created_at TEXT,
          PRIMARY KEY (d, kind)
        );
        INSERT INTO outbox(created_at,kind,priority,title,body,sent_at,attempts)
          VALUES ('2026-08-28T06:00:00','daily','default','旧日报','旧正文',NULL,0);
        INSERT INTO outbox(created_at,kind,priority,title,body,sent_at,attempts)
          VALUES ('2026-08-27T06:00:00','daily','default','旧已发','旧正文','2026-08-27T08:00:00',1);
        INSERT INTO alerts(d,ticker,level,category,body_md)
          VALUES ('2026-08-28','SYN','L1','旧类目','旧提醒');
        INSERT INTO reports(d,kind,body_md,created_at)
          VALUES ('2026-08-28','daily','旧报告','2026-08-28T06:00:00');
    """)
    conn.commit()
    conn.close()
    st = Store(path)
    migrated = st.q("SELECT * FROM outbox ORDER BY id")
    pending, sent = migrated[0], migrated[1]
    check("迁移保留 pending 行", pending["sent_at"], None)
    check("迁移保留 sent 行", sent["sent_at"], "2026-08-27T08:00:00")
    check("outbox 历史逻辑日回填", pending["logical_date"], "2026-08-28")
    check("旧 outbox 得到兼容 event key", pending["event_key"].startswith("daily:legacy:"), True)
    alert = st.q("SELECT logical_date,event_key FROM alerts")[0]
    check("alerts 历史逻辑日回填", alert["logical_date"], "2026-08-28")
    check("reports 历史逻辑日回填", st.q("SELECT logical_date FROM reports")[0]["logical_date"], "2026-08-28")
    check("迁移后 lease 列可用", st.has_column("outbox", "claim_token"), True)
    st.close()


def test_lease_recovery_and_owner_safety():
    print("\nclaim lease：模拟死亡、过期恢复和 owner-safe 失败记录")
    st = fresh_store()
    ident = OB.enqueue(st, "daily", "T", "B", logical_date="2026-08-31")
    first = OB.claim(st, ident, now=datetime(2026, 8, 31, 8, 0), lease_seconds=60)
    row = st.q("SELECT sent_at,claim_token FROM outbox WHERE id=?", (ident,))[0]
    check("claim 不预写 sent_at", row["sent_at"], None)
    check("claim 写入独立令牌", row["claim_token"], first)
    check("错误 token 不能 mark_sent", OB.mark_sent(st, ident, token="wrong"), False)
    check("过期前不能二次 claim", OB.claim(st, ident, now=datetime(2026, 8, 31, 8, 0, 30), lease_seconds=60), None)
    recovered = OB.claim(st, ident, now=datetime(2026, 8, 31, 8, 2), lease_seconds=60)
    check("进程死亡后 lease 可恢复", recovered is not None, True)
    check("旧 token 不能 release 新 owner", OB.release(st, ident, first), False)
    check("新 owner 成功确认后才写 sent_at", OB.mark_sent(st, ident, ts="2026-08-31T08:03:00", token=recovered), True)
    check("确认后 sent_at 落库", st.q("SELECT sent_at FROM outbox WHERE id=?", (ident,))[0]["sent_at"], "2026-08-31T08:03:00")
    st.close()


def test_atomic_emit_rollback():
    print("\n日报/L1/alerts/completed 同事务，模拟 L1 阶段崩溃回滚")
    st = fresh_store()
    alert = {"ticker": "ZXQ", "level": "L1", "category": "价格异动",
             "facts": "发生了客观变化", "data": "当日 -5.0%",
             "base_rate": "单日残差本身不预示方向", "counterpoint": "未检索到反面材料",
             "position": "—", "next_steps": ["继任公告时间", "下一季财报日期", "审计师变化"]}
    ctx = _empty_ctx("2026-08-31", "2026-08-28")
    ctx["alerts"] = [alert]
    original = RD._insert_outbox
    calls = [0]

    def explode_after_daily(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("synthetic crash between daily and L1")
        return original(*args, **kwargs)

    RD._insert_outbox = explode_after_daily
    try:
        try:
            RD.emit(st, Cfg(), ctx)
        except RuntimeError:
            raised = True
        else:
            raised = False
    finally:
        RD._insert_outbox = original
    check("注入崩溃确实触发", raised, True)
    check("daily/outbox 全部回滚", st.q("SELECT * FROM outbox"), [])
    check("reports 回滚", st.q("SELECT * FROM reports"), [])
    check("alerts 回滚", st.q("SELECT * FROM alerts"), [])
    check("没有 completed marker", st.q("SELECT * FROM runs"), [])
    st.close()


def test_normal_holding_l1_without_llm():
    print("\n正常价格归因 + 本地 8-K L1 独立路径")
    st = fresh_store()

    class Holding:
        ticker = "ZXQ"
        sector = "Technology"

    class Portfolio:
        holdings = [Holding()]
        snapshot_date = "2026-08-28"

        def weights(self):
            return {"ZXQ": 1.0}

        def get(self, ticker):
            return self.holdings[0] if ticker == "ZXQ" else None

    old_build, old_attr = RD.PF.build, RD.AT.attribute
    old_l1, old_causes, old_llm = CS.find_l1_causes, CS.find_causes, LM.summarize
    calls = {"full": 0, "llm": 0}
    RD.PF.build = lambda *a, **k: Portfolio()
    RD.AT.attribute = lambda *a, **k: {"ticker": "ZXQ", "d": "2026-08-28",
                                       "ret": 0.0, "mkt_part": 0.0, "sector_part": 0.0,
                                       "idio": 0.0, "z": 0.0, "level": "normal"}
    RD.CS.find_l1_causes = lambda *a, **k: CS.CauseList([
        {"source": "8-K", "summary": "董事变动", "item": "5.02", "is_l1": True,
         "url": ""}], health=[], coverage={})

    def unexpected_full(*a, **k):
        calls["full"] += 1
        return []

    def unexpected_llm(*a, **k):
        calls["llm"] += 1
        return "不应调用"

    RD.CS.find_causes = unexpected_full
    LM.summarize = unexpected_llm
    try:
        ctx = RD.build_context(st, Cfg(), logical_date="2026-08-31", use_llm=True,
                               persist_health=False)
    finally:
        RD.PF.build, RD.AT.attribute = old_build, old_attr
        RD.CS.find_l1_causes, RD.CS.find_causes, LM.summarize = old_l1, old_causes, old_llm
    check("正常票保留本地 L1 cause", ctx["causes_by_ticker"]["ZXQ"][0]["item"], "5.02")
    check("normal+8-K 生成 L1", ctx["alerts"][0]["level"], "L1")
    check("正常票不进完整 causes", calls["full"], 0)
    check("正常票不调 LLM", calls["llm"], 0)
    st.close()


def test_llm_structured_guard_and_runtime_health():
    print("\nLLM schema、数字 provenance、runtime/guard health")
    cfg = Cfg()
    store = fresh_store()
    calls = []

    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"structured_output": {"summary": "变动 2.5%"}})

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Completed()

    old_run = LM.subprocess.run
    LM.subprocess.run = fake_run
    try:
        valid = LM.summarize(cfg, "材料：变动 2.5%", store=store)
    finally:
        LM.subprocess.run = old_run
    check("CLI 结构化结果统一为 summary", valid, "变动 2.5%")
    cmd = calls[0]
    check("CLI 带 --json-schema", "--json-schema" in cmd, True)
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])
    check("schema 要求 summary", schema["required"], ["summary"])

    class BadCompleted:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"structured_output": {"summary": "变动 99.9%"}})

    LM.subprocess.run = lambda *a, **k: BadCompleted()
    try:
        bad = LM.summarize(cfg, "材料：变动 2.5%", store=store)
    finally:
        LM.subprocess.run = old_run
    check("未溯源数字被拒绝", bad, "")
    health = store.q("SELECT source,ok,detail FROM source_health ORDER BY rowid")
    check("guard health 不含材料", all("材料" not in h["detail"] for h in health), True)
    check("guard health 已记录", any(h["source"] == "llm.guard" and h["ok"] == 0 for h in health), True)

    class Fails:
        returncode = 7
        stderr = "secret should not be logged"
        stdout = ""

    LM.subprocess.run = lambda *a, **k: Fails()
    try:
        runtime = LM.summarize(cfg, "PRIVATE_MATERIAL 2.5%", store=store)
    finally:
        LM.subprocess.run = old_run
    check("CLI runtime failure fail-soft", runtime, "")
    health = store.q("SELECT source,ok,detail FROM source_health ORDER BY rowid")
    check("runtime health 已记录", any(h["source"] == "llm.runtime" and h["ok"] == 0 for h in health), True)
    check("runtime health 无 secret/material", all("PRIVATE_MATERIAL" not in h["detail"] and "secret" not in h["detail"] for h in health), True)
    store.close()


def test_api_structured_boundary_and_runtime_health():
    print("\nAPI 结构化边界与异常 health（全程 fake）")
    store = fresh_store()
    cfg = Cfg(**{"llm.provider": "api"})

    class Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class Messages:
        def __init__(self, text=None, error=None):
            self.text = text
            self.error = error

        def create(self, **kwargs):
            if self.error:
                raise RuntimeError(self.error)
            return SimpleNamespace(content=[Block(self.text)])

    class Client:
        messages = Messages(json.dumps({"summary": "变动 2.5%"}))

    old_anthropic = sys.modules.get("anthropic")
    old_key = os.environ.get("ANTHROPIC_API_KEY")
    sys.modules["anthropic"] = SimpleNamespace(Anthropic=lambda: Client())
    os.environ["ANTHROPIC_API_KEY"] = "synthetic-api-key"
    try:
        valid = LM.summarize(cfg, "材料：变动 2.5%", store=store)
        check("API JSON 与 CLI 统一为 summary", valid, "变动 2.5%")
        Client.messages = Messages("普通纯文本 2.5%")
        plain = LM.summarize(cfg, "材料：变动 2.5%", store=store)
        check("API 非结构化纯文本被拒绝", plain, "")
        Client.messages = Messages(error="synthetic-api-secret")
        failed = LM.summarize(cfg, "PRIVATE_MATERIAL 2.5%", store=store)
        check("API 异常 fail-soft", failed, "")
    finally:
        if old_anthropic is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = old_anthropic
        if old_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = old_key
    health = store.q("SELECT source,detail FROM source_health ORDER BY rowid")
    check("API runtime health 已记录", any(h["source"] == "llm.runtime" for h in health), True)
    check("API health 不含 secret/material", all(
        "synthetic-api-secret" not in h["detail"] and "PRIVATE_MATERIAL" not in h["detail"]
        for h in health), True)
    store.close()


def test_partial_health_and_report_wording():
    print("\n部分信源健康与 unavailable/no-cause 文案")
    st = fresh_store()
    old_yfinance = sys.modules.get("yfinance")

    class FakeTicker:
        news = [
            {"content": {"title": "合成新闻", "canonicalUrl": {"url": "https://example.invalid/n"}}},
            {"content": {"canonicalUrl": {"url": "https://example.invalid/missing"}}},
        ]

    sys.modules["yfinance"] = SimpleNamespace(Ticker=lambda ticker: FakeTicker())
    try:
        adapter_result = News.fetch_news("ZXQ", max_items=2)
    finally:
        if old_yfinance is None:
            sys.modules.pop("yfinance", None)
        else:
            sys.modules["yfinance"] = old_yfinance
    check("news adapter 保留成功行", adapter_result.rows, 1)
    check("news adapter 标记缺行 partial", adapter_result.ok, False)

    old_news = CS.N.fetch_news
    old_earnings = __import__("sw.sources.prices", fromlist=["fetch_earnings"]).fetch_earnings
    CS.N.fetch_news = lambda *a, **k: SourceResult(
        source="yfinance.news", ok=False, rows=1, detail="部分新闻解析失败",
        data=[{"summary": "客观新闻", "url": ""}])
    import sw.sources.prices as Prices
    Prices.fetch_earnings = lambda *a, **k: SourceResult(
        source="yfinance.earnings", ok=False, rows=0, detail="Timeout", data=[])
    try:
        causes = CS.find_causes(st, "ZXQ", "2026-08-28", max_news=2)
    finally:
        CS.N.fetch_news = old_news
        Prices.fetch_earnings = old_earnings
    check("部分新闻成功行仍可用", causes[0]["summary"], "客观新闻")
    check("news partial 可见", any(h["source"] == "yfinance.news" and not h["ok"] for h in causes.health), True)
    check("earnings failure 可见", any(h["source"] == "yfinance.earnings" and not h["ok"] for h in causes.health), True)
    empty = CS.CauseList([], health=[{"source": "news", "ok": False}], coverage={})
    ctx = _empty_ctx("2026-08-28", "2026-08-28")
    ctx["attributions"] = [{"ticker": "ZXQ", "level": "anomaly", "ret": .1,
                             "mkt_part": 0., "sector_part": 0., "idio": .1,
                             "z": 2.5}]
    ctx["causes_by_ticker"] = {"ZXQ": empty}
    text = DR.render_markdown(ctx)
    check("报告区分 unavailable 与 no cause", "部分信源不可用" in text and "不可用不等于没有原因" in text, True)
    st.close()


def test_edgar_mapping_paging_and_form4_window():
    print("\nEDGAR CIK 映射/完整扫描、cap degraded、Form4 三日窗口")
    old_ready = E._ready
    E._ready = True
    import edgar as edgar_module
    old_get = edgar_module.get_filings
    E.CIK_TICKER_MAP["0000000001"] = "ZXQ"
    rows = [SimpleNamespace(accession_no=f"a{i}", filing_date="2026-08-28",
                             items="", cik="1", filing_url="", company="X")
            for i in range(405)]
    rows.append(SimpleNamespace(accession_no="stale", filing_date="2026-08-20",
                                items="", cik="1", filing_url="", company="X"))
    edgar_module.get_filings = lambda form=None: rows
    try:
        result = E.fetch_filings("fake@example.invalid", forms=("4",), days_back=3,
                                 limit=400, as_of="2026-08-28")
    finally:
        edgar_module.get_filings = old_get
        E._ready = old_ready
        E.CIK_TICKER_MAP.pop("0000000001", None)
    check("完整迭代保留窗口内 405 条", len(result.data), 405)
    check("CIK 映射 ticker", result.data[0][2], "ZXQ")
    check("窗口外数据排除", all(row[0] != "stale" for row in result.data), True)
    check("完整扫描不因旧 limit 假降级", result.ok, True)

    old_company = edgar_module.Company

    class HeadOnly:
        def head(self, n):
            return rows[:n]

    class FakeCompany:
        def __init__(self, ticker):
            self.ticker = ticker

        def get_filings(self, form=None):
            return HeadOnly()

    edgar_module.Company = FakeCompany
    try:
        capped = E.fetch_filings_for_tickers("fake@example.invalid", ["ZXQ"],
                                             forms=("4",), limit_per=2,
                                             as_of="2026-08-28")
    finally:
        edgar_module.Company = old_company
    check("按票 cap 截断标为 degraded", capped.ok, False)
    check("按票 cap 保留已取得行", len(capped.data), 2)
    check("按票 cap health 含计数", "扫描 2 条" in capped.detail and "窗口内 2 条" in capped.detail, True)

    st = fresh_store()
    cols = ["accession", "filed_at", "ticker", "cik", "form", "items", "url", "raw_json", "seen_at"]
    st.upsert_many("edgar_filings", cols, [
        ("inside-a", "2026-08-26", "ZXQ", "1", "4", "", "", "{}", ""),
        ("inside-b", "2026-08-28", "ZXQ", "1", "4", "", "", "{}", ""),
        ("outside", "2026-08-25", "ZXQ", "1", "4", "", "", "{}", ""),
    ])
    cluster = WL.insider_filing_clusters(st, "2026-08-28", days=3)
    check("Form4 窗口含 d-2", cluster, [{"ticker": "ZXQ", "filings": 2}])
    st.close()


def test_policy_final_send_and_schedule():
    print("\n最终 send policy 与 API schedule fail-closed/rollback")
    cfg = Cfg()
    old_post = NT.requests.post
    posted = []
    NT.requests.post = lambda *a, **k: posted.append((a, k)) or SimpleNamespace(ok=True, status_code=200, text="")
    try:
        ok, _ = NT.send(cfg, "日报", "事实 -2.0%", kind="daily")
        check("daily final send 成功", ok, True)
        try:
            NT.send(cfg, "失败", "请立即处理", kind="failure")
        except ValueError:
            directive_blocked = True
        else:
            directive_blocked = False
        check("failure final directive 被拦", directive_blocked, True)
        try:
            NT.send(cfg, "L1", "金额 €2,000", kind="l1")
        except ValueError:
            amount_blocked = True
        else:
            amount_blocked = False
        check("L1 Unicode currency 被拦", amount_blocked, True)
        for sample in ("金额 2,000€", "金额 人民币 5,000", "金额 美元两千万"):
            try:
                NT.send(cfg, "金额测试", sample, kind="daily")
            except ValueError:
                blocked = True
            else:
                blocked = False
            check(f"金额写法被拦: {sample}", blocked, True)
    finally:
        NT.requests.post = old_post
    check("策略拦截不调用网络", len(posted), 1)

    class ApiCfg(Cfg):
        def __init__(self):
            super().__init__(**{"llm.provider": "api"})

    old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name in ("run_daily.py", "run_weekly.py", "run_notify.py"):
            (root / name).write_text("# fake\n", encoding="utf-8")
        SC.LAUNCH_AGENTS = root / "agents"
        try:
            try:
                SC.apply(ApiCfg(), "/fake/python", str(root), skip_missing=False)
            except ValueError:
                missing_rejected = True
            else:
                missing_rejected = False
            check("API 缺 key 在 mutation 前拒绝", missing_rejected, True)
            check("缺 key 无 plist", list(SC.LAUNCH_AGENTS.glob("*")) if SC.LAUNCH_AGENTS.exists() else [], [])
            check("缺 key 不创建 LaunchAgents/logs", SC.LAUNCH_AGENTS.exists() or (root / "logs").exists(), False)
            os.environ["ANTHROPIC_API_KEY"] = "fake-key"
            SC.LAUNCH_AGENTS.mkdir(parents=True)
            old_daily = SC.LAUNCH_AGENTS / f"{SC.PREFIX}-compute-daily.plist"
            old_daily.write_bytes(b"synthetic-old-plist")
            os.chmod(old_daily, 0o640)
            calls = []

            class R:
                def __init__(self, code):
                    self.returncode = code
                    self.stderr = "bootstrap failed"

            def fake_run(argv, **kwargs):
                calls.append(argv)
                # 旧 daily plist 存在，但 print 返回非零表示原服务未加载；
                # 回滚只恢复文件，不应额外 bootstrap 一个原本未加载的 job。
                return R(1 if argv[1] in ("bootstrap", "print") else 0)

            old_run = SC.subprocess.run
            SC.subprocess.run = fake_run
            try:
                done = SC.apply(ApiCfg(), "/fake/python", str(root), skip_missing=False)
            finally:
                SC.subprocess.run = old_run
            check("bootstrap 失败不进 done", done, [])
            check("失败尝试四项 bootstrap", sum(x[1] == "bootstrap" for x in calls), 4)
            check("bootstrap 失败恢复旧 plist", old_daily.read_bytes(), b"synthetic-old-plist")
            check("bootstrap 失败恢复旧权限", old_daily.stat().st_mode & 0o777, 0o640)

            def successful_run(argv, **kwargs):
                calls.append(argv)
                return R(0)

            SC.subprocess.run = successful_run
            try:
                installed = SC.apply(ApiCfg(), "/fake/python", str(root), skip_missing=False)
            finally:
                SC.subprocess.run = old_run
            check("API key 存在时四项安装成功", len(installed), 4)
            modes = [((SC.LAUNCH_AGENTS / f"{label}.plist").stat().st_mode & 0o777)
                     for label in installed]
            check("含 secret 的 plist 全部 0600", modes, [0o600] * 4)
        finally:
            SC.LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
    if old_key is not None:
        os.environ["ANTHROPIC_API_KEY"] = old_key


if __name__ == "__main__":
    test_logical_date_and_boundary()
    test_process_dry_run_is_read_only()
    test_read_only_open_and_notify_preview()
    test_prechange_schema_migration_preserves_rows()
    test_lease_recovery_and_owner_safety()
    test_atomic_emit_rollback()
    test_normal_holding_l1_without_llm()
    test_llm_structured_guard_and_runtime_health()
    test_api_structured_boundary_and_runtime_health()
    test_partial_health_and_report_wording()
    test_edgar_mapping_paging_and_form4_window()
    test_policy_final_send_and_schedule()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
