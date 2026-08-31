#!/usr/bin/env python
"""同配置重复跑同一只票，统计评级分布。

单次运行给出的评级是一次采样，不是这只票的"答案"。早筛要靠它做决定之前，得先
知道重跑会不会翻——这个脚本就是量这件事的。它不改任何逻辑，只是把 pipeline 跑
N 次并计票。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lean.env import load_env  # noqa: E402

load_env()
RESULTS = REPO_ROOT / "local-data" / "tradingagents"


def main() -> int:
    ap = argparse.ArgumentParser(description="评级稳定性测试")
    ap.add_argument("ticker")
    ap.add_argument("-n", type=int, default=5, help="重复次数")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--deep", default=None)
    ap.add_argument("--evidence", default="prefetch")
    ap.add_argument("--compress-sentiment", action="store_true")
    args = ap.parse_args()

    from tradingagents.dataflows.config import set_config
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.llm_clients import create_llm_client
    from tradingagents.llm_clients.claude_cli_client import RunLedger

    from lean import pipeline
    from lean.render import field

    config = DEFAULT_CONFIG.copy()
    config["results_dir"] = str(RESULTS / "logs")
    config["data_cache_dir"] = str(RESULTS / "cache")
    config["news_article_limit"] = 8
    config["output_language"] = "Simplified Chinese"
    set_config(config)

    ticker = args.ticker.upper()
    deep_name = args.deep or args.model
    print(f"\n{ticker} · {args.date} · 重复 {args.n} 次"
          f"｜证据 {args.evidence}｜分析师 {args.model}｜判官 {deep_name}"
          f"{'｜压缩情绪' if args.compress_sentiment else ''}\n")

    ratings, costs, seconds = [], [], []
    for i in range(1, args.n + 1):
        ledger = RunLedger()
        quick = create_llm_client(provider="claude_cli", model=args.model).get_llm()
        deep = quick if deep_name == args.model else \
            create_llm_client(provider="claude_cli", model=deep_name).get_llm()
        for llm in {id(quick): quick, id(deep): deep}.values():
            llm.ledger = ledger

        started = time.time()
        result = pipeline.run(ticker, args.date, quick, deep, mode=args.evidence,
                              compress_sentiment=args.compress_sentiment)
        elapsed = time.time() - started

        rating = (field(result.plan, "Recommendation") or "未产出").splitlines()[0].strip()
        ratings.append(rating)
        costs.append(ledger.total("cost_usd"))
        seconds.append(elapsed)
        degraded = sorted({n for st in result.trace for n in st.failed_sources})
        note = f"  ⚠ {'/'.join(degraded)} 降级" if degraded else ""
        print(f"  第 {i} 次  {rating:<12} {elapsed:5.0f}s  ${costs[-1]:.2f}"
              f"  输入{ledger.total('input_tokens')+ledger.total('cache_read')+ledger.total('cache_write'):>7,.0f}"
              f"  输出{ledger.total('output_tokens'):>6,.0f}{note}")

    print()
    tally = Counter(ratings)
    for rating, count in tally.most_common():
        print(f"  {rating:<12} {count}/{args.n}  {'█' * count}")
    print(f"\n  众数占比 {max(tally.values()) / args.n * 100:.0f}%"
          f"｜不同结果 {len(tally)} 种"
          f"｜合计 ${sum(costs):.2f}，平均 {sum(seconds) / len(seconds):.0f}s/次")
    if len(tally) > 1:
        print("  ⚠ 同一配置出现了不同评级：单次结果不能当作这只票的定论。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
