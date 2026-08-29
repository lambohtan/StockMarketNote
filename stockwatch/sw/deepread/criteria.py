"""py 的六条硬指标。

**这六条的阈值是拍脑袋定的，未经回测验证。** 它是纪律工具 —— 让每天的
判断口径一致、可复核 —— 不是 alpha 来源。渲染时必须把这句话带上。

**明确没有估值分位**：免费数据拿不到可靠的历史 PE 序列，硬算出来的分位是
假精确。PE / forward PE 只在报告里如实列出原始值，不参与判定。

**第 5 条只说申报份数**：`edgar_filings` 里没有申报人身份和交易方向，
所以不能说「内部人买入」或「N 个人买」，只能说「N 份申报」。
"""

REVENUE_YOY_MIN = 0.20
GROSS_MARGIN_TOLERANCE_PT = -1.0
CORR_MAX = 0.50
FORM4_MIN = 3
RANK_JUMP_MIN = 10

CRITERIA = [
    {"key": "revenue_growth", "label": "营收同比增速 ≥ 20%"},
    {"key": "operating_cash_flow", "label": "经营现金流为正"},
    {"key": "gross_margin", "label": "毛利率环比未恶化（≥ 上季 −1pt）"},
    {"key": "low_correlation", "label": "与现有持仓相关性 < 0.5"},
    {"key": "insider_filings", "label": "近 30 天 Form 4 申报 ≥ 3 份"},
    {"key": "rank_jump", "label": "社区热度排名较 24h 前上升 ≥ 10 名"},
]

DISCLAIMER = "标准未经回测验证，是纪律工具不是涨跌预测。"


def _pct(x):
    return f"{x * 100:.1f}%"


def evaluate(facts):
    """判定六条。取不到数的记 None —— 不算命中也不算未命中，分母缩小。"""
    hits, details = {}, {}

    v = facts.get("revenue_yoy")
    hits["revenue_growth"] = None if v is None else v >= REVENUE_YOY_MIN
    details["revenue_growth"] = "数据缺失" if v is None else f"营收同比 {_pct(v)}"

    v = facts.get("operating_cash_flow")
    hits["operating_cash_flow"] = None if v is None else v > 0
    details["operating_cash_flow"] = (
        "数据缺失" if v is None
        else f"经营现金流 {v:,.0f}（{'为正' if v > 0 else '为负'}）")

    v = facts.get("gross_margin_delta_pt")
    hits["gross_margin"] = None if v is None else v >= GROSS_MARGIN_TOLERANCE_PT
    details["gross_margin"] = ("数据缺失" if v is None
                               else f"毛利率环比 {v:+.1f}pt")

    v = facts.get("avg_corr_to_holdings")
    hits["low_correlation"] = None if v is None else v < CORR_MAX
    details["low_correlation"] = ("数据缺失" if v is None
                                  else f"与持仓平均相关性 {v:.2f}")

    v = facts.get("form4_filings_30d")
    hits["insider_filings"] = None if v is None else v >= FORM4_MIN
    details["insider_filings"] = ("数据缺失" if v is None
                                  else f"近 30 天 {v} 份 Form 4 申报")

    v = facts.get("rank_delta")
    hits["rank_jump"] = None if v is None else v >= RANK_JUMP_MIN
    details["rank_jump"] = ("数据缺失" if v is None
                            else f"热度排名较 24h 前 {v:+d} 名")

    known = [k for k in hits if hits[k] is not None]
    return {"hits": hits, "details": details,
            "hit": sum(1 for k in known if hits[k]), "total": len(known)}
