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

    Task 8 修复轮 2 Minor 之后的行为变化：reason 字段现在会在截断之前先
    做 MONEY_RE 替换（跟 sw/alerts.py::render_alert_push 的「兜底替换」
    设计保持一致），所以 causes 里的金额不再让 render_push 抛异常，而是
    被静默替换成占位符后继续渲染——这是有意的行为变化，不是回归：
    assert_no_money(body) 仍然保留作为最终防线，用来兜住 reason 替换
    没覆盖到的其他字段（比如 watchlist_events、title）。
    """
    print("\n推送正文的金额守卫必须覆盖 causes 带来的文本（不只是模板本身）")
    ctx = make_ctx()
    ctx["causes_by_ticker"]["ABCD"] = [
        {"source": "新闻", "summary": "分析师预计相关支出约 1,234,567 美元",
         "url": "https://example.com", "item": None, "is_l1": False},
    ]
    title, body = DR.render_push(ctx)
    check("causes 里的裸金额没有原样出现在推送正文里", "1,234,567" in body, False)
    check("金额被替换成了占位符", "[金额见面板]" in body, True)


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


def test_push_money_survives_truncation_boundary():
    """
    Task 8 修复轮 2 Minor：render_push 里 reason[:80] 的字符截断如果发生
    在金额替换之前，金额短语恰好跨在第 80 字符边界上时会被切碎——
    比如「...累计成本约 50万」|「美元...」，截断后只剩裸的「50万」，
    没有币种单位就不匹配 MONEY_RE，原样进了推送正文。

    这里精确构造一条 causes summary：前缀正好 77 个字符，紧接着是
    「50万美元」，这样 reason[:80] 的旧写法会恰好切在「万」和「美」
    之间，只留下「50万」。用 assert 在测试内部先自检一次构造是否符合
    预期（不然这条测试就测不到我们想测的场景），再验证 render_push
    的真实输出里没有留下这个裸数量词。
    """
    print("\n金额短语跨在 80 字符截断边界上时也不能漏")
    prefix = "情" * 77
    summary = prefix + "50万美元，后续以公司披露为准"
    # 自检：确认构造的字符串真的会把「50万」和「美元」切在截断边界两侧。
    assert summary[:80] == prefix + "50万", "测试构造的边界不对，请检查前缀长度"
    ctx = make_ctx()
    ctx["causes_by_ticker"]["ABCD"] = [
        {"source": "新闻", "summary": summary,
         "url": "https://example.com", "item": None, "is_l1": False},
    ]
    title, body = DR.render_push(ctx)
    check("裸的「50万」没有原样出现在推送正文里（应已被替换成占位符）",
          "50万" in body, False)
    check("推送正文含金额占位符的痕迹", "[金额" in body, True)
    # 最终防线复核：即使占位符逻辑有问题，assert_no_money 也不能放过。
    assert_no_money(body)


def test_push_replaces_money_from_watchlist_events_instead_of_raising():
    """
    Task 8 修复轮 3：复审自选变异发现的不对称——同一个 render_push 里，
    causes 来的 reason 遇到金额会静默替换，watchlist_events 遇到金额却
    依旧硬抛 ValueError，导致整条推送渲染失败、当天日报全丢。协调者裁定
    统一成「替换」：推送是产品本体，为一个金额把当天报告整个弄没，代价
    远大于显示一个占位符。

    这里构造一条含金额的 watchlist_events，验证 render_push 不抛异常，
    body 里含占位符、不含原始金额数字。
    """
    print("\nwatchlist_events 里的金额也要被替换，而不是让整条推送渲染失败")
    ctx = make_ctx()
    ctx["watchlist_events"] = ["MNOP 获 3 家基金新建仓，合计 5000 万美元"]
    title, body = DR.render_push(ctx)
    check("watchlist 的裸金额没有原样出现在推送正文里", "5000 万美元" in body, False)
    check("金额被替换成了占位符", "[金额见面板]" in body, True)
    check("MNOP 仍然出现（不是把整条事件删掉，只是替换金额）", "MNOP" in body, True)
    # 最终防线复核：即使占位符逻辑有问题，assert_no_money 也不能放过。
    assert_no_money(body)


if __name__ == "__main__":
    test_quiet_holdings_are_absent()
    test_push_has_no_money_and_no_directives()
    test_push_blocks_money_leaking_from_causes()
    test_markdown_blocks_directive_leaking_from_causes()
    test_push_blocks_directive_leaking_from_causes()
    test_no_attributions_is_reported_as_missing_data()
    test_push_replaces_money_from_watchlist_events_instead_of_raising()
    test_push_money_survives_truncation_boundary()
    test_decomposition_shown()
    test_empty_day_is_still_valid()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
