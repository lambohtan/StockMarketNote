#!/usr/bin/env python3
"""日报渲染测试。运行：python3 tests/test_daily_report.py

两条核心断言：
  1. 残差正常的持仓**根本不出现** —— 日报一半的价值来自它不说什么
  2. 推送版不含金额 —— ntfy.sh 是公共服务器
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw import daily_report as DR
from sw.notify import assert_no_money
from sw.alerts import assert_no_directives

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def make_ctx():
    return {
        "d": "2026-08-27",
        "attributions": [
            {"ticker": "ABCD", "ret": 0.062, "mkt_part": 0.008, "sector_part": 0.021,
             "idio": 0.033, "z": 2.4, "level": "anomaly", "sector": "Technology",
             "beta_mkt": 1.4, "beta_sector": 0.9, "r2": 0.55, "n": 60},
            {"ticker": "QUIET", "ret": 0.004, "mkt_part": 0.003, "sector_part": 0.001,
             "idio": 0.000, "z": 0.1, "level": "normal", "sector": "Technology",
             "beta_mkt": 1.0, "beta_sector": 0.8, "r2": 0.7, "n": 60},
            {"ticker": "ALSOQUIET", "ret": -0.002, "mkt_part": -0.001,
             "sector_part": -0.001, "idio": 0.0, "z": -0.2, "level": "normal",
             "sector": "Energy", "beta_mkt": 0.6, "beta_sector": 0.9,
             "r2": 0.6, "n": 60},
        ],
        "causes_by_ticker": {
            "ABCD": [{"source": "8-K", "summary": "2026-08-27 提交 8-K：披露季度业绩",
                      "url": "https://sec.gov/x", "item": "2.02", "is_l1": False}],
        },
        "alerts": [],
        "watchlist_events": ["3 家基金本季新建仓 MNOP", "LMNO 讨论量从 78 名升至 19 名"],
        "market": {"spy_vs_200d": 0.042, "vix": 16.8},
        "portfolio_weights": {"ABCD": 0.11, "QUIET": 0.07, "ALSOQUIET": 0.03},
        "n_holdings": 3,
    }


def test_quiet_holdings_are_absent():
    print("\n残差正常的持仓不该出现（日报一半的价值来自它不说什么）")
    md = DR.render_markdown(make_ctx())
    check("异动的 ABCD 出现", "ABCD" in md, True)
    check("平静的 QUIET 不出现", "QUIET" in md.replace("ALSOQUIET", ""), False)
    check("有「其余 N 只无异常」的交代", "无异常" in md, True)


def test_push_has_no_money_and_no_directives():
    print("\n推送版：无金额、无指令")
    ctx = make_ctx()
    title, body = DR.render_push(ctx)
    assert_no_money(body)
    assert_no_money(title)
    assert_no_directives(body)
    check("标题含日期", "2026-08-27" in title, True)
    check("正文含异动代码", "ABCD" in body, True)
    check("正文含观察池", "MNOP" in body, True)
    print("  ✅ 推送版通过金额与指令双重检查")


def test_decomposition_shown():
    print("\n必须展示三段拆解，每个数字可追溯")
    md = DR.render_markdown(make_ctx())
    for frag in ["大盘", "行业", "个股独立", "σ"]:
        check(f"含「{frag}」", frag in md, True)


def test_empty_day_is_still_valid():
    print("\n全无异动的一天也要能生成报告，不能崩")
    ctx = make_ctx()
    for a in ctx["attributions"]:
        a["level"], a["z"] = "normal", 0.1
    ctx["causes_by_ticker"] = {}
    ctx["watchlist_events"] = []
    md = DR.render_markdown(ctx)
    title, body = DR.render_push(ctx)
    check("Markdown 非空", len(md) > 50, True)
    check("推送正文非空", len(body) > 10, True)
    check("说明今天无异动", "无异常" in md or "无异动" in md, True)


def test_push_blocks_money_leaking_from_causes():
    """
    金额守卫不能只防模板本身写死的文案 —— causes 的 summary 来自新闻/
    8-K/LLM 摘要，是外部输入，一样可能带金额。这里直接让一条 causes
    summary 里混进金额，验证 render_push 真的会拦下来，而不是因为
    默认测试数据里从来没出现过金额，导致内部的 assert_no_money 形同摆设。
    """
    print("\n推送正文的金额守卫必须覆盖 causes 带来的文本（不只是模板本身）")
    ctx = make_ctx()
    ctx["causes_by_ticker"]["ABCD"] = [
        {"source": "新闻", "summary": "分析师预计相关支出约 1,234,567 美元",
         "url": "https://example.com", "item": None, "is_l1": False},
    ]
    try:
        DR.render_push(ctx)
        check("causes 里的金额被 render_push 拦下（未抛异常）", False, True)
    except ValueError:
        check("causes 里的金额被 render_push 的 assert_no_money 拦下", True, True)


def test_markdown_blocks_directive_leaking_from_causes():
    """
    Task 8 修复轮 1 Important 2：复审指出 render_markdown 里的
    assert_no_directives 没有任何回归测试兜底——把它删掉，之前的
    4 个测试仍然全绿。跟金额守卫是同一类盲区：默认测试数据里从来
    没出现过指令性措辞，测试当然测不出「删掉守卫会怎样」。

    这里让一条 causes summary（模拟 LLM 摘要）带上明确的指令性措辞，
    验证 render_markdown 真的会因为 assert_no_directives 抛出 ValueError。
    """
    print("\nrender_markdown 的指令守卫必须覆盖 causes 带来的文本")
    ctx = make_ctx()
    ctx["causes_by_ticker"]["ABCD"] = [
        {"source": "新闻", "summary": "你应该立即清仓 ABCD",
         "url": "https://example.com", "item": None, "is_l1": False},
    ]
    try:
        DR.render_markdown(ctx)
        check("causes 里的指令措辞被 render_markdown 拦下（未抛异常）", False, True)
    except ValueError:
        check("causes 里的指令措辞被 render_markdown 的 assert_no_directives 拦下",
              True, True)


def test_push_blocks_directive_leaking_from_causes():
    """同上，验证 render_push 侧也真的会拦。"""
    print("\nrender_push 的指令守卫必须覆盖 causes 带来的文本")
    ctx = make_ctx()
    ctx["causes_by_ticker"]["ABCD"] = [
        {"source": "新闻", "summary": "你应该立即清仓 ABCD",
         "url": "https://example.com", "item": None, "is_l1": False},
    ]
    try:
        DR.render_push(ctx)
        check("causes 里的指令措辞被 render_push 拦下（未抛异常）", False, True)
    except ValueError:
        check("causes 里的指令措辞被 render_push 的 assert_no_directives 拦下",
              True, True)


def test_no_attributions_is_reported_as_missing_data():
    """
    Task 8 修复轮 1 Minor：attributions 为空时不能说成「持仓全部无异常」——
    「没有可归因的持仓数据」和「持仓都正常」是两件事，混成一句会让人误以为
    系统跑过归因、结果一切正常，实际上可能是持仓快照缺失或行情数据不足。
    """
    print("\n无归因数据时要明确说「没有数据」而不是「全部无异常」")
    ctx = make_ctx()
    ctx["attributions"] = []
    md = DR.render_markdown(ctx)
    check("说明没有可归因数据", "没有可归因的持仓数据" in md, True)
    check("没有误导性地说全部无异常", "0 只持仓**全部无异常**" in md, False)


if __name__ == "__main__":
    test_quiet_holdings_are_absent()
    test_push_has_no_money_and_no_directives()
    test_push_blocks_money_leaking_from_causes()
    test_markdown_blocks_directive_leaking_from_causes()
    test_push_blocks_directive_leaking_from_causes()
    test_no_attributions_is_reported_as_missing_data()
    test_decomposition_shown()
    test_empty_day_is_still_valid()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
