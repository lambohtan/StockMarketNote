"""压缩版 sentiment 分析师。

上游的 sentiment analyst 已经是预取式的（不走 tool 循环），贵在两处：把 30 条
StockTwits 原文和 Reddit 正文摘录整段塞进 prompt，外加约 1,000 token 的"分析
最佳实践"说明。实测三个数据块 5,000 字符里，真正携带信号的是 StockTwits 顶部
那行多空计数——其余多是 `$NVDA` 这种无内容消息。

这里保留**同一个输出 schema**（`SentimentReport`），只压缩喂进去的量：多空计数原样
保留，带标签的消息留 6 条。输出形状与上游完全一致，所以下游的多空辩论和 Research
Manager 读到的东西是同构的，可对照。

**Reddit 当前已停用**（2026-08-31）：该数据段正在迁移到独立的预抓取管线
（`lean/reddit_prefetch.py`），本模块暂时不读它。停用期间返回的是一个显式标记而不是
空串，理由见 `_reddit_block`。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.utils.agent_utils import get_news
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages

MAX_LABELLED = 6
MAX_REDDIT_TITLES = 6


def _compress_stocktwits(block: str) -> str:
    """保留计数行 + 前 6 条**带多空标签**的消息，丢掉无标签噪声。"""
    lines = block.splitlines()
    header = next((ln for ln in lines if ln.startswith("Bullish:")), "")
    labelled = [ln for ln in lines
                if ("· Bullish]" in ln or "· Bearish]" in ln)][:MAX_LABELLED]
    if not header and not labelled:
        return block[:400]
    parts = [header] if header else []
    parts += labelled
    parts.append(f"（无标签消息已略去；完整样本 {sum(1 for ln in lines if ln.startswith('['))} 条）")
    return "\n".join(parts)


#: Reddit 停用期间注入的占位文本。**不是空串**——空串会让情绪分析师写出"社区无人
#: 讨论"，把"我们没去取"变成"市场没有声音"，那正是 `CLAUDE.md` 第 3 条禁止的
#: "缺失数据变成空成功"。上游 `dataflows/reddit.py` 的失败分支当初就是为这件事改的
#: （见 vendor/README.md），停用分支不能倒退回去。措辞对齐 `load_cached_reddit`
#: 的缓存缺失分支，接线前后分析师看到的形状一致。
REDDIT_DISABLED_NOTE = (
    "<REDDIT DISABLED — 本次运行没有读取 Reddit。该数据段正在迁移到独立的预抓取"
    "管线（lean/reddit_prefetch.py），尚未接线。这不是「没人讨论 {ticker}」的证据；"
    "请把该来源当作不可用，并据此降低 confidence。>"
)


def _reddit_block(ticker: str, trade_date: str) -> str:
    """Reddit 的接线点。预抓取管线就绪后，把函数体换成：

        from .reddit_prefetch import load_cached_reddit
        return load_cached_reddit(ticker, trade_date).text

    `load_cached_reddit` 只读 SQLite、不发网络请求，缓存缺失或不可读时自己会返回带
    降级说明的文本，形状与这里的占位一致，所以换过去不需要改调用方。

    这里同时打一条 warning：`pipeline.SourceWatch` 靠 logger 名字末段识别降级来源，
    发出来终端的证据健康行才会显示 `Sentiment✗（Reddit 降级）`。不打的话停用就是
    静默的，而静默的降级正是这条管线最该避免的东西。
    """
    logging.getLogger("tradingagents.dataflows.reddit").warning(
        "Reddit 读取已停用，等待预抓取管线接线（ticker=%s trade_date=%s）",
        ticker, trade_date)
    return REDDIT_DISABLED_NOTE.format(ticker=ticker.upper())


def _compress_reddit(block: str) -> str:
    """只留标题行，丢掉 body excerpt —— 正文摘录占了这块的大部分体积。

    **当前未被调用**：Reddit 走 `_reddit_block` 的停用桩。保留是因为预抓取管线接线后
    可能仍需要压缩它的快照文本；接线时确认不需要再删。
    """
    out = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("body excerpt:"):
            continue
        if stripped:
            out.append(line)
        if len(out) > MAX_REDDIT_TITLES * 2:
            break
    return "\n".join(out) if out else block[:300]


INSTRUCTIONS = """你是市场情绪分析师。下面三个来源的数据已经取好，只根据它们作判断。

<news>
{news}
</news>

<stocktwits>
{stocktwits}
</stocktwits>

<reddit>
{reddit}
</reddit>

判断口径：
1. StockTwits 的多空比是零售情绪的领先信号，但样本量决定可信度——按实际条数而不是百分比下结论。
2. 来源之间的背离本身就是信号（新闻偏空而散户偏多，反之亦然）。
3. 区分事件与观点：新闻标题是事件，社交帖是观点，权重不同。
4. 任何来源返回失败或占位符时，必须在 confidence 里降级并在 narrative 里写明。

填写 overall_band / overall_score / confidence / narrative 四个字段。"""


def compressed_sentiment_node(llm):
    """返回一个与上游 sentiment analyst 签名兼容的节点函数。"""
    structured = bind_structured(llm, SentimentReport, "Sentiment Analyst")

    def node(state):
        ticker = state["company_of_interest"]
        end = state["trade_date"]
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")

        news = get_news.func(ticker, start, end)
        stocktwits = _compress_stocktwits(fetch_stocktwits_messages(ticker, limit=30))
        reddit = _reddit_block(ticker, end)

        prompt = INSTRUCTIONS.format(news=news, stocktwits=stocktwits, reddit=reddit)
        text = invoke_structured_or_freetext(
            structured, llm, prompt, render_sentiment_report, "Sentiment Analyst")
        from langchain_core.messages import AIMessage
        return {"messages": [AIMessage(content=text)], "sentiment_report": text}

    return node
