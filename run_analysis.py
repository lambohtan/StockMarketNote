#!/usr/bin/env python
"""跑一只股票：输入代码，输出结论。

    .venv/bin/python run_analysis.py NVDA

这是上游 TradingAgents 的原始编排（4 分析师 → 多空辩论 → Research Manager →
Trader → 三方风险辩论 → Portfolio Manager），一个字没删。唯一的改动是 LLM 后端
换成本地 claude CLI（见 vendor/TradingAgents/tradingagents/llm_clients/claude_cli_client.py）。

完整的分节报告照原样落盘；终端只打最核心的几行。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from lean.env import load_env  # noqa: E402

load_env()
RESULTS = REPO_ROOT / "local-data" / "tradingagents"

from lean.render import field, summarize, usage_table  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TradingAgents on the local claude CLI")
    p.add_argument("ticker", help="股票代码，例如 NVDA")
    p.add_argument("--date", default=date.today().isoformat(),
                   help="分析日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--deep", default="opus",
                   help="deep-think 模型（Research Manager / Portfolio Manager），默认 opus")
    p.add_argument("--quick", default="sonnet",
                   help="quick-think 模型（分析师 / 辩论），默认 sonnet")
    p.add_argument("--analysts", default="market,social,news,fundamentals",
                   help="逗号分隔，可选 market,social,news,fundamentals")
    p.add_argument("--rounds", type=int, default=1, help="多空辩论轮数")
    p.add_argument("--risk-rounds", type=int, default=1, help="风险辩论轮数")
    p.add_argument("--full", action="store_true", help="终端也打完整报告，不只打摘要")
    p.add_argument("--fast", action="store_true",
                   help="提速档：分析师和辩论用 haiku，两个 manager 用 sonnet")
    args = p.parse_args()
    if args.fast:
        # 只在用户没有显式指定时才覆盖，--fast --deep opus 仍然听后者的。
        if "--quick" not in sys.argv:
            args.quick = "haiku"
        if "--deep" not in sys.argv:
            args.deep = "sonnet"
    return args


def main() -> int:
    args = parse_args()
    ticker = args.ticker.strip().upper()

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.llm_clients.claude_cli_client import (
        ClaudeCLIError,
        RunLedger,
        check_no_api_key,
    )

    try:
        check_no_api_key()
    except ClaudeCLIError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    if not shutil.which(args_bin := "claude") and not Path.home().joinpath(".local/bin/claude").exists():
        print(f"找不到 {args_bin} CLI；设置 CLAUDE_CLI_BIN 指向它的绝对路径。", file=sys.stderr)
        return 2

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = "claude_cli"
    config["deep_think_llm"] = args.deep
    config["quick_think_llm"] = args.quick
    config["max_debate_rounds"] = args.rounds
    config["max_risk_discuss_rounds"] = args.risk_rounds
    config["results_dir"] = str(RESULTS / "logs")
    config["data_cache_dir"] = str(RESULTS / "cache")
    config["memory_log_path"] = str(RESULTS / "memory" / "trading_memory.md")

    analysts = tuple(a.strip() for a in args.analysts.split(",") if a.strip())

    print(f"\n分析 {ticker}（{args.date}）"
          f"｜分析师 {'+'.join(analysts)}｜deep={args.deep} quick={args.quick}")
    print("每行 [claude-cli] 是一次模型调用；整轮大约十几次，请等几分钟。\n")

    graph = TradingAgentsGraph(selected_analysts=analysts, debug=False, config=config)
    ledger = RunLedger()
    for llm in (graph.deep_thinking_llm, graph.quick_thinking_llm):
        llm.verbose_cli = True
        llm.ledger = ledger

    started = time.time()
    state, rating = graph.propagate(ticker, args.date)
    elapsed = time.time() - started

    report = graph.save_reports(state, ticker)
    print(summarize(state, ticker, args.date, rating, elapsed, report.parent))
    print(usage_table(ledger))

    if args.full:
        print(report.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
