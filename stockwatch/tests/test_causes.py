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
    print("\n同行读数：区分行业性事件和公司自己的事（样本足够大时）")
    # 修复轮 2 · Important 3：判定「行业性事件」要求同行数 >= MIN_PEERS_FOR_SECTOR_CALL，
    # 这里用 4 只同行（原来是 2 只——样本太小，已被修复轮 2 判定为不该下结论）
    attrs = [
        {"ticker": "A", "sector": "Technology", "z": 2.4},
        {"ticker": "B", "sector": "Technology", "z": 2.1},
        {"ticker": "C", "sector": "Technology", "z": 2.6},
        {"ticker": "E", "sector": "Technology", "z": 2.2},
        {"ticker": "D", "sector": "Energy", "z": 0.2},
    ]
    r = CS.peer_readthrough(attrs, "A", "Technology")
    check("同行数（不含自己）", r["n_peers"], 3)
    check("同样在动的同行数", r["n_moving"], 3)
    # 全行业都在动，样本也够 → 更像行业性事件
    check("判定为行业性", r["looks_sector_wide"], True)

    attrs2 = [
        {"ticker": "A", "sector": "Technology", "z": 3.0},
        {"ticker": "B", "sector": "Technology", "z": 0.1},
        {"ticker": "C", "sector": "Technology", "z": -0.3},
        {"ticker": "E", "sector": "Technology", "z": 0.0},
    ]
    r2 = CS.peer_readthrough(attrs2, "A", "Technology")
    check("只有自己在动 → 不是行业性", r2["looks_sector_wide"], False)


def test_peer_readthrough_requires_min_peers():
    """
    修复轮 2 · Important 3：n_peers=1 或 2 时，就算全部同行都在动，
    样本太小也不该下「行业性事件」的结论 —— 宁可承认样本不足。
    """
    print("\n同行数不足时（1 只、2 只），即使全动也不下「行业性」的结论")
    attrs_1peer = [
        {"ticker": "A", "sector": "Technology", "z": 3.0},
        {"ticker": "B", "sector": "Technology", "z": 3.0},   # 唯一的同行，也在动
    ]
    r1 = CS.peer_readthrough(attrs_1peer, "A", "Technology")
    check("n_peers=1", r1["n_peers"], 1)
    check("n_peers=1 时不判定为行业性", r1["looks_sector_wide"], False)

    attrs_2peers = [
        {"ticker": "A", "sector": "Technology", "z": 3.0},
        {"ticker": "B", "sector": "Technology", "z": 3.0},
        {"ticker": "C", "sector": "Technology", "z": 3.0},   # 2 只同行，全动
    ]
    r2 = CS.peer_readthrough(attrs_2peers, "A", "Technology")
    check("n_peers=2", r2["n_peers"], 2)
    check("n_peers=2 时仍不判定为行业性（样本太小）", r2["looks_sector_wide"], False)


def test_find_causes_peer_summary_states_sample_too_small():
    """
    修复轮 2 · Important 3：同行数不足时，find_causes 的摘要要陈述事实
    （样本不足），不能措辞成一个判断（行业性/公司自身）。
    """
    print("\n同行数不足时，摘要应陈述「样本不足」而不是下判断")
    st = fresh_store()
    pr = {"n_peers": 1, "n_moving": 1, "looks_sector_wide": False}
    got = CS.find_causes(st, "NOPE2", "2026-08-27", peers=pr, max_news=0)
    check("找到 1 条（同行读数）", len(got), 1)
    check("摘要提到样本不足", "样本不足" in got[0]["summary"], True)
    check("摘要不断言更像行业性事件", "更像行业性事件" in got[0]["summary"], False)
    check("摘要不断言集中在该公司自身", "集中在该公司自身" in got[0]["summary"], False)
    st.close()


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


class _RaisingStore:
    """模拟 store.q 抛异常（比如库被锁、schema 意外变化）。"""
    def q(self, sql, args=()):
        raise RuntimeError("模拟的数据库异常")


def test_8k_query_failure_does_not_crash():
    """
    修复轮 2 · Important 4：find_causes 会被套在遍历全部持仓的循环里调用
    （Task 9），8-K 查询本身失败不能让整个函数抛异常、中断其余持仓的查找。
    """
    print("\n8-K 查询抛异常时，find_causes 不该崩，应继续走后面的信源")
    got = CS.find_causes(_RaisingStore(), "BOOM", "2026-08-27", peers=None, max_news=0)
    check("返回的是列表而不是异常", isinstance(got, list), True)
    check("8-K 查不到时该列表为空", got, [])


def test_items_with_whitespace_still_matches_l1():
    """
    修复轮 2 · Minor 6：真实数据里 items 字段可能带空格（比如 "7.01, 5.02"）。
    .strip() 必须生效，否则 ' 5.02' 匹配不上 L1_ITEMS 的键 '5.02'，
    摘要里也会因为没清理空格而出现双空格。
    """
    print("\nitems 带空格时仍应正确识别 L1，且摘要不出现双空格")
    st = fresh_store()
    seed_filing(st, "SPCE", "2026-08-27", "8-K", "7.01, 5.02", "acc-space")
    got = CS.find_causes(st, "SPCE", "2026-08-27", peers=None, max_news=0)
    check("找到 1 条", len(got), 1)
    check("带空格的 items 仍判定为 L1", got[0]["is_l1"], True)
    check("item 取第一个且已去除空格", got[0]["item"], "7.01")
    check("摘要不含双空格", "  " in got[0]["summary"], False)
    st.close()


if __name__ == "__main__":
    test_finds_8k_and_marks_l1()
    test_non_l1_item_not_marked()
    test_honest_empty_when_nothing_found()
    test_only_looks_at_recent_window()
    test_peer_readthrough_distinguishes_sector_event()
    test_peer_readthrough_requires_min_peers()
    test_find_causes_peer_summary_states_sample_too_small()
    test_matches_row_shaped_like_fetch_filings_for_tickers()
    test_8k_query_failure_does_not_crash()
    test_items_with_whitespace_still_matches_l1()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
