#!/usr/bin/env python3
"""观察池增量事件的测试。运行：python3 tests/test_watchlist.py

规则来自 HANDOFF「三个信号源的正确用法」，这里逐条锁死：
  - Reddit 绝对排名前 10 的**直接排除** —— 那时候你是接盘方
  - 有价值的是变化率：从 50 名开外冲进前 20
  - Form 4 只按多份申报的集中启发式，单笔忽略；不推断申报人或买卖方向
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


def test_reddit_invalid_tickers_are_excluded():
    print("\nReddit：NULL、空串、纯空白 ticker 不应生成事件")
    st = fresh_store()
    # helper 不过滤输入，确保过滤确实发生在生产查询层。
    seed_reddit(st, "2026-08-27", [
        ("VALID", 12, 78),
        (None, 12, 78),
        ("", 12, 78),
        ("   ", 12, 78),
    ])
    got = WL.reddit_jumps(st, "2026-08-27")
    check("无效 ticker 不进入 reddit_jumps",
          {x["ticker"] for x in got}, {"VALID"})
    lines = WL.collect(st, "2026-08-27")
    check("无效 ticker 不进入 collect", len(lines), 1)
    check("collect 保留有效 ticker", lines[0].startswith("VALID ") if lines else False,
          True)
    st.close()


def test_insider_cluster_counts_filings_not_filers():
    print("\nForm 4：按申报笔数启发式，不能虚构申报人或买入方向")
    st = fresh_store()
    rows = []
    # ABCD 三个不同 accession 只代表三份 filing；schema 没有 owner/filer identity，
    # 所以这里不能把它们表述为三个申报人或三笔买入。
    for i in range(3):
        rows.append((f"acc-a{i}", "2026-08-26", "ABCD", "111", "4", "",
                     "u", json.dumps({"company": "ABCD"}), "2026-08-27"))
    # WXYZ 只有一份 filing → 忽略
    rows.append(("acc-w0", "2026-08-26", "WXYZ", "222", "4", "",
                 "u", json.dumps({"company": "WXYZ"}), "2026-08-27"))
    st.upsert_many("edgar_filings",
                   ["accession", "filed_at", "ticker", "cik", "form",
                    "items", "url", "raw_json", "seen_at"], rows)
    canonical = WL.insider_filing_clusters(st, "2026-08-27",
                                           days=3, min_filings=2)
    compat = WL.insider_buy_clusters(st, "2026-08-27",
                                     days=3, min_filers=2)
    check("canonical 按申报笔数选中 ABCD",
          {x["ticker"] for x in canonical}, {"ABCD"})
    check("canonical 忽略单份 filing 的 WXYZ",
          "WXYZ" in {x["ticker"] for x in canonical}, False)
    check("legacy wrapper 与 canonical 结果一致", compat, canonical)

    lines = WL.collect(st, "2026-08-27")
    joined = "\n".join(lines)
    check("用户文案只说内部人申报集中", "内部人申报" in joined, True)
    check("用户文案不声称多个申报人", "申报人" not in joined, True)
    check("用户文案不声称买入", "买入" not in joined, True)
    st.close()


def test_legacy_wrapper_maps_threshold_to_canonical():
    print("\nlegacy 参数 min_filers 映射到 canonical 的 min_filings")
    st = fresh_store()
    calls = []
    original = WL.insider_filing_clusters

    def fake_canonical(store, d, days=3, min_filings=2):
        calls.append((store, d, days, min_filings))
        return [{"ticker": "ABCD", "filings": min_filings}]

    try:
        WL.insider_filing_clusters = fake_canonical
        got = WL.insider_buy_clusters(st, "2026-08-27",
                                      days=5, min_filers=4)
    finally:
        WL.insider_filing_clusters = original

    check("wrapper 返回 canonical 结果", got, [{"ticker": "ABCD", "filings": 4}])
    check("wrapper 正确传递 days/min_filings",
          (calls[0][2], calls[0][3]) if calls else None, (5, 4))
    st.close()


def test_collect_uses_canonical_and_neutral_wording():
    print("\ncollect 只调用 canonical，不依赖 legacy 误命名接口")
    st = fresh_store()
    original_canonical = WL.insider_filing_clusters
    original_legacy = WL.insider_buy_clusters

    def fake_canonical(*args, **kwargs):
        return [{"ticker": "CANON", "filings": 2}]

    def fake_legacy(*args, **kwargs):
        return [{"ticker": "LEGACY", "filings": 99}]

    try:
        WL.insider_filing_clusters = fake_canonical
        WL.insider_buy_clusters = fake_legacy
        lines = WL.collect(st, "2026-08-27")
    finally:
        WL.insider_filing_clusters = original_canonical
        WL.insider_buy_clusters = original_legacy

    joined = "\n".join(lines)
    check("collect 使用 canonical 结果", "CANON" in joined, True)
    check("collect 不调用 legacy 结果", "LEGACY" in joined, False)
    check("collect 文案保持中性", "买入" not in joined and "申报人" not in joined,
          True)
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
    test_reddit_invalid_tickers_are_excluded()
    test_insider_cluster_counts_filings_not_filers()
    test_legacy_wrapper_maps_threshold_to_canonical()
    test_collect_uses_canonical_and_neutral_wording()
    test_collect_marks_held_tickers()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
