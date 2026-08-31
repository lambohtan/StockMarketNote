"""终端渲染：字段抽取、按显示宽度排版、用量表、精简结论。

`run_analysis.py`（上游原貌）和 `run_lean.py`（精简管线）共用这一份，免得两处
排版逻辑各自漂移。
"""

from __future__ import annotations

import re


def field(markdown: str, label: str) -> str:
    """取出 `**Label**: value` 一段——上游 render_* 函数产出的就是这个形状。"""
    if not markdown:
        return ""
    # 上游两种写法都出现过：`**Label**: v`（render_pm_decision）和
    # `**Label:** v`（render_sentiment_report）。两种都认。
    m = re.search(rf"\*\*{re.escape(label)}(?:\*\*:|:\*\*)\s*(.+?)(?=\n\*\*|\Z)",
                  markdown, re.S)
    return m.group(1).strip() if m else ""


def wrap(text: str, indent: str = "  ", width: int = 88) -> str:
    """按显示宽度折行；中文按两格算，不然中英混排会溢出终端。"""
    out, line, w = [], "", 0
    for ch in text.replace("\n", " "):
        cw = 2 if ord(ch) > 0x2E80 else 1
        if w + cw > width and ch == " ":
            out.append(line); line, w = "", 0
            continue
        line += ch; w += cw
        if w >= width and ch in " ，。、；":
            out.append(line); line, w = "", 0
    if line.strip():
        out.append(line)
    return "\n".join(indent + ln.strip() for ln in out if ln.strip())


def summarize(state: dict, ticker: str, trade_date: str, rating: str,
              elapsed: float, report_dir: Path) -> str:
    pm = state.get("final_trade_decision", "") or ""
    plan = state.get("investment_plan", "") or ""
    trader = state.get("trader_investment_plan", "") or ""
    sentiment = state.get("sentiment_report", "") or ""

    sent_line = ""
    m = re.search(r"\*\*Overall Sentiment:\*\*\s*\*\*(.+?)\*\*\s*\(Score:\s*([\d.]+)", sentiment)
    if m:
        conf = field(sentiment, "Confidence") or "?"
        sent_line = f"{m.group(1)}  {m.group(2)}/10  信心 {conf.splitlines()[0]}"

    lines = [
        "",
        f"━━━ {ticker} · {trade_date} · 用时 {elapsed / 60:.1f} 分钟 ━━━",
        "",
        f"  最终评级    {rating}   (Portfolio Manager)",
    ]
    rec = field(plan, "Recommendation")
    if rec:
        lines.append(f"  研究部意见  {rec.splitlines()[0]}   (Research Manager)")
    act = field(trader, "Action")
    if act:
        lines.append(f"  交易台动作  {act.splitlines()[0]}   (Trader)")
    if sent_line:
        lines.append(f"  市场情绪    {sent_line}")

    summary = field(pm, "Executive Summary")
    if summary:
        lines += ["", "  核心结论", wrap(summary, indent="    ")]

    target, horizon = field(pm, "Price Target"), field(pm, "Time Horizon")
    extras = []
    if target:
        extras.append(f"价格目标 {target.splitlines()[0]}")
    if horizon:
        extras.append(f"持有周期 {horizon.splitlines()[0]}")
    if extras:
        lines += ["", "  " + "    ".join(extras)]

    lines += [
        "",
        f"  完整报告    {report_dir}",
        "",
        "  提示：这是研究材料，不是下单指令。",
        "",
    ]
    return "\n".join(lines)


def dwidth(text: str) -> int:
    """显示宽度：CJK 占两格。用 len() 排版会让中英混排的表格错位。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in text)


def cell(text: str, width: int, right: bool = False) -> str:
    pad = " " * max(0, width - dwidth(text))
    return pad + text if right else text + pad


def usage_table(ledger) -> str:
    """按节点汇总这一轮的 token 和耗时。

    生成和启动分开列：启动是每次调用固定要付的 Claude Code 进程开销，跟模型和
    prompt 都无关，只跟调用次数有关。两者混在一起就看不出该优化哪个。
    """
    if not ledger.records:
        return ""

    order, grouped = [], {}
    for rec in ledger.records:
        if rec.node not in grouped:
            order.append(rec.node)
            grouped[rec.node] = []
        grouped[rec.node].append(rec)

    cols = (21, 3, 5, 8, 10, 9, 7, 6)

    def row(values, rights=(False, True, True, False, True, True, True, True)) -> str:
        return "  " + "  ".join(
            cell(v, w, r) for v, w, r in zip(values, cols, rights)
        )

    lines = ["", "━━━ 用量明细 ━━━", "",
             row(("节点", "次", "往返", "模型", "输入tok", "输出tok", "生成", "启动")),
             "  " + "─" * (sum(cols) + 2 * (len(cols) - 1))]

    for node in order:
        recs = grouped[node]
        lines.append(row((
            node,
            str(len(recs)),
            str(sum(r.iterations for r in recs)),
            "/".join(sorted({r.model for r in recs})),
            f"{sum(r.input_tokens + r.cache_read + r.cache_write for r in recs):,}",
            f"{sum(r.output_tokens for r in recs):,}",
            f"{sum(r.api_s for r in recs):.0f}s",
            f"{sum(r.startup_s for r in recs):.0f}s",
        )))

    n = len(ledger.records)
    api_s, startup_s = ledger.total("api_s"), ledger.total("startup_s")
    total_in = (ledger.total("input_tokens") + ledger.total("cache_read")
                + ledger.total("cache_write"))
    lines += ["  " + "─" * (sum(cols) + 2 * (len(cols) - 1)), row((
        "合计", str(n), f"{ledger.total('iterations'):.0f}", "",
        f"{total_in:,.0f}", f"{ledger.total('output_tokens'):,.0f}",
        f"{api_s:.0f}s", f"{startup_s:.0f}s",
    ))]

    actual = sorted({m for r in ledger.records for m in r.models_used})
    lines += [
        "",
        f"  实际调用的模型  {', '.join(actual) if actual else '未知'}",
        f"  生成 {api_s / 60:.1f} 分钟 ｜ CLI 启动开销 {startup_s / 60:.1f} 分钟"
        f"（{n} 次 × {startup_s / n:.0f} 秒，与模型和 prompt 都无关，只跟调用次数有关）",
        f"  按 API 挂牌价折算 ${ledger.total('cost_usd'):.2f}"
        f"（走 claude CLI 订阅时不另行计费，这里只作规模参考）",
        "",
        "  想更快：--fast（分析师和辩论换 haiku，两个 manager 保持 sonnet）",
        "         --analysts market,fundamentals（少两个分析师；节点就不全了）",
        "  别用 --rounds 0 提速：上游的轮次判定会让空方直接不发言（风险轮同理）。",
    ]
    return "\n".join(lines)


def verdict(result, ledger) -> str:
    """精简管线的终端输出：一个评级、一段话、一行证据健康度。

    刻意不打分析师原文。四份报告已经落盘；早上扫十只票时要的是能不能买，
    不是四万字研究。
    """
    plan = result.plan or ""
    lines = [
        "",
        f"━━━ {result.ticker} · {result.trade_date} · {result.seconds / 60:.1f} 分钟 ━━━",
        "",
        f"  评级  {field(plan, 'Recommendation').splitlines()[0] if field(plan, 'Recommendation') else '未产出'}",
    ]

    votes = getattr(result, "votes", None) or []
    if len(votes) > 1:
        from collections import Counter
        tally = Counter(votes)
        spread = "  ".join(f"{k} {v}/{len(votes)}" for k, v in tally.most_common())
        lines.append(f"  判官采样  {spread}"
                     + ("   ⚠ 结果分歧，众数仅供参考" if len(tally) > 1 else "   一致"))

    rationale = field(plan, "Rationale")
    if rationale:
        lines += ["", "  理由", wrap(rationale, indent="    ")]
    actions = field(plan, "Strategic Actions")
    if actions:
        lines += ["", "  操作要点", wrap(actions, indent="    ")]

    health = []
    for step in result.trace:
        if getattr(step, "has_evidence", False) or step.failed_sources:
            mark = "✗" if step.failed_sources else "✓"
            note = (f"（{'/'.join(sorted(set(step.failed_sources)))} 降级）"
                    if step.failed_sources else "")
            health.append(f"{step.node.replace(' Analyst', '')}{mark}{note}")
    if health:
        lines += ["", "  证据  " + " ｜ ".join(health)]

    lines += ["", "  提示：这是研究材料，不是下单指令。", ""]
    if ledger is not None:
        lines.append(usage_table(ledger))
    return "\n".join(lines)
