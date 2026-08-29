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
    check("Form4 无申报是 0 不是 None", f["form4_filings_30d"], 0)
    check("缺失项被登记", "revenue_yoy" in f["missing"], True)
    st.close()


def test_financials_failure_does_not_raise():
    st = fresh_store(); seed(st)
    def boom(tk):
        raise RuntimeError("yfinance 挂了")
    f = F.collect(st, Cfg(), "NVDA", "2026-08-29", held_tickers=[], fin=boom)
    check("财务源挂了不抛异常", f["revenue_yoy"], None)
    check("其他字段照常", f["rank"], 12)
    st.close()


def test_no_holdings_means_corr_unknown():
    st = fresh_store(); seed(st)
    f = F.collect(st, Cfg(), "NVDA", "2026-08-29", held_tickers=[], fin=lambda tk: FIN)
    check("没有持仓时相关性是 None", f["avg_corr_to_holdings"], None)
    st.close()


for fn in (test_collects_all_fields, test_missing_is_none_not_zero,
           test_financials_failure_does_not_raise, test_no_holdings_means_corr_unknown):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
