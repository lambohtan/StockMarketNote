"""Python 侧的确定性证据预取。

为什么要有这一层
----------------
上游让分析师自己调工具，每多一轮，之前所有的工具返回都要重新发一遍。实测
market analyst 第四轮的输入是 43,728 token，而它真正需要的事实不到 500 token。

这里把同一批事实一次性取好、压缩好，交给分析师。数据源和上游完全一致（都走
`tradingagents.dataflows`），只是取数的决定权从 LLM 移回 Python —— 上游自己的
sentiment analyst 就是这么做的，它的 docstring 写明了理由：给了 tool 压力却
工具不够时，LLM 会编造数据。

每个 fetcher 失败时返回一段写明失败原因的文本，绝不返回空字符串假装成功。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from tradingagents.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_indicators,
    get_news,
    get_stock_data,
)

# 上游 market analyst 让 LLM 从 12 个指标里挑 8 个。预取模式下 Python 全算，
# 但只交出**最新值**——分析师要的是当前状态，不是 30 天的逐日序列。
INDICATORS = (
    "close_50_sma", "close_200_sma", "close_10_ema",
    "macd", "macds", "rsi", "boll_ub", "boll_lb", "atr", "vwma",
)


@dataclass
class Block:
    """一份证据，附带它是否真的取到了。"""

    name: str
    text: str
    ok: bool

    def render(self) -> str:
        status = "" if self.ok else "  【取数失败，下面是失败原因，不是数据】"
        return f"--- {self.name}{status} ---\n{self.text.strip()}"


def _safe(name: str, fn) -> Block:
    try:
        text = str(fn())
    except Exception as exc:                      # noqa: BLE001 - 任何来源故障都要可见
        return Block(name, f"{type(exc).__name__}: {exc}", ok=False)
    if not text.strip():
        return Block(name, "来源返回空内容", ok=False)
    return Block(name, text, ok=True)


def _days_before(curr_date: str, days: int) -> str:
    return (datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")


def _price_digest(csv_text: str) -> str:
    """把上百行 OHLCV 压成一段摘要 + 最近 5 根 K 线。

    上游把 145 行原始 CSV（约 2,500 token）整个塞进 prompt。分析师读它是为了
    判断趋势位置和波动，那些信息用几个统计量就能表达完；逐日明细只在需要
    "某天发生了什么" 时才有价值，所以保留最近 5 根。
    """
    body = "\n".join(ln for ln in csv_text.splitlines() if not ln.startswith("#"))
    frame = pd.read_csv(io.StringIO(body))
    if frame.empty:
        return "价格数据为空"

    close = frame["Close"]
    latest = close.iloc[-1]

    def change(n: int) -> str:
        if len(close) <= n:
            return "unknown"
        return f"{(latest / close.iloc[-1 - n] - 1) * 100:+.1f}%"

    window_high, window_low = frame["High"].max(), frame["Low"].min()
    lines = [
        f"区间: {frame.iloc[0, 0]} 至 {frame.iloc[-1, 0]}，共 {len(frame)} 个交易日",
        f"最新收盘: {latest:.2f}",
        f"涨跌: 5日 {change(5)}｜20日 {change(20)}｜60日 {change(60)}",
        f"区间高/低: {window_high:.2f} / {window_low:.2f}"
        f"（最新价位于区间 {(latest - window_low) / max(window_high - window_low, 1e-9) * 100:.0f}% 分位）",
        f"平均成交量: {frame['Volume'].mean():,.0f}，最近一日 {frame['Volume'].iloc[-1]:,.0f}",
        "",
        "最近 5 个交易日:",
        frame.tail(5).to_string(index=False),
    ]
    return "\n".join(lines)


def _latest_indicator(block_text: str) -> str:
    """从 `## rsi values from ...` 里取最新一行的值。"""
    for line in block_text.splitlines():
        if ":" in line and not line.startswith("#"):
            date, _, value = line.partition(":")
            try:
                return f"{float(value.strip()):.2f}（{date.strip()}）"
            except ValueError:
                continue
    return "unknown"


def market_block(ticker: str, curr_date: str, lookback_days: int = 180) -> list[Block]:
    start = _days_before(curr_date, lookback_days)
    price = _safe("价格与成交量", lambda: _price_digest(
        get_stock_data.func(ticker, start, curr_date)))

    rows = []
    for name in INDICATORS:
        try:
            rows.append(f"{name}: {_latest_indicator(get_indicators.func(ticker, name, curr_date, 10))}")
        except Exception as exc:                  # noqa: BLE001
            rows.append(f"{name}: 取数失败 {type(exc).__name__}")
    failed = sum(1 for r in rows if "取数失败" in r)
    indicators = Block("技术指标（最新值）", "\n".join(rows), ok=failed < len(rows))
    return [price, indicators]


#: 从三张季度报表里取出的行。yfinance 对同一概念有多种命名，按顺序回退。
_STATEMENT_ROWS = (
    ("营业收入", ("Total Revenue", "Operating Revenue")),
    ("毛利", ("Gross Profit",)),
    ("营业利润", ("Operating Income", "Total Operating Income As Reported")),
    ("净利润", ("Net Income", "Net Income From Continuing Operation Net Minority Interest")),
    ("自由现金流", ("Free Cash Flow",)),
    ("经营现金流", ("Operating Cash Flow",)),
    ("资本开支", ("Capital Expenditure",)),
    ("股票回购", ("Repurchase Of Capital Stock",)),
    ("总负债", ("Total Debt",)),
    ("股东权益", ("Stockholders Equity", "Total Equity Gross Minority Interest")),
)


def _read_statement(text: str) -> pd.DataFrame:
    body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
    return pd.read_csv(io.StringIO(body), index_col=0)


def _pick(frame: pd.DataFrame, names: tuple[str, ...]):
    for name in names:
        if name in frame.index:
            return frame.loc[name]
    return None


def _statements_digest(ticker: str, curr_date: str) -> str:
    """三张季度报表 → 关键行 + Python 算好的同比。

    上游让分析师自己调 income/cashflow/balance 三个工具，拿回 3,700 token 的
    原始表，再让模型口算同比。这里只留下真正会被引用的十行，增长率由 pandas 算，
    因为确定性数值不该由 LLM 产生（仓库硬约束）。
    """
    frames = {}
    for label, fn in (("income", get_income_statement), ("cash", get_cashflow),
                      ("balance", get_balance_sheet)):
        frames[label] = _read_statement(fn.func(ticker, "quarterly", curr_date))

    merged = pd.concat(frames.values())
    columns = list(merged.columns)
    if len(columns) < 2:
        return "季度报表列数不足，无法计算同比"
    latest = columns[0]
    # 季度表按时间倒序，第 5 列即去年同期；不足 5 列时退到最早一列并标注。
    yoy_index = 4 if len(columns) > 4 else len(columns) - 1
    prior = columns[yoy_index]

    def money(value) -> str:
        if pd.isna(value):
            return "unknown"
        return f"{value / 1e8:,.0f}亿"

    rows = [f"（单位：亿美元；最新季 {latest}，同期 {prior}）"]
    revenue_now = revenue_then = None
    for label, names in _STATEMENT_ROWS:
        series = _pick(merged, names)
        if series is None:
            rows.append(f"{label}: unknown（报表中无此行）")
            continue
        now, then = series.get(latest), series.get(prior)
        if label == "营业收入":
            revenue_now, revenue_then = now, then
        growth = ""
        if pd.notna(now) and pd.notna(then) and then not in (0,):
            growth = f"  同比 {(now / then - 1) * 100:+.1f}%"
        rows.append(f"{label}: {money(now)}（同期 {money(then)}）{growth}")

    if revenue_now and pd.notna(revenue_now):
        for label, names in (("毛利率", ("Gross Profit",)),
                             ("营业利润率", ("Operating Income",)),
                             ("净利率", ("Net Income",))):
            series = _pick(merged, names)
            if series is None:
                continue
            now, then = series.get(latest), series.get(prior)
            if pd.notna(now) and pd.notna(then) and pd.notna(revenue_then):
                rows.append(f"{label}: {now / revenue_now * 100:.1f}%"
                            f"（同期 {then / revenue_then * 100:.1f}%）")
    return "\n".join(rows)


#: 快照里这些比率字段在原始输出里是**裸数字**，而 yfinance 对它们用了三种不同
#: 口径。实测后果：AMD 的 `Debt to Equity: 6.361` 被分析师读成 6.36 倍杠杆并
#: 写进空方论据，真实值是 0.064 倍（全样本最低）；NVDA 的同一个 16.971 在不同
#: 运行里被读出"几乎零杠杆"和"高杠杆"两个相反结论。单位换算是确定性计算，
#: 按仓库硬约束（CLAUDE.md 第 2 条）不该留给 LLM 猜。
#:
#: 三种口径已逐个核对（用 totalDebt/Equity 与真实股息率反算）：
#:   fraction  —— 小数表示的比率，×100 才是百分比（AAPL ROE 1.4875 = 148.75%）
#:   percent   —— 本来就是百分数，只补一个 %（KO Dividend Yield 2.36 = 2.36%）
#:   pct_ratio —— 百分数表示的倍数关系，百分比和倍数都给，避免再被读错量纲
_SNAPSHOT_UNITS = {
    "Profit Margin": "fraction",
    "Operating Margin": "fraction",
    "Return on Equity": "fraction",
    "Return on Assets": "fraction",
    "Dividend Yield": "percent",
    "Debt to Equity": "pct_ratio",
}


def _normalize_units(text: str) -> str:
    """给快照里的比率字段补上单位。认不出的行原样保留，不猜、不丢。"""
    out = []
    for line in text.splitlines():
        label, sep, raw = line.partition(":")
        kind = _SNAPSHOT_UNITS.get(label.strip()) if sep else None
        if kind is None:
            out.append(line)
            continue
        try:
            value = float(raw.strip())
        except ValueError:
            out.append(line)          # 来源给了非数值，保留原文让它可见
            continue
        if kind == "fraction":
            out.append(f"{label}: {value * 100:.2f}%")
        elif kind == "percent":
            out.append(f"{label}: {value:.2f}%")
        else:
            out.append(f"{label}: {value:.2f}%"
                       f"（负债/权益 = {value / 100:.3f} 倍）")
    return "\n".join(out)


def _eps_expectation(text: str) -> str:
    """Forward EPS 相对 TTM EPS 的倍数——Forward PE 便不便宜的全部前提。

    实测 AMD：Forward PE 30.1 看着温和，但它成立的前提是 EPS 从 3.91 涨到
    15.45，即 3.95 倍。180 字上限下的分析师从没做过这步除法，多空辩论因此整个
    缺了这条主线；把字数放开后它才出现，并直接改变了评级方向。除法是确定性
    计算，不该取决于分析师有没有字数、想不想得到。
    """
    values: dict[str, float] = {}
    for line in text.splitlines():
        label, sep, raw = line.partition(":")
        if sep and label.strip() in ("EPS (TTM)", "Forward EPS"):
            try:
                values[label.strip()] = float(raw.strip())
            except ValueError:
                pass

    ttm, forward = values.get("EPS (TTM)"), values.get("Forward EPS")
    if ttm is None or forward is None:
        return "预期 EPS 倍数: unknown（快照缺 EPS (TTM) 或 Forward EPS）"
    if ttm <= 0:
        # 亏损公司的倍数没有意义，写清楚原因，不要输出一个负数让人当增长率读。
        return (f"预期 EPS 倍数: unknown（TTM EPS {ttm:.2f} 非正，倍数无意义；"
                f"Forward EPS {forward:.2f}）")
    return (f"预期 EPS 倍数: Forward EPS {forward:.2f} / TTM EPS {ttm:.2f} = "
            f"{forward / ttm:.2f} 倍（Forward PE 要成立，EPS 必须涨到这个水平）")


def _snapshot_digest(ticker: str, curr_date: str) -> str:
    """上游快照 + 单位归一 + Python 算好的预期 EPS 倍数。"""
    raw = get_fundamentals.func(ticker, curr_date)
    return f"{_normalize_units(raw)}\n{_eps_expectation(raw)}"


def fundamentals_block(ticker: str, curr_date: str) -> list[Block]:
    """快照 + 报表摘要。

    只给快照会丢掉增长轨迹：实测同一只票，仅有 28 项快照时分析师给不出任何同比
    数字，多方论据塌掉，最终评级从 Overweight 落到 Hold。增长率是这个判断的主要
    依据，必须进证据集。
    """
    return [
        _safe("基本面快照", lambda: _snapshot_digest(ticker, curr_date)),
        _safe("季度报表摘要", lambda: _statements_digest(ticker, curr_date)),
    ]


#: `get_news` 在窗口内无文章时返回的定型句。它读起来像"那段时间没有新闻"，
#: 但更常见的原因是 yfinance 只提供最近若干篇——报道量大的票（如 NVDA）最近
#: 50 篇连两天都覆盖不到，任何历史窗口都必然为空。两种情况必须区分。
_NO_NEWS = "no news found"


def _news_coverage_note(ticker: str) -> str:
    """探一次原始新闻源，说明它实际覆盖到哪个日期。"""
    try:
        import yfinance as yf
        raw = yf.Ticker(ticker).get_news(count=50)
    except Exception as exc:                      # noqa: BLE001
        return f"（无法探测来源覆盖范围：{type(exc).__name__}）"
    if not raw:
        return "（来源当前一篇文章都没有返回）"
    stamps = []
    for article in raw:
        content = article.get("content", article)
        stamp = content.get("pubDate") or article.get("providerPublishTime")
        if stamp:
            stamps.append(str(stamp)[:10])
    if not stamps:
        return f"（来源返回 {len(raw)} 篇但都没有时间戳）"
    return (f"（来源当前只提供 {len(raw)} 篇，最早一篇是 {min(stamps)}；"
            f"它覆盖不到你要的窗口，这不等于当时没有新闻）")


def news_block(ticker: str, curr_date: str, days: int = 7,
               max_chars: int = 3000) -> list[Block]:
    start = _days_before(curr_date, days)
    block = _safe("个股新闻", lambda: get_news.func(ticker, start, curr_date))

    if block.ok and _NO_NEWS in block.text.lower():
        # 窗口内没有文章：把它标成来源不可用，并说清为什么，免得分析师把
        # "查不到" 当成 "确实没有消息面驱动" 写进结论。
        return [Block(block.name,
                      f"{block.text.strip()}\n{_news_coverage_note(ticker)}",
                      ok=False)]

    if block.ok and len(block.text) > max_chars:
        block = Block(block.name,
                      block.text[:max_chars] + f"\n…（已截断，原文 {len(block.text)} 字符）",
                      ok=True)
    return [block]


#: 分析师 key -> 该分析师需要的证据。sentiment 不在其中：上游的 sentiment
#: analyst 本来就自己预取 news/StockTwits/Reddit 且不使用工具，无需重复。
FETCHERS = {
    "market": market_block,
    "fundamentals": fundamentals_block,
    "news": news_block,
}


def gather(analyst: str, ticker: str, curr_date: str) -> tuple[str, list[Block]]:
    """取回某个分析师的证据，返回 (可直接进 prompt 的文本, 明细)。"""
    fetch = FETCHERS.get(analyst)
    if fetch is None:
        return "", []
    blocks = fetch(ticker, curr_date)
    header = (
        f"=== 已为你取好的 {ticker} 事实（截至 {curr_date}）===\n"
        "下面是 Python 确定性取得的数据。你引用的每个数字都必须出自这里；"
        "这里没有的，写 unknown，不要推测。\n\n"
    )
    return header + "\n\n".join(b.render() for b in blocks), blocks
