#!/usr/bin/env python3
"""渲染与守卫测试。运行：python3 tests/test_deepread_render.py

两条硬要求：
  - 推送正文里不能有 $ 金额（新闻正文里全是），且替换必须在截断之前
  - 倾向标签词要能过 assert_no_directives，买卖指令仍要被拦
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.deepread import render as R
from sw.policy import assert_no_directives, assert_no_money

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def item(**kw):
    base = {
        "ticker": "NVDA", "source": "apewisdom",
        "reason": "社区热度第 12 名（较 24h 前 +38）",
        "label": "偏正面", "note": "", "hit": 6, "total": 8,
        "narrative": "发生了什么：营收同比增长 94%。\n\n多头论点：订单能见度高。\n\n"
                     "空头论点：毛利率环比走弱。\n\n接下来盯什么：① 指引 ② 供货 ③ 资本开支",
        "disagreements": [], "text_hits": {}, "text_quotes": {},
        "model": "claude-opus-5", "disclaimer": "标准未经回测验证，是纪律工具不是涨跌预测。",
        "facts": {"pe": 52.3, "forward_pe": 31.8, "rank": 12, "rank_prev": 50,
                  "rank_delta": 38, "missing": []},
        "py": {"hits": {"revenue_growth": True, "low_correlation": False},
               "details": {"revenue_growth": "营收同比 94.0%",
                           "low_correlation": "与持仓平均相关性 0.71"},
               "hit": 5, "total": 6},
    }
    base.update(kw)
    return base


def test_report_has_required_parts():
    md = R.render_report("2026-08-29", [item()])
    for needle in ("NVDA", "偏正面", "6/8", "入池原因", "发生了什么",
                   "接下来盯什么", "未经回测验证", "PE"):
        check(f"报告含 {needle!r}", needle in md, True)


def test_report_lists_rank_and_delta():
    """前 10 全收之后，接盘风险靠这两个数字可见。"""
    md = R.render_report("2026-08-29", [item()])
    check("列出当前排名", "12" in md, True)
    check("列出排名变化", "+38" in md or "38" in md, True)


def test_report_shows_disagreements():
    md = R.render_report("2026-08-29", [item(disagreements=[
        {"key": "gross_margin", "label": "毛利率环比未恶化", "py": True, "llm": False,
         "py_detail": "毛利率环比 -0.4pt",
         "llm_quote": "we expect gross margin to normalize"}])])
    check("分歧成节", "分歧" in md, True)
    check("py 理由在", "-0.4pt" in md, True)
    check("LLM 原文引用在", "normalize" in md, True)


def test_push_strips_money_before_truncating():
    long_news = "公司宣布 $1,234,567 的回购计划。" * 200
    t, b = R.render_push(item(narrative="发生了什么：" + long_news))
    check("推送不含美元金额", "$" in b, False)
    check("推送在上限内", len(b.encode("utf-8")) <= R.MAX_PUSH_BYTES, True)
    assert_no_money(b)      # 抛异常就是没做干净
    check("金额守卫通过", True, True)


def test_push_passes_directive_guard():
    t, b = R.render_push(item())
    assert_no_directives(t)
    assert_no_directives(b)
    check("标签词过守卫", True, True)


def test_push_title_carries_label_and_score():
    t, b = R.render_push(item())
    check("标题含代码", "NVDA" in t, True)
    check("标题含标签", "偏正面" in t, True)
    check("标题含比分", "6/8" in t, True)


def test_push_points_to_report():
    t, b = R.render_push(item())
    check("正文指向报告文件", "pool_" in b, True)


def test_low_confidence_note_surfaces_in_push():
    t, b = R.render_push(item(note="py 与 LLM 有 2 条分歧，此标签可信度低"))
    check("可信度提示进推送", "可信度低" in b, True)


for fn in (test_report_has_required_parts, test_report_lists_rank_and_delta,
           test_report_shows_disagreements, test_push_strips_money_before_truncating,
           test_push_passes_directive_guard, test_push_title_carries_label_and_score,
           test_push_points_to_report, test_low_confidence_note_surfaces_in_push):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
