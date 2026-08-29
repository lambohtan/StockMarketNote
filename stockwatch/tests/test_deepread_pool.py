#!/usr/bin/env python3
"""池子构建测试。运行：python3 tests/test_deepread_pool.py

用户 2026-08-29 决定：pool 收 ApeWisdom 前 20，**含前 10**。
日报那边的 watchlist.py 继续排除前 10，两者不是一回事，这里锁死差异。
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw.deepread import pool as P
from sw.analysis import watchlist as WL

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
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    return Store(tmp.name)


def seed_reddit(st, d, rows):
    st.insert_ignore_many(
        "reddit_rank", ("d", "source", "ticker", "rank", "mentions",
                        "upvotes", "rank_24h_ago", "mentions_24h_ago"),
        [(d, "all-stocks", tk, rank, 0, 0, prev, 0) for tk, rank, prev in rows])


def test_top20_includes_top10():
    st = fresh_store()
    seed_reddit(st, "2026-08-29", [("AAA", 1, 3), ("BBB", 12, 50), ("CCC", 40, 60)])
    got = [x["ticker"] for x in P.reddit_top(st, "2026-08-29", n=20)]
    check("第 1 名进池", "AAA" in got, True)
    check("第 12 名进池", "BBB" in got, True)
    check("第 40 名不进池", "CCC" in got, False)
    st.close()


def test_watchlist_still_excludes_top10():
    """回归：日报那边的老规矩不能被这次改动带偏。

    注：BBB 的 prev 用 51 而非 50 —— watchlist.JUMP_FROM=50 要求
    prev > 50（严格大于），prev=50 正好踩在阈值边界上不会命中，
    这是 brief 固件的边界值笔误，这里按 watchlist.py 的真实语义订正。
    """
    st = fresh_store()
    seed_reddit(st, "2026-08-29", [("AAA", 1, 3), ("BBB", 12, 51)])
    got = [x["ticker"] for x in WL.reddit_jumps(st, "2026-08-29")]
    check("日报仍排除前 10", "AAA" in got, False)
    check("日报仍收跃升票", "BBB" in got, True)
    st.close()


def test_empty_ticker_filtered():
    st = fresh_store()
    seed_reddit(st, "2026-08-29", [("", 2, 30), ("  ", 3, 30), ("DDD", 4, 30)])
    got = [x["ticker"] for x in P.reddit_top(st, "2026-08-29")]
    check("空代码被过滤", got, ["DDD"])
    st.close()


def test_holdings_anomaly_all_selected():
    st = fresh_store()
    # level 取值来自 attribution.classify()：normal / anomaly / extreme
    attributions = [
        {"ticker": "NVDA", "level": "extreme", "z": 3.2},
        {"ticker": "AMD", "level": "anomaly", "z": 2.4},
        {"ticker": "AAPL", "level": "normal", "z": 0.3},
    ]
    out = P.build(st, Cfg(), "2026-08-29", attributions=attributions)
    picked = {x["ticker"]: x["source"] for x in out}
    check("extreme 进池", picked.get("NVDA"), "holding_anomaly")
    check("anomaly 也进池", picked.get("AMD"), "holding_anomaly")
    check("正常票不进", "AAPL" in picked, False)
    st.close()


def test_new_ticker_slots_capped():
    st = fresh_store()
    seed_reddit(st, "2026-08-29",
                [(f"T{i}", i + 1, 40 + i) for i in range(10)])
    out = P.build(st, Cfg(), "2026-08-29", attributions=[], new_slots=3)
    check("新票名额封顶 3", len(out), 3)
    st.close()


def test_anomaly_not_counted_against_new_slots():
    st = fresh_store()
    seed_reddit(st, "2026-08-29", [(f"T{i}", i + 1, 40 + i) for i in range(10)])
    attributions = [{"ticker": "NVDA", "level": "extreme", "z": 3.2}]
    out = P.build(st, Cfg(), "2026-08-29", attributions=attributions, new_slots=3)
    check("异动 1 只 + 新票 3 只 = 4", len(out), 4)
    st.close()


def test_dedup_prefers_holding_anomaly():
    """同一只票既是持仓异动又上了热榜，只留一条，且归因原因优先。"""
    st = fresh_store()
    seed_reddit(st, "2026-08-29", [("NVDA", 5, 40)])
    out = P.build(st, Cfg(), "2026-08-29",
                  attributions=[{"ticker": "NVDA", "level": "extreme", "z": 3.2}])
    check("只留一条", len(out), 1)
    check("原因取持仓异动", out[0]["source"], "holding_anomaly")
    st.close()


for fn in (test_top20_includes_top10, test_watchlist_still_excludes_top10,
           test_empty_ticker_filtered, test_holdings_anomaly_all_selected,
           test_new_ticker_slots_capped, test_anomaly_not_counted_against_new_slots,
           test_dedup_prefers_holding_anomaly):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
