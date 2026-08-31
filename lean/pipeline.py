"""精简管线：4 分析师 → 多空辩论 → Research Manager，到此为止。

和 `run_analysis.py`（上游原貌）的关系
------------------------------------
这条路复用上游**同一批** agent 工厂和 prompt，只换掉三件事：

1. 编排方式。选定的 7 个节点是线性的，不需要 LangGraph 的条件路由，所以这里
   自己按顺序驱动。上游的 `setup.py` / `conditional_logic.py` / `trading_graph.py`
   一行未改，`run_analysis.py` 随时还能跑完整的 12 节点版本。
2. 证据来源。见 `evidence.py` 与下面的 `EvidenceMode`。
3. 输出长度。通过 `ChatClaudeCLI.brevity` 在系统提示末尾追加硬性字数上限。

被砍掉的是 Trader、三个风险辩论者和 Portfolio Manager（5 次调用）。Research
Manager 本身就产出 Buy/Overweight/Hold/Underweight/Sell 评级和理由，正是要看的
东西；砍掉的那些节点每一个都要把四份分析师报告完整读一遍，是输入端最贵的部分。
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.agents.analysts.news_analyst import create_news_analyst
from tradingagents.agents.analysts.sentiment_analyst import create_sentiment_analyst
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.utils import agent_utils
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    resolve_instrument_identity,
)

from . import evidence
from .sentiment import compressed_sentiment_node

#: 三个分析师会用到的全部工具，按名字索引，供 hybrid / tools 模式执行。
TOOLS = {
    t.name: t
    for t in (
        agent_utils.get_stock_data,
        agent_utils.get_indicators,
        agent_utils.get_verified_market_snapshot,
        agent_utils.get_fundamentals,
        agent_utils.get_balance_sheet,
        agent_utils.get_cashflow,
        agent_utils.get_income_statement,
        agent_utils.get_news,
        agent_utils.get_global_news,
        agent_utils.get_macro_indicators,
        agent_utils.get_prediction_markets,
    )
}

ANALYSTS = {
    "market": ("Market Analyst", create_market_analyst, "market_report"),
    "sentiment": ("Sentiment Analyst", create_sentiment_analyst, "sentiment_report"),
    "news": ("News Analyst", create_news_analyst, "news_report"),
    "fundamentals": ("Fundamentals Analyst", create_fundamentals_analyst, "fundamentals_report"),
}

#: 输出约束。语言必须在这里显式写死：这段提示排在系统提示最末尾，模型会跟着它
#: 的语言走——之前这段只有中文，导致 sonnet 输出中文而 opus 跟随上游的英文指令
#: 输出英文，同一条流水线两种语言，没法对照。
BREVITY_TEMPLATE = """=== HARD OUTPUT LIMIT (overrides any earlier request for detail, thoroughness, or tables) ===
Write in {language}. At most {words} words. No markdown tables, no headings; 3-5 short bullets.
Every number you cite must appear in the evidence given above. If it is not there, write "unknown" —
do not estimate and do not add background knowledge.
Do not restate the data; say what it implies."""

#: 默认字数上限。除下表点名的节点外，所有节点用这个。
DEFAULT_WORDS = 180

#: 按节点分配的字数上限。**不是**所有节点都缺字数——实测（AMD，同一天同一份
#: 证据）把全局上限取消后：
#:
#:   Fundamentals  +5% 的增量token，但正是它带来了差异——180 字下它写不出
#:                 Forward EPS 与 TTM EPS 的倍数关系，估值论证整条缺失
#:   News          +2%，宏观与个股事件要分两步说清
#:   Market        增量为**负**（输出反而更短），它本来就不缺字数
#:   Sentiment     吃掉 33% 的增量，而它的置信度受数据源上限封顶（见 README
#:                 已知问题 4），给再多字数也换不来更可靠的结论
#:
#: 所以放宽只给 fundamentals 和 news。给 market/sentiment 加字数是纯浪费。
WORD_LIMITS = {
    "News Analyst": 400,
    "Fundamentals Analyst": 400,
}


def brevity_for(language: str, words: int = DEFAULT_WORDS) -> str:
    return BREVITY_TEMPLATE.format(language=language, words=words)


class SourceWatch(logging.Handler):
    """捕获取数过程中的失败告警。

    Sentiment 分析师由上游自己抓 news/StockTwits/Reddit，没有注入点，所以它的
    来源健康无法像其他分析师那样从 `evidence.Block` 读出来。上游的 fetcher 失败
    时都会 `logger.warning`，这里挂一个 handler 收集，让降级在终端摘要里可见——
    否则 Reddit 哪天再挂掉，你只会看到一份置信度莫名很低的情绪报告。
    """

    _SOURCE_NAMES = {"reddit": "Reddit", "stocktwits": "StockTwits",
                     "yfinance_news": "新闻", "y_finance": "行情",
                     "fred": "FRED", "polymarket": "预测市场"}

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.failed: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        module = record.name.rsplit(".", 1)[-1]
        name = self._SOURCE_NAMES.get(module)
        if name and name not in self.failed:
            self.failed.append(name)

    def __enter__(self) -> SourceWatch:
        logging.getLogger("tradingagents").addHandler(self)
        return self

    def __exit__(self, *exc) -> None:
        logging.getLogger("tradingagents").removeHandler(self)


@dataclass
class StepTrace:
    """一个节点实际发生了什么——用来解释账单，也用来暴露降级。"""

    node: str
    calls: int = 0
    tool_rounds: int = 0
    evidence_chars: int = 0
    failed_sources: list[str] = field(default_factory=list)
    seconds: float = 0.0
    #: 该节点确实用上了证据（无论是我预取的还是上游自取的），用来决定要不要
    #: 在终端摘要的证据健康行里出现。
    has_evidence: bool = False


@dataclass
class LeanResult:
    ticker: str
    trade_date: str
    reports: dict[str, str]
    bull: str
    bear: str
    plan: str
    trace: list[StepTrace]
    seconds: float
    #: 判官每次采样给出的评级，按先后顺序。`plan` 取的是众数那一次的原文。
    votes: list[str] = field(default_factory=list)


class EvidenceMode:
    """PREFETCH：Python 一次取完，不给工具，一次成文（最省）。

    HYBRID：Python 先给证据，模型仍可继续调工具补充，但轮次有上限。
    TOOLS：上游原貌，模型自己从零开始调工具。
    """

    PREFETCH = "prefetch"
    HYBRID = "hybrid"
    TOOLS = "tools"


def _rating_of(plan: str) -> str:
    """从 render_research_plan 的 markdown 里取出评级标签。"""
    match = re.search(r"\*\*Recommendation\*\*:\s*(\S+)", plan or "")
    return match.group(1).strip() if match else "未产出"


def _run_node_with_tools(node, state: dict, llm, max_rounds: int,
                         trace: StepTrace) -> Any:
    """驱动一个分析师节点，最多允许 `max_rounds` 轮工具调用。

    第 `max_rounds` 轮时把 `ignore_tools` 打开，逼它用手上的材料成文。没有这一步
    的话，模型可以无限申请工具，而每一轮都要把之前所有工具返回重新发一遍——那
    正是原版最贵的地方（实测单轮输入 43,728 token）。
    """
    original = llm.ignore_tools
    try:
        for round_index in range(max_rounds + 1):
            llm.ignore_tools = original or round_index >= max_rounds
            out = node(state)
            trace.calls += 1
            message = out["messages"][-1]
            state["messages"] = state["messages"] + list(out["messages"])

            calls = getattr(message, "tool_calls", None) or []
            if not calls:
                return out
            trace.tool_rounds += 1

            results = []
            for call in calls:
                tool = TOOLS.get(call["name"])
                if tool is None:
                    content = f"ERROR: 未知工具 {call['name']}"
                    trace.failed_sources.append(call["name"])
                else:
                    try:
                        content = str(tool.invoke(call["args"]))
                    except Exception as exc:      # noqa: BLE001
                        content = f"ERROR: {type(exc).__name__}: {exc}"
                        trace.failed_sources.append(call["name"])
                results.append(ToolMessage(content=content, tool_call_id=call["id"],
                                           name=call["name"]))
            state["messages"] = state["messages"] + results
        return out
    finally:
        llm.ignore_tools = original


def run(ticker: str, trade_date: str, quick_llm, deep_llm,
        analysts: tuple[str, ...] = ("market", "sentiment", "news", "fundamentals"),
        mode: str = EvidenceMode.HYBRID, tool_rounds: int = 2,
        debate_rounds: int = 1, brief: bool = True,
        compress_sentiment: bool = False,
        language: str = "Simplified Chinese",
        judge_samples: int = 1) -> LeanResult:
    started = time.time()
    if brief:
        quick_llm.brevity = deep_llm.brevity = brevity_for(language)

    identity = build_instrument_context(ticker, "stock", resolve_instrument_identity(ticker))
    state: dict[str, Any] = {
        "messages": [HumanMessage(content=ticker)],
        "company_of_interest": ticker,
        "asset_type": "stock",
        "instrument_context": identity,
        "trade_date": trade_date,
        "market_report": "", "sentiment_report": "",
        "news_report": "", "fundamentals_report": "",
        "investment_debate_state": {
            "bull_history": "", "bear_history": "", "history": "",
            "current_response": "", "judge_decision": "", "count": 0,
        },
    }

    trace: list[StepTrace] = []
    rounds = 0 if mode == EvidenceMode.PREFETCH else tool_rounds

    for key in analysts:
        label, factory, report_key = ANALYSTS[key]
        step = StepTrace(node=label)
        step_started = time.time()
        quick_llm.node_label = label
        if brief:
            quick_llm.brevity = brevity_for(language, WORD_LIMITS.get(label, DEFAULT_WORDS))

        # 每个分析师从干净的消息开始——上游用 "Msg Clear" 节点做同一件事，
        # 目的是不让上一个分析师的工具返回堆进下一个的上下文。
        state["messages"] = [HumanMessage(
            content=f"为 {identity} 完成你负责的那部分分析。分析日期 {trade_date}。")]

        if mode != EvidenceMode.TOOLS:
            block, blocks = evidence.gather(key, ticker, trade_date)
            if block:
                step.has_evidence = True
                step.evidence_chars = len(block)
                step.failed_sources += [b.name for b in blocks if not b.ok]
                state["messages"].append(HumanMessage(content=block))

        if key == "sentiment":
            # 上游自取的三个来源没有 Block 可读，靠日志告警识别降级。
            step.has_evidence = True
            if compress_sentiment:
                factory = compressed_sentiment_node

        with SourceWatch() as watch:
            out = _run_node_with_tools(factory(quick_llm), state, quick_llm, rounds, step)
        step.failed_sources += [n for n in watch.failed if n not in step.failed_sources]
        state[report_key] = out.get(report_key, "") or ""
        step.seconds = time.time() - step_started
        trace.append(step)

    if brief:
        quick_llm.brevity = brevity_for(language, DEFAULT_WORDS)

    for _ in range(debate_rounds):
        for label, factory in (("Bull Researcher", create_bull_researcher),
                               ("Bear Researcher", create_bear_researcher)):
            step = StepTrace(node=label)
            step_started = time.time()
            quick_llm.node_label = label
            state.update(factory(quick_llm)(state))
            step.calls, step.seconds = 1, time.time() - step_started
            trace.append(step)

    # 判官对同一份辩论重复采样。方差集中在这一步——它要把一堆证据塌缩成一个
    # 分类标签，而上游的分析师和多空辩论只是在描述证据。重跑判官约 $0.02，
    # 重跑整条链约 $0.13，所以这是最便宜的降噪位置。
    step = StepTrace(node="Research Manager")
    step_started = time.time()
    deep_llm.node_label = "Research Manager"
    judge = create_research_manager(deep_llm)

    samples: list[tuple[str, dict]] = []
    for _ in range(max(1, judge_samples)):
        out = judge(dict(state))
        samples.append((_rating_of(out.get("investment_plan", "")), out))
        step.calls += 1
    votes = [rating for rating, _ in samples]
    winner = Counter(votes).most_common(1)[0][0]
    state.update(next(out for rating, out in samples if rating == winner))
    step.seconds = time.time() - step_started
    trace.append(step)

    debate = state["investment_debate_state"]
    return LeanResult(
        ticker=ticker, trade_date=trade_date,
        reports={k: state[ANALYSTS[k][2]] for k in analysts},
        bull=debate.get("bull_history", ""), bear=debate.get("bear_history", ""),
        plan=state.get("investment_plan", ""),
        trace=trace, seconds=time.time() - started, votes=votes,
    )
