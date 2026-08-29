#!/usr/bin/env python3
"""主流程测试。运行：python3 tests/test_run_pool.py

锁四件事：
  - --dry-run 严格只读，不写库不发网络
  - --dry-run 的进程入口（main()）不联网、不调 LLM（裁定 1 的额外测试）
  - 单只票失败不拖垮其余票
  - 结果落 deepread_results，一天一票一行（P7 回看的数据基础）

修复轮补充（I4/I5/I6）：
  - 行业未映射到 ETF 的持仓仍进归因结果；单票归因异常不拖垮其余持仓
  - deepread_results.llm_hits 落库的是完整的 8 条 + 引用，不是只有 2 条文本判定
  - 入池但全部深读失败必须推送；今天没有票入池不需要推送
"""
import contextlib
import io
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw.analysis import portfolio as PF
import run_pool

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


class Cfg:
    def get(self, key, default=None):
        return {"benchmarks.market": "SPY", "llm.model": "claude-opus-5",
                "identity.sec_email": "a@b.com"}.get(key, default)


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    return Store(tmp.name)


def fake_item(ticker, **kw):
    base = {"ticker": ticker, "source": "apewisdom", "reason": "热度第 5 名",
            "label": "偏正面", "note": "", "hit": 6, "total": 8,
            "narrative": "发生了什么：略。", "disagreements": [],
            "text_hits": {}, "text_quotes": {}, "model": "claude-opus-5",
            "disclaimer": "标准未经回测验证。", "d": "2026-08-29",
            "facts": {"pe": None, "forward_pe": None, "rank": 5,
                      "rank_prev": 20, "rank_delta": 15, "missing": []},
            "py": {"hits": {}, "details": {}, "hit": 5, "total": 6}}
    base.update(kw)
    return base


def test_emit_writes_outbox_and_results():
    st = fresh_store()
    tmpdir = tempfile.mkdtemp()
    n = run_pool.emit(st, Cfg(), "2026-08-29",
                      [fake_item("NVDA"), fake_item("AMD")], report_dir=tmpdir)
    check("入队两条", n, 2)
    kinds = st.q("SELECT kind, COUNT(*) c FROM outbox GROUP BY kind")
    check("都是 pool 类型", [(r["kind"], r["c"]) for r in kinds], [("pool", 2)])
    rows = st.q("SELECT ticker, label FROM deepread_results ORDER BY ticker")
    check("结果落库", [r["ticker"] for r in rows], ["AMD", "NVDA"])
    check("报告落盘", (Path(tmpdir) / "pool_2026-08-29.md").exists(), True)
    st.close()


def test_emit_is_idempotent():
    st = fresh_store()
    tmpdir = tempfile.mkdtemp()
    run_pool.emit(st, Cfg(), "2026-08-29", [fake_item("NVDA")], report_dir=tmpdir)
    run_pool.emit(st, Cfg(), "2026-08-29", [fake_item("NVDA")], report_dir=tmpdir)
    n = st.q("SELECT COUNT(*) c FROM outbox")[0]["c"]
    check("重跑不重复入队", n, 1)
    st.close()


def test_dry_run_writes_nothing():
    st = fresh_store()
    tmpdir = tempfile.mkdtemp()
    run_pool.emit(st, Cfg(), "2026-08-29", [fake_item("NVDA")],
                  dry_run=True, report_dir=tmpdir)
    n = st.q("SELECT COUNT(*) c FROM outbox")[0]["c"]
    check("干跑不入队", n, 0)
    m = st.q("SELECT COUNT(*) c FROM deepread_results")[0]["c"]
    check("干跑不落结果", m, 0)
    check("干跑不写报告", (Path(tmpdir) / "pool_2026-08-29.md").exists(), False)
    st.close()


def test_one_ticker_failure_does_not_stop_others():
    st = fresh_store()
    def one_item(store, cfg, d, entry):
        if entry["ticker"] == "BAD":
            raise RuntimeError("这只票挂了")
        return fake_item(entry["ticker"])
    entries = [{"ticker": "BAD", "source": "apewisdom", "reason": "r", "strength": 1},
               {"ticker": "GOOD", "source": "apewisdom", "reason": "r", "strength": 1}]
    items = run_pool.build_items(st, Cfg(), "2026-08-29", entries, one_item=one_item)
    check("坏票被跳过", [i["ticker"] for i in items], ["GOOD"])
    health = st.q("SELECT source, ok FROM source_health")
    check("失败记进健康表", any(r["ok"] == 0 for r in health), True)
    st.close()


def test_dry_run_process_is_offline_and_read_only():
    """裁定 1：run_pool.py --dry-run 的进程入口必须严格离线只读。

    P3 的 run_daily.py --dry-run 曾经在「只读」的假象下，照样通过
    build_context 拉真实 EDGAR/新闻正文、调两次真实 LLM，被判为 Critical。
    这里用同一种手法验证 run_pool.py 没有重犯：把每一个可能触发网络/LLM
    的入口都换成"一调用就报错"的哨兵，dry-run 全程不应该碰到任何一个。
    """
    print("\nrun_pool --dry-run 进程入口：只读、不联网、不调 LLM")
    st = fresh_store()
    path = Path(st.path)
    st.close()
    before_bytes = path.read_bytes()

    class ProcessCfg(Cfg):
        db_path = str(path)

    calls = {"pool_build": 0, "edgar": 0, "news": 0, "stage1": 0, "stage2": 0}

    def fake_pool_build(store, cfg, d, attributions=None, new_slots=3):
        calls["pool_build"] += 1
        if not store.read_only:
            raise AssertionError("dry-run 必须使用只读 Store 构建池子")
        return [{"ticker": "NVDA", "source": "apewisdom", "reason": "热度上升",
                "strength": 1}]

    def forbidden_edgar(*a, **k):
        calls["edgar"] += 1
        raise AssertionError("dry-run 不得抓取 EDGAR 财报正文（网络）")

    def forbidden_news(*a, **k):
        calls["news"] += 1
        raise AssertionError("dry-run 不得抓取新闻正文（网络）")

    def forbidden_stage1(*a, **k):
        calls["stage1"] += 1
        raise AssertionError("dry-run 不得调用 LLM stage1")

    def forbidden_stage2(*a, **k):
        calls["stage2"] += 1
        raise AssertionError("dry-run 不得调用 LLM stage2")

    old = (run_pool.CFG, run_pool.PL.build, run_pool.FT.fetch, run_pool.NF.fetch,
           run_pool.DR.stage1, run_pool.DR.stage2)
    old_argv = sys.argv
    run_pool.CFG = ProcessCfg()
    run_pool.PL.build = fake_pool_build
    run_pool.FT.fetch = forbidden_edgar
    run_pool.NF.fetch = forbidden_news
    run_pool.DR.stage1 = forbidden_stage1
    run_pool.DR.stage2 = forbidden_stage2
    try:
        sys.argv = ["run_pool.py", "--dry-run", "--date", "2026-08-29"]
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run_pool.main()
    finally:
        (run_pool.CFG, run_pool.PL.build, run_pool.FT.fetch, run_pool.NF.fetch,
         run_pool.DR.stage1, run_pool.DR.stage2) = old
        sys.argv = old_argv

    check("dry-run 进程正常退出", rc, 0)
    check("池子按只读 Store 构建一次", calls["pool_build"], 1)
    check("不抓 EDGAR 财报正文", calls["edgar"], 0)
    check("不抓新闻正文", calls["news"], 0)
    check("不调 LLM stage1", calls["stage1"], 0)
    check("不调 LLM stage2", calls["stage2"], 0)
    check("dry-run 不改 DB 字节", path.read_bytes() == before_bytes, True)

    ro = Store.open_read_only(path)
    check("outbox 仍为空", ro.q("SELECT COUNT(*) c FROM outbox")[0]["c"], 0)
    check("deepread_results 仍为空",
          ro.q("SELECT COUNT(*) c FROM deepread_results")[0]["c"], 0)
    check("source_health 仍为空",
          ro.q("SELECT COUNT(*) c FROM source_health")[0]["c"], 0)
    ro.close()


def test_emit_persists_full_llm_hits_and_disagreements_detail():
    """I5：deepread_results.llm_hits 要落完整的 8 条判定 + 引用，
    disagreements_detail 要落分歧的具体内容，不能只存计数。

    这张表存在的唯一理由是"一年后回看当时是怎么判的"和 P7 信号有效性
    追踪——交叉验证的证据恰恰是最该留的那部分，只存 2 条文本判定等于
    把 6 条对照判定和全部引用永久丢弃。
    """
    import json as _json
    st = fresh_store()
    tmpdir = tempfile.mkdtemp()
    dis = [{"key": "gross_margin", "label": "毛利率环比未恶化", "py": True,
            "llm": False, "py_detail": "毛利率环比 -0.4pt",
            "llm_quote": "we expect gross margin to normalize"}]
    item = fake_item(
        "NVDA",
        llm_hits={"revenue_growth": True, "gross_margin": False},
        llm_quotes={"gross_margin": "we expect gross margin to normalize"},
        text_hits={"guidance_direction": False, "no_new_risk": True},
        text_quotes={"guidance_direction": "…"},
        disagreements=dis,
    )
    run_pool.emit(st, Cfg(), "2026-08-29", [item], report_dir=tmpdir)
    row = st.q("SELECT llm_hits, disagreements, disagreements_detail "
              "FROM deepread_results WHERE ticker='NVDA'")[0]
    stored = _json.loads(row["llm_hits"])
    check("落库的 llm_hits 含 6 条对照判定",
          stored.get("hits", {}).get("revenue_growth"), True)
    check("落库的 llm_hits 含引用",
          "normalize" in stored.get("quotes", {}).get("gross_margin", ""), True)
    check("落库的 llm_hits 含 2 条文本判定",
          stored.get("text_hits", {}).get("no_new_risk"), True)
    check("disagreements 仍是计数", row["disagreements"], 1)
    stored_dis = _json.loads(row["disagreements_detail"])
    check("disagreements_detail 存了分歧的具体内容",
          stored_dis[0]["llm_quote"], "we expect gross margin to normalize")
    st.close()


def test_attributions_includes_unmapped_sector_and_survives_single_failure():
    """I4：run_pool._attributions 要和 run_daily.build_context 的写法对齐。

    此前两处都反了：① 行业未映射到 ETF 时直接 `continue` 跳过这只持仓——
    但 AT.attribute 的 docstring 明说 sector_etf=None 会退化为单因子，
    不是不能算，跳过会让"行业未知的持仓即使极端异动也进不了深读池"，
    与 spec「持仓异动全读」矛盾；② try/except 包住整个循环，任意一只票
    抛异常就让整个函数返回 []，当天 holding_anomaly 这个来源整个消失。
    """
    st = fresh_store()
    holdings = [
        PF.Holding(ticker="NVDA", sector="Technology"),
        PF.Holding(ticker="WEIRD", sector=None),        # 行业未映射
        PF.Holding(ticker="BOOM", sector="Technology"),
    ]

    calls = []

    def fake_attribute(store, ticker, etf, market, window):
        calls.append((ticker, etf))
        if ticker == "BOOM":
            raise RuntimeError("归因挂了")
        return {"ticker": ticker, "etf": etf, "level": "anomaly", "z": 3.0}

    old = run_pool.AT.attribute
    run_pool.AT.attribute = fake_attribute
    try:
        out = run_pool._attributions(st, Cfg(), "2026-08-29", holdings)
    finally:
        run_pool.AT.attribute = old

    tickers = {r["ticker"] for r in out}
    check("行业未映射的持仓仍进结果（不是 continue 跳过）", "WEIRD" in tickers, True)
    check("行业未映射时 etf 传的是 None（退化为单因子，不是被跳过）",
          dict(calls)["WEIRD"], None)
    check("挂掉的那只不进结果，但不影响其余", "BOOM" in tickers, False)
    check("BOOM 之后的循环仍然继续（try/except 是逐票的）",
          "NVDA" in tickers, True)
    st.close()


def test_empty_pool_produces_no_alert():
    """I6：今天没有票入池是正常情况（没有触发入池条件的信号），不该推送。

    与"入池但全灭"区分开：entries 本身为空时 emit() 照常写一份「0 只」的
    报告，不需要额外的 kind='pool' 警报。
    """
    st = fresh_store()
    path = Path(st.path)
    st.close()
    tmpdir = tempfile.mkdtemp()

    def fake_pool_build(store, cfg, d, attributions=None, new_slots=3):
        return []

    class ProcessCfg(Cfg):
        db_path = str(path)

    old = (run_pool.CFG, run_pool.PL.build, run_pool.REPORTS_DIR)
    old_argv = sys.argv
    run_pool.CFG = ProcessCfg()
    run_pool.PL.build = fake_pool_build
    run_pool.REPORTS_DIR = Path(tmpdir)
    try:
        sys.argv = ["run_pool.py", "--date", "2026-08-29"]
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run_pool.main()
    finally:
        run_pool.CFG, run_pool.PL.build, run_pool.REPORTS_DIR = old
        sys.argv = old_argv

    check("main 正常退出", rc, 0)
    chk = Store(str(path))
    rows = chk.q("SELECT kind FROM outbox")
    check("空池子不推送任何东西", rows, [])
    check("仍然写了报告", (Path(tmpdir) / "pool_2026-08-29.md").exists(), True)
    chk.close()


def test_all_tickers_failed_triggers_pool_alert():
    """I6：入池 N 只但全部深读失败——必须推一条 kind='pool' 高优先级通知。

    notify.drain 的看门狗只认 kind='daily'，不会替 run_pool 兜底；
    spec §8：所有源失败要以 kind='pool' 推一条「今日股票池深读未产出」，
    沉默不等于没事。
    """
    st = fresh_store()
    path = Path(st.path)
    st.close()
    tmpdir = tempfile.mkdtemp()

    def fake_pool_build(store, cfg, d, attributions=None, new_slots=3):
        return [{"ticker": "BAD", "source": "apewisdom", "reason": "r",
                "strength": 1}]

    def failing_one_item(store, cfg, d, entry, held=None):
        raise RuntimeError("模拟深读失败")

    class ProcessCfg(Cfg):
        db_path = str(path)

    old = (run_pool.CFG, run_pool.PL.build, run_pool.one_item, run_pool.REPORTS_DIR)
    old_argv = sys.argv
    run_pool.CFG = ProcessCfg()
    run_pool.PL.build = fake_pool_build
    run_pool.one_item = failing_one_item
    run_pool.REPORTS_DIR = Path(tmpdir)
    try:
        sys.argv = ["run_pool.py", "--date", "2026-08-29"]
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run_pool.main()
    finally:
        (run_pool.CFG, run_pool.PL.build, run_pool.one_item,
         run_pool.REPORTS_DIR) = old
        sys.argv = old_argv

    check("main 正常退出", rc, 0)
    chk = Store(str(path))
    rows = chk.q("SELECT kind, priority FROM outbox")
    check("入队了一条高优先级 pool 警报",
          [(r["kind"], r["priority"]) for r in rows], [("pool", "high")])
    health = chk.q("SELECT ok FROM source_health WHERE source='run_pool'")
    check("失败留痕", any(r["ok"] == 0 for r in health), True)
    chk.close()


for fn in (test_emit_writes_outbox_and_results, test_emit_is_idempotent,
           test_dry_run_writes_nothing, test_one_ticker_failure_does_not_stop_others,
           test_dry_run_process_is_offline_and_read_only,
           test_emit_persists_full_llm_hits_and_disagreements_detail,
           test_attributions_includes_unmapped_sector_and_survives_single_failure,
           test_empty_pool_produces_no_alert,
           test_all_tickers_failed_triggers_pool_alert):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
