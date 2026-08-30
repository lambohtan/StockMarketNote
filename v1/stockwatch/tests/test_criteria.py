#!/usr/bin/env python3
"""六条硬指标测试。运行：python3 tests/test_criteria.py

延续 P2 的教训（HANDOFF §11）：一眼看不出对错的判定必须有已知答案的锚。
这里每条都用刚好在阈值两侧的合成 facts 锁死边界。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.deepread import criteria as C

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def facts(**kw):
    # 注：gross_margin_delta_pt 与 avg_corr_to_holdings 的基线故意设成会
    # 「不命中」的值（而不是 brief 原文里恰好会「命中」的 0.0 / 0.1）。
    # 原因见 test_unknown_shrinks_denominator：那条测试只覆盖
    # revenue_yoy / operating_cash_flow 两个字段，其余字段吃这里的基线值；
    # 如果基线值本身就命中，会污染「缺失不算命中」这条断言，
    # 跟这两个字段各自的边界测试（它们会显式覆盖这两个 key）互不冲突。
    base = {"ticker": "X", "revenue_yoy": 0.0, "operating_cash_flow": 1.0,
            "gross_margin_delta_pt": -5.0, "avg_corr_to_holdings": 0.9,
            "form4_filings_30d": 0, "rank": 30, "rank_prev": 30,
            "rank_delta": 0, "pe": None, "forward_pe": None, "missing": []}
    base.update(kw)
    return base


def test_revenue_growth_boundary():
    check("增速 20.0% 命中", C.evaluate(facts(revenue_yoy=0.20))["hits"]["revenue_growth"], True)
    check("增速 19.9% 不命中", C.evaluate(facts(revenue_yoy=0.199))["hits"]["revenue_growth"], False)


def test_cash_flow_boundary():
    check("现金流为正命中", C.evaluate(facts(operating_cash_flow=1.0))["hits"]["operating_cash_flow"], True)
    check("现金流为 0 不命中", C.evaluate(facts(operating_cash_flow=0.0))["hits"]["operating_cash_flow"], False)
    check("现金流为负不命中", C.evaluate(facts(operating_cash_flow=-1.0))["hits"]["operating_cash_flow"], False)


def test_gross_margin_boundary():
    check("环比 -1.0pt 仍算未恶化",
          C.evaluate(facts(gross_margin_delta_pt=-1.0))["hits"]["gross_margin"], True)
    check("环比 -1.1pt 算恶化",
          C.evaluate(facts(gross_margin_delta_pt=-1.1))["hits"]["gross_margin"], False)


def test_correlation_boundary():
    check("相关性 0.49 命中", C.evaluate(facts(avg_corr_to_holdings=0.49))["hits"]["low_correlation"], True)
    check("相关性 0.50 不命中", C.evaluate(facts(avg_corr_to_holdings=0.50))["hits"]["low_correlation"], False)


def test_form4_boundary():
    check("3 份命中", C.evaluate(facts(form4_filings_30d=3))["hits"]["insider_filings"], True)
    check("2 份不命中", C.evaluate(facts(form4_filings_30d=2))["hits"]["insider_filings"], False)


def test_rank_jump_boundary():
    check("上升 10 名命中", C.evaluate(facts(rank_delta=10))["hits"]["rank_jump"], True)
    check("上升 9 名不命中", C.evaluate(facts(rank_delta=9))["hits"]["rank_jump"], False)
    check("下降不命中", C.evaluate(facts(rank_delta=-20))["hits"]["rank_jump"], False)


def test_unknown_shrinks_denominator():
    r = C.evaluate(facts(revenue_yoy=None, operating_cash_flow=None))
    check("缺失记 None", r["hits"]["revenue_growth"], None)
    check("分母缩小到 4", r["total"], 4)
    check("缺失不算命中", r["hit"], 0)


def test_all_hit():
    r = C.evaluate(facts(revenue_yoy=0.94, operating_cash_flow=1e9,
                         gross_margin_delta_pt=0.5, avg_corr_to_holdings=0.2,
                         form4_filings_30d=4, rank_delta=38))
    check("全中 6/6", (r["hit"], r["total"]), (6, 6))


def test_details_are_human_readable():
    r = C.evaluate(facts(revenue_yoy=0.94))
    check("细节里带具体数值", "94" in r["details"]["revenue_growth"], True)
    # operating_cash_flow 曾是六条里唯一不带数值的一条（只写「为正/为负」），
    # 与「报告里每个数字都能追溯」的要求有落差；这里锁住修复。
    r2 = C.evaluate(facts(operating_cash_flow=1.2e10))
    check("现金流细节里也带具体数值", "12,000,000,000" in r2["details"]["operating_cash_flow"], True)


for fn in (test_revenue_growth_boundary, test_cash_flow_boundary,
           test_gross_margin_boundary, test_correlation_boundary,
           test_form4_boundary, test_rank_jump_boundary,
           test_unknown_shrinks_denominator, test_all_hit,
           test_details_are_human_readable):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
