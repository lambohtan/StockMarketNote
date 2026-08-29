#!/usr/bin/env python3
"""主流程测试。运行：python3 tests/test_run_daily.py

用合成价格建一个临时库，跑完整流程，验证：
  - 产出 daily 条目进 outbox
  - 已经成功过的当天再跑不重复入队（07:00 重试任务会依赖这个）
  - 过了推送点时自己 drain（睡过头竞态的堵法）
"""
import sys, tempfile, math
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from sw.store import Store
from sw import outbox as OB
import run_daily as RD

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def seeded_store(anomaly=True, shock=3.2):
    """造一个含 SPY / XLK / ABCD 的库。ABCD 最后一天注入 shock×σ 冲击（默认 +3.2σ）。

    shock 支持负数、支持超过 4.0——修复轮 1 之前的默认值 +3.2σ 只够到
    L2（`sw/alerts.py` 的 L1 判据是「z ≤ -4.0」或「8-K 命中 L1 item」，
    正向异动、且 |z|<4 永远够不到 L1），导致 L1 单独入队那条分支
    （`run_daily.py::emit()` 里 priority="urgent" 的独立 outbox 记录）
    从来没有测试真正跑到过。这里把注入冲击的大小和方向都开放出来，供
    L1 场景按需构造。
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    st = Store(tmp.name)
    rng = np.random.default_rng(5)
    n = 120
    days = []
    d = date(2026, 3, 2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)

    mkt = rng.normal(0, 0.009, n)
    sec = rng.normal(0, 0.007, n)
    sigma = 0.004
    idio = rng.normal(0, sigma, n)
    if anomaly:
        idio[-1] += shock * sigma
    tgt = 1.2 * mkt + 0.7 * sec + idio

    def to_rows(tk, rets):
        px, rows = 100.0, []
        for dd, r in zip(days, rets):
            px *= (1 + r)
            rows.append((dd, tk, px, 1e6))
        return rows

    rows = to_rows("SPY", mkt) + to_rows("XLK", sec) + to_rows("ABCD", tgt)
    st.upsert_many("prices", ["d", "ticker", "close", "volume"], rows)
    st.upsert_many("meta", ["ticker", "sector", "industry", "name",
                            "market_cap", "updated_at"],
                   [("ABCD", "Technology", "Semis", "ABCD Inc", 1e11, "2026-08-27")])
    st.upsert_many("positions",
                   ["snapshot_date", "ticker", "description", "quantity",
                    "last_price", "market_value", "cost_basis_total", "avg_cost",
                    "total_gain", "asset_type", "account", "loaded_at"],
                   [(days[-1], "ABCD", "ABCD Inc", 10, 100.0, 1000.0, 800.0,
                     80.0, 200.0, "equity", "TEST", days[-1])])
    return st, days[-1]


class Cfg:
    def __init__(self):
        self._d = {
            "benchmarks.market": "SPY",
            "benchmarks.sectors": {"Technology": "XLK"},
            "llm.provider": "claude_cli",
            "portfolio.core_tickers": [],
            "portfolio.exclude_tickers": [],
            "schedule.push_time": "08:00",
        }
    def get(self, k, d=None):
        return self._d.get(k, d)
    ntfy_url = "https://ntfy.sh/test"
    ntfy_topic = "test"


def test_anomaly_produces_daily_entry():
    print("\n有异动时产出 daily 条目")
    st, last_d = seeded_store(anomaly=True)
    ctx = RD.build_context(st, Cfg(), snapshot_date=last_d, use_llm=False)
    check("检出 1 只异动", len([a for a in ctx["attributions"]
                              if a["level"] != "normal"]), 1)
    n = RD.emit(st, Cfg(), ctx, dry_run=False)
    check("入队 1 条 daily", OB.has_kind_on(st, "daily", ctx["d"]), True)
    st.close()


def test_no_anomaly_still_produces_entry():
    print("\n无异动也要产出条目 —— 否则 notify 会误报失败")
    st, last_d = seeded_store(anomaly=False)
    ctx = RD.build_context(st, Cfg(), snapshot_date=last_d, use_llm=False)
    check("没有异动", [a for a in ctx["attributions"] if a["level"] != "normal"], [])
    # 正常持仓不该被送去找原因——EDGAR/新闻查询和后续 LLM 摘要都要消耗
    # 额度，只有异动才值得查。
    check("正常持仓没有被送去找原因", ctx["causes_by_ticker"], {})
    RD.emit(st, Cfg(), ctx, dry_run=False)
    check("仍然入队 daily", OB.has_kind_on(st, "daily", ctx["d"]), True)
    st.close()


def test_build_context_collects_watchlist_events_fail_soft():
    """build_context 使用 WL.collect，并在观察池源异常时降级为空列表。"""
    print("\nbuild_context 应接入观察池汇总，且观察池失败时不中断日报")
    st, last_d = seeded_store(anomaly=False)
    calls = []
    original_collect = RD.WL.collect

    def fake_collect(store, d, held):
        calls.append((store, d, held))
        return ["合成观察池事件"]

    try:
        RD.WL.collect = fake_collect
        ctx = RD.build_context(st, Cfg(), snapshot_date=last_d, use_llm=False)
        check("watchlist_events 来自 WL.collect",
              ctx["watchlist_events"], ["合成观察池事件"])
        check("WL.collect 收到当前日期和持仓 ticker",
              (calls[0][1], calls[0][2]) if calls else None,
              (last_d, {"ABCD"}))

        def failing_collect(*args, **kwargs):
            raise RuntimeError("synthetic watchlist failure")

        RD.WL.collect = failing_collect
        ctx_failed = RD.build_context(st, Cfg(), snapshot_date=last_d,
                                      use_llm=False)
        check("观察池异常时返回空列表",
              ctx_failed["watchlist_events"], [])
    finally:
        RD.WL.collect = original_collect
        st.close()


def test_rerun_is_idempotent():
    print("\n当天重跑不重复入队（07:00 重试任务依赖这个）")
    st, last_d = seeded_store()
    ctx = RD.build_context(st, Cfg(), snapshot_date=last_d, use_llm=False)
    RD.emit(st, Cfg(), ctx, dry_run=False)
    RD.emit(st, Cfg(), ctx, dry_run=False)
    n = len(st.q("SELECT id FROM outbox WHERE kind='daily'"))
    check("只有 1 条 daily", n, 1)
    st.close()


def test_force_allows_rerun():
    print("\n--force 可以强制重跑")
    st, last_d = seeded_store()
    ctx = RD.build_context(st, Cfg(), snapshot_date=last_d, use_llm=False)
    RD.emit(st, Cfg(), ctx, dry_run=False)
    RD.emit(st, Cfg(), ctx, dry_run=False, force=True)
    n = len(st.q("SELECT id FROM outbox WHERE kind='daily'"))
    check("有 2 条 daily", n, 2)
    st.close()


def test_main_drains_when_past_push_time():
    """
    睡过头竞态的堵法：main() 跑完发现已经过了推送点，要自己 drain 一次，
    不能干等 08:00 那个独立任务——Mac 从 06:00 睡到 08:30 才醒的话，
    06:00 这次运行本身就已经在推送点之后了。

    不依赖真实挂钟时间是否已经过了 08:00（测试可能在一天里任何时刻跑）：
    把 CFG 的 schedule.push_time 临时改成 "00:00"，任何 "HH:MM" 字符串
    都 >= "00:00"，确保这条分支必进。同时把 sw.notify.drain 换成假实现，
    只记录有没有被调用——真实 CFG 指向真实 ntfy topic，测试环境绝不能
    真的发一条推送出去。全程用 STOCKWATCH_DB 指向临时库，不碰
    data/stockwatch.db。
    """
    print("\nmain() 跑完发现已过推送点，应该自己 drain 一次（同一个 notify.drain，不是复制逻辑）")
    import os, sys as _sys

    st, last_d = seeded_store(anomaly=False)
    db_path = str(st.path)
    st.close()

    orig_env = os.environ.get("STOCKWATCH_DB")
    orig_argv = _sys.argv
    push_cfg = RD.CFG._d.setdefault("schedule", {})
    had_push_time = "push_time" in push_cfg
    orig_push_time = push_cfg.get("push_time")
    orig_drain = RD.NT.drain
    calls = []

    def fake_drain(store, cfg, *a, **kw):
        calls.append((store, cfg))
        return {"sent": 0, "failed": 0, "failure_reported": False}

    try:
        os.environ["STOCKWATCH_DB"] = db_path
        _sys.argv = ["run_daily.py", "--skip-ingest"]
        push_cfg["push_time"] = "00:00"
        RD.NT.drain = fake_drain
        rc = RD.main()
    finally:
        if orig_env is not None:
            os.environ["STOCKWATCH_DB"] = orig_env
        else:
            os.environ.pop("STOCKWATCH_DB", None)
        _sys.argv = orig_argv
        if had_push_time:
            push_cfg["push_time"] = orig_push_time
        else:
            push_cfg.pop("push_time", None)
        RD.NT.drain = orig_drain

    check("main() 正常退出（exit code 0）", rc, 0)
    check("过了推送点，main() 自己调用了一次 drain", len(calls), 1)


def test_l1_alert_enqueued_on_extreme_negative_move():
    """
    L1 判据 1：残差 z ≤ -4.0（不依赖 8-K）。

    修复轮 1 · Important 1：emit() 里「L1 单独入队为 priority=urgent 独立
    通知」这段此前没有任何测试覆盖——把整段删掉，原有 12 项断言照样全绿。
    根因是 seeded_store 默认只注入 +3.2σ（正向、且够不到 4.0 门槛），
    L1 从来没在测试里真正触发过。这里用 shock=-5.5 构造一次确定能过
    -4.0 门槛的负向极端冲击（实测 z≈-4.68，留了足够裕量，不是卡在边界上）。
    """
    print("\n残差 z ≤ -4.0 时，L1 提醒应作为独立 outbox 条目入队（priority=urgent）")
    st, last_d = seeded_store(shock=-5.5)
    ctx = RD.build_context(st, Cfg(), snapshot_date=last_d, use_llm=False)
    l1_alerts = [a for a in ctx["alerts"] if a["level"] == "L1"]
    check("build_context 产出至少 1 条 L1 提醒", len(l1_alerts) >= 1, True)

    RD.emit(st, Cfg(), ctx, dry_run=False)
    l1_rows = st.q("SELECT id, kind, priority FROM outbox WHERE kind='l1'")
    daily_rows = st.q("SELECT id, kind FROM outbox WHERE kind='daily'")
    check("入队 1 条独立的 l1 记录", len(l1_rows), 1)
    check("l1 记录 priority=urgent", l1_rows[0]["priority"] if l1_rows else None, "urgent")
    check("daily 记录仍然只有 1 条", len(daily_rows), 1)
    # daily 和 l1 是 outbox 里两条不同 id 的独立记录，不是拼在一起的一条。
    check("daily 与 l1 是两条不同 id 的独立记录",
          bool(daily_rows) and bool(l1_rows) and daily_rows[0]["id"] != l1_rows[0]["id"],
          True)
    st.close()


def test_l1_alert_enqueued_on_8k_l1_item():
    """
    L1 判据 2：8-K 命中 L1 item（如 5.02 高管变动），不依赖 z 阈值。

    普通的 +3.2σ 异动（level="anomaly"，够不到 z≤-4.0）配合一条落在
    异动日回溯窗口内、items 含 5.02 的 8-K 申报，也应该触发 L1——
    scan() 里 `if l1_causes: level = "L1"` 这条判据完全不看 z。
    """
    print("\n8-K 命中 L1 item 时，L1 提醒也应该作为独立 outbox 条目入队")
    st, last_d = seeded_store()  # 默认 anomaly=True, shock=3.2 —— level=anomaly 即可
    st.upsert_many(
        "edgar_filings",
        ["accession", "filed_at", "ticker", "cik", "form", "items",
         "url", "raw_json", "seen_at"],
        [("acc-test-502", last_d, "ABCD", "0000000000", "8-K", "5.02",
          "https://sec.gov/acc-test-502", "{}", last_d)])

    ctx = RD.build_context(st, Cfg(), snapshot_date=last_d, use_llm=False)
    l1_alerts = [a for a in ctx["alerts"] if a["level"] == "L1"]
    check("8-K 5.02 触发至少 1 条 L1 提醒", len(l1_alerts) >= 1, True)

    RD.emit(st, Cfg(), ctx, dry_run=False)
    l1_rows = st.q("SELECT id, kind, priority FROM outbox WHERE kind='l1'")
    daily_rows = st.q("SELECT id, kind FROM outbox WHERE kind='daily'")
    check("入队 1 条独立的 l1 记录", len(l1_rows), 1)
    check("l1 记录 priority=urgent", l1_rows[0]["priority"] if l1_rows else None, "urgent")
    check("daily 与 l1 是两条不同 id 的独立记录",
          bool(daily_rows) and bool(l1_rows) and daily_rows[0]["id"] != l1_rows[0]["id"],
          True)
    st.close()


if __name__ == "__main__":
    test_anomaly_produces_daily_entry()
    test_no_anomaly_still_produces_entry()
    test_build_context_collects_watchlist_events_fail_soft()
    test_rerun_is_idempotent()
    test_force_allows_rerun()
    test_main_drains_when_past_push_time()
    test_l1_alert_enqueued_on_extreme_negative_move()
    test_l1_alert_enqueued_on_8k_l1_item()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
