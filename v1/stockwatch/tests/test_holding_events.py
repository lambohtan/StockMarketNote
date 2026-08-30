#!/usr/bin/env python3
"""持仓每日事件扫描测试。运行：python3 tests/test_holding_events.py

用户 2026-08-29 的要求：「每天都看一下全部股票哪只有重大新闻就行……
如果没什么事就别提醒了」。所以持仓的入池条件从「只看价格残差 >2σ」
扩展成四选一，且**都不命中就完全不出现** —— 沉默本身是产品的一部分。

四条触发全部用已抓的本地数据判定，不额外联网：财报日历从未入库
（`fundamentals` 表是空的，`run_ingest` 也不抓），所以「财报刚发布」
用已存的 8-K item 2.02 判，而不是调会联网的 `next_earnings()`。
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw.deepread import pool as P

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


class Cfg:
    def get(self, key, default=None):
        return default


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Store(tmp.name)


def seed_8k(st, ticker, filed_at, items):
    st.insert_ignore_many(
        "edgar_filings",
        ("accession", "filed_at", "ticker", "cik", "form", "items", "url",
         "raw_json", "seen_at"),
        [(f"{ticker}-{filed_at}-{items}", filed_at, ticker, "1", "8-K",
          items, "", "{}", "x")])


def seed_form4(st, ticker, filed_at, n):
    st.insert_ignore_many(
        "edgar_filings",
        ("accession", "filed_at", "ticker", "cik", "form", "items", "url",
         "raw_json", "seen_at"),
        [(f"{ticker}-f4-{i}", filed_at, ticker, "1", "4", "", "", "{}", "x")
         for i in range(n)])


D = "2026-08-29"


def test_quiet_holding_does_not_surface():
    """没事就别提醒 —— 用户明确要的行为，也是最容易被做丢的一条。"""
    st = fresh_store()
    check("平静持仓不出现", P.holding_events(st, D, ["AAPL"], attributions=[]), [])
    st.close()


def test_price_anomaly_triggers():
    st = fresh_store()
    out = P.holding_events(st, D, ["NVDA"], attributions=[
        {"ticker": "NVDA", "level": "extreme", "z": 3.2}])
    check("异动票进池", [x["ticker"] for x in out], ["NVDA"])
    check("原因写明是异动", "异动" in out[0]["reason"], True)
    st.close()


def test_l1_8k_triggers_without_price_move():
    """价格没动但 CEO 走了 —— 旧实现会漏掉，这正是要补的缺口。"""
    st = fresh_store()
    seed_8k(st, "AAPL", "2026-08-28", "5.02")
    out = P.holding_events(st, D, ["AAPL"], attributions=[])
    check("L1 级 8-K 进池", [x["ticker"] for x in out], ["AAPL"])
    check("原因点名事件类型", "高管" in out[0]["reason"], True)
    st.close()


def test_non_l1_8k_does_not_trigger():
    st = fresh_store()
    seed_8k(st, "AAPL", "2026-08-28", "8.01")   # 其他事件，不算重大
    check("普通 8-K 不进池", P.holding_events(st, D, ["AAPL"], attributions=[]), [])
    st.close()


def test_old_8k_outside_window_does_not_trigger():
    st = fresh_store()
    seed_8k(st, "AAPL", "2026-07-01", "5.02")
    check("窗口外的旧 8-K 不进池",
          P.holding_events(st, D, ["AAPL"], attributions=[]), [])
    st.close()


def test_earnings_release_triggers():
    """财报日历没入库，所以用已存的 8-K item 2.02 判「财报刚发布」。"""
    st = fresh_store()
    seed_8k(st, "MU", "2026-08-28", "2.02")
    out = P.holding_events(st, D, ["MU"], attributions=[])
    check("财报刚发布进池", [x["ticker"] for x in out], ["MU"])
    check("原因写明财报", "财报" in out[0]["reason"], True)
    st.close()


def test_insider_cluster_triggers():
    st = fresh_store()
    seed_form4(st, "AMD", "2026-08-20", 3)
    out = P.holding_events(st, D, ["AMD"], attributions=[])
    check("Form 4 集中进池", [x["ticker"] for x in out], ["AMD"])
    check("原因写明申报份数", "3 份" in out[0]["reason"], True)
    st.close()


def test_two_filings_not_enough():
    st = fresh_store()
    seed_form4(st, "AMD", "2026-08-20", 2)
    check("2 份不够", P.holding_events(st, D, ["AMD"], attributions=[]), [])
    st.close()


def test_only_scans_held_tickers():
    """扫描范围是持仓，不能把全市场的 8-K 也拉进来。"""
    st = fresh_store()
    seed_8k(st, "TSLA", "2026-08-28", "5.02")
    check("非持仓票不进持仓扫描",
          P.holding_events(st, D, ["AAPL"], attributions=[]), [])
    st.close()


def test_multiple_triggers_dedup_to_one_entry():
    st = fresh_store()
    seed_8k(st, "NVDA", "2026-08-28", "5.02")
    seed_form4(st, "NVDA", "2026-08-20", 4)
    out = P.holding_events(st, D, ["NVDA"], attributions=[
        {"ticker": "NVDA", "level": "extreme", "z": 3.2}])
    check("多重触发只出一条", len(out), 1)
    check("原因合并了多个触发", out[0]["reason"].count("·") >= 1, True)
    st.close()


def test_build_uses_holding_events():
    """build() 要接上事件扫描，且持仓不占新票名额。"""
    st = fresh_store()
    seed_8k(st, "AAPL", "2026-08-28", "5.02")
    st.insert_ignore_many(
        "reddit_rank", ("d", "source", "ticker", "rank", "mentions",
                        "upvotes", "rank_24h_ago", "mentions_24h_ago"),
        [(D, "all-stocks", f"T{i}", i + 1, 0, 0, 60 + i, 0) for i in range(10)])
    out = P.build(st, Cfg(), D, attributions=[], held_tickers=["AAPL"],
                  new_slots=8)
    picked = {x["ticker"]: x["source"] for x in out}
    check("持仓事件票进了池", picked.get("AAPL"), "holding_event")
    check("持仓不占新票名额", len(out), 9)      # 1 持仓 + 8 新票
    st.close()


for fn in (test_quiet_holding_does_not_surface, test_price_anomaly_triggers,
           test_l1_8k_triggers_without_price_move, test_non_l1_8k_does_not_trigger,
           test_old_8k_outside_window_does_not_trigger, test_earnings_release_triggers,
           test_insider_cluster_triggers, test_two_filings_not_enough,
           test_only_scans_held_tickers, test_multiple_triggers_dedup_to_one_entry,
           test_build_uses_holding_events):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
