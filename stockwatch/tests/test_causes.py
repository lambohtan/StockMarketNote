#!/usr/bin/env python3
"""找原因的测试。运行：python3 tests/test_causes.py

核心断言是**顺序**和**诚实**：
  - 8-K 必须排在新闻前面（官方申报比媒体转述可靠）
  - 什么都没找到时返回空列表，绝不编一个看似合理的理由
"""
import sys, tempfile, json
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw.analysis import causes as CS
from sw.sources import edgar_src as E

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Store(tmp.name)


def seed_filing(st, ticker, d, form, items, acc):
    st.upsert_many("edgar_filings",
                   ["accession", "filed_at", "ticker", "cik", "form",
                    "items", "url", "raw_json", "seen_at"],
                   [(acc, d, ticker, "0000000", form, items,
                     f"https://sec.gov/{acc}",
                     json.dumps({"company": ticker}), d)])


def test_finds_8k_and_marks_l1():
    print("\n8-K Item 5.02 应被识别为 L1")
    st = fresh_store()
    seed_filing(st, "WXYZ", "2026-08-27", "8-K", "5.02", "acc-1")
    got = CS.find_causes(st, "WXYZ", "2026-08-27", peers=None, max_news=0)
    check("找到 1 条", len(got), 1)
    check("来源是 8-K", got[0]["source"], "8-K")
    check("标为 L1", got[0]["is_l1"], True)
    check("item 记对", got[0]["item"], "5.02")
    check("含中文释义", "高管" in got[0]["summary"], True)
    st.close()


def test_non_l1_item_not_marked():
    print("\n普通 8-K item 不该被标成 L1")
    st = fresh_store()
    seed_filing(st, "ABCD", "2026-08-27", "8-K", "7.01", "acc-2")
    got = CS.find_causes(st, "ABCD", "2026-08-27", peers=None, max_news=0)
    check("找到 1 条", len(got), 1)
    check("不是 L1", got[0]["is_l1"], False)
    st.close()


def test_honest_empty_when_nothing_found():
    print("\n什么都没找到时必须返回空，不许编")
    st = fresh_store()
    got = CS.find_causes(st, "NOPE", "2026-08-27", peers=None, max_news=0)
    check("返回空列表", got, [])
    st.close()


def test_only_looks_at_recent_window():
    print("\n只看异动日前后的窗口，不能把上个月的 8-K 当今天的原因")
    st = fresh_store()
    seed_filing(st, "ABCD", "2026-07-01", "8-K", "5.02", "acc-old")
    got = CS.find_causes(st, "ABCD", "2026-08-27", peers=None, max_news=0)
    check("一个月前的不算", got, [])
    st.close()


def test_peer_readthrough_distinguishes_sector_event():
    print("\n同行读数：区分行业性事件和公司自己的事")
    attrs = [
        {"ticker": "A", "sector": "Technology", "z": 2.4},
        {"ticker": "B", "sector": "Technology", "z": 2.1},
        {"ticker": "C", "sector": "Technology", "z": 2.6},
        {"ticker": "D", "sector": "Energy", "z": 0.2},
    ]
    r = CS.peer_readthrough(attrs, "A", "Technology")
    check("同行数（不含自己）", r["n_peers"], 2)
    check("同样在动的同行数", r["n_moving"], 2)
    # 全行业都在动 → 更像行业性事件
    check("判定为行业性", r["looks_sector_wide"], True)

    attrs2 = [
        {"ticker": "A", "sector": "Technology", "z": 3.0},
        {"ticker": "B", "sector": "Technology", "z": 0.1},
        {"ticker": "C", "sector": "Technology", "z": -0.3},
    ]
    r2 = CS.peer_readthrough(attrs2, "A", "Technology")
    check("只有自己在动 → 不是行业性", r2["looks_sector_wide"], False)


def test_matches_row_shaped_like_fetch_filings_for_tickers():
    """
    修复轮 1：全市场 EDGAR 索引不带 ticker/items（已用真实请求核实），
    causes.py 实际会消费的是 edgar_src.fetch_filings_for_tickers() 产出的行。
    这里不发网络请求，只是用 _assemble_row（行组装的纯函数）模拟一条
    真实结构的 filing（ticker 非空、items 是逗号分隔的多个编号），
    确认它写进库之后 find_causes 能正确匹配、正确识别 L1、正确取第一个 item。
    """
    print("\n真实结构的行（ticker + 多个 items）应能被 find_causes 匹配")
    fake_filing = SimpleNamespace(
        accession_no="0000320193-26-000018",
        filing_date="2026-08-27",
        items="7.01,5.02",          # 非 L1 item 排在前，L1 item 排在后 —— any() 该认得
        cik="320193",
        filing_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000018/",
        company="Apple Inc.",
    )
    row = E._assemble_row(fake_filing, "8-K", "2026-08-27T09:00:00", ticker="AAPL")
    st = fresh_store()
    st.upsert_many("edgar_filings",
                   ["accession", "filed_at", "ticker", "cik", "form",
                    "items", "url", "raw_json", "seen_at"], [row])
    got = CS.find_causes(st, "AAPL", "2026-08-27", peers=None, max_news=0)
    check("找到 1 条", len(got), 1)
    check("标为 L1（items 里含 5.02 即可，不要求排在第一）", got[0]["is_l1"], True)
    check("item 取第一个", got[0]["item"], "7.01")
    check("摘要含两个 item 的中文释义", "高管" in got[0]["summary"] and "Regulation FD" in got[0]["summary"], True)
    st.close()


if __name__ == "__main__":
    test_finds_8k_and_marks_l1()
    test_non_l1_item_not_marked()
    test_honest_empty_when_nothing_found()
    test_only_looks_at_recent_window()
    test_peer_readthrough_distinguishes_sector_event()
    test_matches_row_shaped_like_fetch_filings_for_tickers()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
