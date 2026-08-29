#!/usr/bin/env python3
"""观察池增量事件的测试。运行：python3 tests/test_watchlist.py

规则来自 HANDOFF「三个信号源的正确用法」，这里逐条锁死：
  - Reddit 绝对排名前 10 的**直接排除** —— 那时候你是接盘方
  - 有价值的是变化率：从 50 名开外冲进前 20
  - Form 4 只认「多个申报人集中买入」，单笔忽略
"""
import sys, tempfile, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw.analysis import watchlist as WL

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    return Store(tmp.name)


def seed_reddit(st, d, rows):
    """rows: [(ticker, rank, rank_24h_ago)]"""
    st.upsert_many("reddit_rank",
                   ["d", "source", "ticker", "rank", "mentions", "upvotes",
                    "rank_24h_ago", "mentions_24h_ago"],
                   [(d, "all-stocks", t, r, 100, 50, r24, 40)
                    for t, r, r24 in rows])


def test_reddit_jump_detected():
    print("\nReddit 排名跃升")
    st = fresh_store()
    seed_reddit(st, "2026-08-27", [
        ("JUMPY", 12, 78),     # 78 → 12：从 50 名开外冲进前 20 ✓
        ("STEADY", 25, 27),    # 几乎没动 ✗
        ("SLOW", 45, 60),      # 进步了但没进前 20 ✗
    ])
    got = {x["ticker"] for x in WL.reddit_jumps(st, "2026-08-27")}
    check("只有 JUMPY 入选", got, {"JUMPY"})
    st.close()


def test_reddit_top10_excluded():
    print("\n绝对排名前 10 直接排除（那时候你是接盘方）")
    st = fresh_store()
    seed_reddit(st, "2026-08-27", [
        ("HOT", 3, 80),        # 冲得猛，但已经是第 3 名 → 排除
        ("OK", 15, 70),        # 进前 20 但不在前 10 → 保留
    ])
    got = {x["ticker"] for x in WL.reddit_jumps(st, "2026-08-27")}
    check("HOT 被排除", "HOT" in got, False)
    check("OK 保留", "OK" in got, True)
    st.close()


def test_reddit_missing_prior_rank_is_skipped():
    print("\n没有前值时不猜")
    st = fresh_store()
    seed_reddit(st, "2026-08-27", [("NEW", 15, None)])
    check("跳过", WL.reddit_jumps(st, "2026-08-27"), [])
    st.close()


def test_insider_cluster_needs_multiple_filers():
    print("\nForm 4：单笔忽略，多个申报人才算 cluster")
    st = fresh_store()
    rows = []
    # ABCD 三个不同 accession（视作三个申报）→ 算 cluster
    for i in range(3):
        rows.append((f"acc-a{i}", "2026-08-26", "ABCD", "111", "4", "",
                     "u", json.dumps({"company": "ABCD"}), "2026-08-27"))
    # WXYZ 只有一笔 → 忽略
    rows.append(("acc-w0", "2026-08-26", "WXYZ", "222", "4", "",
                 "u", json.dumps({"company": "WXYZ"}), "2026-08-27"))
    st.upsert_many("edgar_filings",
                   ["accession", "filed_at", "ticker", "cik", "form",
                    "items", "url", "raw_json", "seen_at"], rows)
    got = {x["ticker"] for x in WL.insider_buy_clusters(st, "2026-08-27",
                                                        days=3, min_filers=2)}
    check("ABCD 入选", "ABCD" in got, True)
    check("WXYZ 不入选", "WXYZ" in got, False)
    st.close()


def test_collect_marks_held_tickers():
    print("\n已持仓的票要标出来 —— 含义完全不同")
    st = fresh_store()
    seed_reddit(st, "2026-08-27", [("NVDA", 15, 70), ("OTHER", 18, 66)])
    lines = WL.collect(st, "2026-08-27", held_tickers={"NVDA"})
    joined = "\n".join(lines)
    check("NVDA 标了「已持仓」", "已持仓" in joined, True)
    check("两条都在", len(lines) >= 2, True)
    st.close()


if __name__ == "__main__":
    test_reddit_jump_detected()
    test_reddit_top10_excluded()
    test_reddit_missing_prior_rank_is_skipped()
    test_insider_cluster_needs_multiple_filers()
    test_collect_marks_held_tickers()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
