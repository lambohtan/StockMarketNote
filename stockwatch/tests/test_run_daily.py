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


def seeded_store(anomaly=True):
    """造一个含 SPY / XLK / ABCD 的库。ABCD 最后一天注入 3σ 冲击。"""
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
        idio[-1] += 3.2 * sigma
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
    RD.emit(st, Cfg(), ctx, dry_run=False)
    check("仍然入队 daily", OB.has_kind_on(st, "daily", ctx["d"]), True)
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


if __name__ == "__main__":
    test_anomaly_produces_daily_entry()
    test_no_anomaly_still_produces_entry()
    test_rerun_is_idempotent()
    test_force_allows_rerun()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
