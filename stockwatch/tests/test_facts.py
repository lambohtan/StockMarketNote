#!/usr/bin/env python3
"""原始数字采集测试。运行：python3 tests/test_facts.py

最重要的一条：**取不到数就是 None，不是 0**。
0 会被下游判成「现金流为负」，把「不知道」伪装成「不符合」。
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw.deepread import facts as F

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


class Cfg:
    def get(self, key, default=None):
        return {"benchmarks.market": "SPY"}.get(key, default)


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    return Store(tmp.name)


def seed(st):
    st.insert_ignore_many(
        "reddit_rank", ("d", "source", "ticker", "rank", "mentions",
                        "upvotes", "rank_24h_ago", "mentions_24h_ago"),
        [("2026-08-29", "all-stocks", "NVDA", 12, 500, 100, 50, 200)])
    st.insert_ignore_many(
        "edgar_filings", ("accession", "filed_at", "ticker", "cik", "form",
                          "items", "url", "raw_json", "seen_at"),
        [(f"acc-{i}", "2026-08-20", "NVDA", "1", "4", "", "", "{}", "x")
         for i in range(3)])


FIN = {"revenue_yoy": 0.94, "operating_cash_flow": 1.2e10,
       "gross_margin_delta_pt": -0.4, "pe": 52.3, "forward_pe": 31.8}


def test_collects_all_fields():
    st = fresh_store(); seed(st)
    f = F.collect(st, Cfg(), "NVDA", "2026-08-29", held_tickers=[], fin=lambda tk: FIN)
    check("营收增速", f["revenue_yoy"], 0.94)
    check("热度排名", f["rank"], 12)
    check("排名变化", f["rank_delta"], 38)
    check("Form4 申报数", f["form4_filings_30d"], 3)
    check("PE 原样带出", f["pe"], 52.3)
    # held=[] 时相关性算不出来，它必然在 missing 里 —— 这正是「不用 0 冒充」的体现
    check("只缺相关性一项", f["missing"], ["avg_corr_to_holdings"])
    st.close()


def test_missing_is_none_not_zero():
    st = fresh_store()          # 什么都不 seed
    f = F.collect(st, Cfg(), "ABCD", "2026-08-29", held_tickers=[],
                  fin=lambda tk: {})
    check("营收增速缺失是 None", f["revenue_yoy"], None)
    check("现金流缺失是 None", f["operating_cash_flow"], None)
    check("排名缺失是 None", f["rank"], None)
    # I1：ABCD 在窗口内完全没有任何 edgar_filings 行 —— 这只票根本没被
    # EDGAR 查过（全市场扫描行 ticker 是空串，ingest 只按持仓逐个查 Form 4，
    # 新票的 edgar_filings 天然是空的）。没查过不能报 0：报 0 会被
    # criteria.evaluate 判成硬「未命中」，把「没查」伪装成「查了没有」。
    check("没有 EDGAR 覆盖时 Form4 是 None 不是 0", f["form4_filings_30d"], None)
    check("缺失项被登记", "revenue_yoy" in f["missing"], True)
    check("Form4 缺失也被登记", "form4_filings_30d" in f["missing"], True)
    st.close()


def test_form4_zero_only_when_edgar_covers_ticker():
    """区分「没查过」与「查过、确实 0 份」：只有窗口内有任意申报行才报数。

    这是 I1 修复的核心断言：只塞一条非 Form 4 的申报（比如 8-K），证明
    这只票在窗口内确实被 EDGAR 覆盖到了 —— 这种情况下 Form 4 数量必须是
    确定的 0（criteria 仍应判「未命中」），而不是退化成 None（那样反而
    会把一个真实的「不符合」错误地缩小分母、算成「不知道」）。
    """
    st = fresh_store()
    st.insert_ignore_many(
        "edgar_filings", ("accession", "filed_at", "ticker", "cik", "form",
                          "items", "url", "raw_json", "seen_at"),
        [("acc-8k", "2026-08-20", "NVDA", "1", "8-K", "", "", "{}", "x")])
    f = F.collect(st, Cfg(), "NVDA", "2026-08-29", held_tickers=[],
                  fin=lambda tk: {})
    check("被覆盖但 0 份 Form4 时是确定的 0", f["form4_filings_30d"], 0)
    check("确定的 0 不进 missing", "form4_filings_30d" in f["missing"], False)
    st.close()


def test_financials_failure_does_not_raise():
    st = fresh_store(); seed(st)
    def boom(tk):
        raise RuntimeError("yfinance 挂了")
    f = F.collect(st, Cfg(), "NVDA", "2026-08-29", held_tickers=[], fin=boom)
    check("财务源挂了不抛异常", f["revenue_yoy"], None)
    check("其他字段照常", f["rank"], 12)
    # 财务源整体抛异常也要留痕到 source_health，方便日后排查
    rows = st.q("SELECT * FROM source_health WHERE source='yfinance.financials'")
    check("整体异常写入 source_health", len(rows) >= 1, True)
    check("留痕包含异常信息", "yfinance 挂了" in (rows[0]["detail"] if rows else ""), True)
    st.close()


def test_no_holdings_means_corr_unknown():
    st = fresh_store(); seed(st)
    f = F.collect(st, Cfg(), "NVDA", "2026-08-29", held_tickers=[], fin=lambda tk: FIN)
    check("没有持仓时相关性是 None", f["avg_corr_to_holdings"], None)
    st.close()


def test_nan_is_treated_as_missing():
    """NaN 不是合法数值 —— 它真值为 True、参与运算不抛异常，必须被拦成 None。"""
    st = fresh_store(); seed(st)
    nan_fin = dict(FIN)
    nan_fin["revenue_yoy"] = float("nan")
    nan_fin["operating_cash_flow"] = float("nan")
    f = F.collect(st, Cfg(), "NVDA", "2026-08-29", held_tickers=[], fin=lambda tk: nan_fin)
    check("NaN 营收增速折成 None", f["revenue_yoy"], None)
    check("NaN 现金流折成 None", f["operating_cash_flow"], None)
    check("NaN 字段进 missing", "revenue_yoy" in f["missing"], True)
    check("NaN 现金流字段进 missing", "operating_cash_flow" in f["missing"], True)
    # 未被污染的字段（毛利率环比、PE）应照常带出，证明拦截只作用于 NaN 本身
    check("非 NaN 字段不受影响", f["gross_margin_delta_pt"], -0.4)
    st.close()


def test_rank_delta_missing_when_no_prior_rank():
    """今天有排名、没有 24h 前排名（比如刚进榜）—— rank_prev/rank_delta 都应缺失。"""
    st = fresh_store()
    st.insert_ignore_many(
        "reddit_rank", ("d", "source", "ticker", "rank", "mentions",
                        "upvotes", "rank_24h_ago", "mentions_24h_ago"),
        [("2026-08-29", "all-stocks", "NVDA", 12, 500, 100, None, None)])
    f = F.collect(st, Cfg(), "NVDA", "2026-08-29", held_tickers=[], fin=lambda tk: FIN)
    check("有排名", f["rank"], 12)
    check("无前值排名是 None", f["rank_prev"], None)
    check("无前值时排名变化是 None", f["rank_delta"], None)
    check("rank_prev 进 missing", "rank_prev" in f["missing"], True)
    check("rank_delta 进 missing", "rank_delta" in f["missing"], True)
    st.close()


def test_financials_partial_error_logs_health():
    """financials() 内部单块失败（通过 _errors 上报）同样要留痕，不能悄悄消失。"""
    st = fresh_store(); seed(st)
    def partial(tk):
        return {"pe": 52.3, "_errors": ["income_stmt KeyError: 'Total Revenue'"]}
    f = F.collect(st, Cfg(), "NVDA", "2026-08-29", held_tickers=[], fin=partial)
    check("_errors 不混入固定 key", "_errors" in f, False)
    check("其他字段照常带出", f["pe"], 52.3)
    rows = st.q("SELECT * FROM source_health WHERE source='yfinance.financials'")
    check("单块失败写入 source_health", len(rows) >= 1, True)
    check("留痕包含具体错误", "Total Revenue" in (rows[0]["detail"] if rows else ""), True)
    st.close()


for fn in (test_collects_all_fields, test_missing_is_none_not_zero,
           test_form4_zero_only_when_edgar_covers_ticker,
           test_financials_failure_does_not_raise, test_no_holdings_means_corr_unknown,
           test_nan_is_treated_as_missing, test_rank_delta_missing_when_no_prior_rank,
           test_financials_partial_error_logs_health):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
