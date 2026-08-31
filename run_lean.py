#!/usr/bin/env python
"""精简管线入口：一只票 → 一个评级 + 一段话。

    .venv/bin/python run_lean.py AMD

对照 `run_analysis.py`（上游 12 节点原貌，一只票约 17 次调用）：这里是 7 个节点
（4 分析师 → 多空辩论 → Research Manager），证据由 Python 预取，输出有硬性字数
上限。两条路共存，用哪条自己选。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from lean.env import load_env  # noqa: E402

load_env()
RESULTS = REPO_ROOT / "local-data" / "tradingagents"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TradingAgents 精简管线（本地 claude CLI）")
    p.add_argument("ticker")
    p.add_argument("--date", default=date.today().isoformat(), help="分析日期，默认今天")
    p.add_argument("--model", default="sonnet", help="全部节点用的模型，默认 sonnet")
    p.add_argument("--deep", default="opus",
                   help="只给 Research Manager 用的模型，默认 opus。实测判官是全链方差"
                        "最大的一步，opus 在同一份辩论上 5/5 一致，sonnet 只有 4/5")
    p.add_argument("--evidence", choices=("prefetch", "hybrid", "tools"), default="prefetch",
                   help="prefetch=Python 取完不给工具（默认）｜hybrid=不推荐，实测 24 个"
                        "分析师节点零工具调用、不带来任何新证据，只多付 15-30%% 成本并降低"
                        "评级一致性｜tools=上游原貌")
    p.add_argument("--tool-rounds", type=int, default=2,
                   help="hybrid/tools 模式下允许的工具轮次上限，默认 2")
    p.add_argument("--analysts", default="market,sentiment,news,fundamentals")
    p.add_argument("--rounds", type=int, default=1, help="多空辩论轮数")
    p.add_argument("--long", action="store_true",
                   help="取消所有节点的字数上限。不是调节字数的正确工具：实测增量只有 5%% "
                        "落在起作用的 fundamentals，33%% 被 sentiment 吃掉，单票 9.8 分钟 / "
                        "$0.82。要调字数改 lean/pipeline.py 的 WORD_LIMITS")
    p.add_argument("--save", action="store_true", help="把各节点原文落盘")
    p.add_argument("--judge-samples", type=int, default=3,
                   help="对同一份辩论重复采样判官这么多次，取众数。单次评级方差很大，"
                        "每多一次约 +$0.02")
    p.add_argument("--lang", default="Simplified Chinese",
                   help="输出语言，默认简体中文；同时写进上游的 output_language")
    p.add_argument("--full-sentiment", dest="compress_sentiment", action="store_false",
                   help="改用上游原版情绪分析师。默认走压缩版：同样三个来源、同样输出 "
                        "schema，但实测输入省 59%%、成本省 35%%，NVDA 三轮评级不变")
    p.set_defaults(compress_sentiment=True)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    ticker = args.ticker.strip().upper()

    from tradingagents.dataflows.config import set_config
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.llm_clients import create_llm_client
    from tradingagents.llm_clients.claude_cli_client import (
        ClaudeCLIError,
        RunLedger,
        check_no_api_key,
    )

    from lean import pipeline
    from lean.render import verdict

    try:
        check_no_api_key()
    except ClaudeCLIError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    config = DEFAULT_CONFIG.copy()
    config["results_dir"] = str(RESULTS / "logs")
    config["data_cache_dir"] = str(RESULTS / "cache")
    # 不要调低 news_article_limit。上游是先向 yfinance 要最近 N 条、再按日期窗口
    # 过滤（yfinance_news.py:111），N 太小时最新的几条全部落在窗口之后，过滤完
    # 一条不剩，返回 "No news found" —— 看起来像"本周无新闻"，实际是配置把它藏了。
    # 实测 AMD 2026-08-21~08-28：limit=8 得到 0 条，limit=20 得到 3,704 字符。
    # 控制体积改在证据层按字符截断（lean/evidence.py::news_block）。
    config["output_language"] = args.lang
    set_config(config)

    ledger = RunLedger()
    quick = create_llm_client(provider="claude_cli", model=args.model).get_llm()
    deep = (quick if not args.deep or args.deep == args.model
            else create_llm_client(provider="claude_cli", model=args.deep).get_llm())
    for llm in {id(quick): quick, id(deep): deep}.values():
        llm.ledger, llm.verbose_cli = ledger, True

    analysts = tuple(a.strip() for a in args.analysts.split(",") if a.strip())
    from lean.env import credential_report
    print(f"\n分析 {ticker}（{args.date}）｜{'+'.join(analysts)}"
          f"｜证据 {args.evidence}｜模型 {args.model}"
          f"{'' if deep is quick else ' + ' + args.deep}")
    print(f"凭证 {credential_report()}\n")

    started = time.time()
    result = pipeline.run(
        ticker, args.date, quick, deep,
        analysts=analysts, mode=args.evidence, tool_rounds=args.tool_rounds,
        debate_rounds=args.rounds, brief=not args.long,
        compress_sentiment=args.compress_sentiment, language=args.lang,
        judge_samples=args.judge_samples,
    )
    print(verdict(result, ledger))

    if args.save:
        out = RESULTS / "lean" / f"{ticker}_{time.strftime('%Y%m%d_%H%M%S')}"
        out.mkdir(parents=True, exist_ok=True)
        for key, text in result.reports.items():
            (out / f"{key}.md").write_text(text, encoding="utf-8")
        (out / "bull.md").write_text(result.bull, encoding="utf-8")
        (out / "bear.md").write_text(result.bear, encoding="utf-8")
        (out / "plan.md").write_text(result.plan, encoding="utf-8")
        print(f"  各节点原文  {out}")
    print(f"  实际墙上时间 {time.time() - started:.0f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
