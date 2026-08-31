#!/usr/bin/env python
"""Scheduler-facing CLI for the Reddit evidence prefetch stage.

Examples:

    .venv/bin/python prefetch_reddit.py --pool-file today-pool.json
    .venv/bin/python prefetch_reddit.py AMD NVDA --dry-run

The command performs no LLM calls.  It exits 0 when every ticker completed
without a fetch degradation, 1 when usable partial results or failures were
recorded, and 2 for invalid input/configuration.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from lean.reddit_prefetch import (  # noqa: E402
    DEFAULT_CACHE_PATH,
    DEFAULT_RATE_LOCK,
    DEFAULT_RATE_STATE,
    DEFAULT_SUBREDDITS,
    PoolInputError,
    RedditCache,
    RedditRSSClient,
    load_pool,
    prefetch_reddit,
    utc_now,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="预抓取股票池的 Reddit 帖子和评论，只写本地缓存，不调用 LLM"
    )
    parser.add_argument("tickers", nargs="*", help="可直接传 ticker；也可使用 --pool-file")
    parser.add_argument("--pool-file", type=Path, help="JSON、逐行或逗号分隔的股票池文件")
    parser.add_argument(
        "--as-of",
        help="证据截止时间（ISO-8601，默认当前 UTC）；定时任务建议显式传入",
    )
    parser.add_argument(
        "--analysis-date",
        help="这批数据归属的本地分析日（YYYY-MM-DD，默认运行机本地日期）",
    )
    parser.add_argument("--window-hours", type=int, default=168, help="RSS 原始数据窗口，默认一周（168 小时）")
    parser.add_argument(
        "--comments-per-post", type=int, default=20,
        help="每篇帖子单次 RSS 最多请求多少评论，默认 20、最大 500",
    )
    parser.add_argument(
        "--min-interval-seconds", type=float, default=30.0,
        help="所有进程共享的 Reddit 最小请求间隔，默认 30 秒",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="单次 RSS 请求超时")
    parser.add_argument(
        "--thread-cache-hours", type=float, default=168.0,
        help="相同帖子原始评论缓存复用时间，默认一周",
    )
    parser.add_argument(
        "--subreddits", default=",".join(DEFAULT_SUBREDDITS),
        help="逗号分隔的 subreddit，默认 wallstreetbets,stocks,investing",
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH, help="SQLite 缓存路径")
    parser.add_argument("--rate-lock", type=Path, default=DEFAULT_RATE_LOCK, help=argparse.SUPPRESS)
    parser.add_argument("--rate-state", type=Path, default=DEFAULT_RATE_STATE, help=argparse.SUPPRESS)
    parser.add_argument("--retention-days", type=int, default=7, help="清理多少天前的 Reddit 原始缓存，默认 7")
    parser.add_argument("--json-summary", action="store_true", help="完成后输出机器可读 JSON")
    parser.add_argument(
        "--force", action="store_true",
        help="即使相同 --as-of 的快照已完成也重新抓取；默认用于断点恢复时跳过已完成 ticker",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只校验股票池并估算最坏请求数/时间，不联网、不写缓存",
    )
    return parser.parse_args(argv)


def _parse_as_of(raw: str | None) -> datetime:
    if not raw:
        return utc_now()
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PoolInputError(f"invalid --as-of value: {raw!r}") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_analysis_date(raw: str | None) -> str:
    if not raw:
        return datetime.now().astimezone().date().isoformat()
    try:
        value = datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise PoolInputError(f"invalid --analysis-date value: {raw!r}") from exc
    if value != raw:
        raise PoolInputError(f"invalid --analysis-date value: {raw!r}")
    return value


def _validate_args(args: argparse.Namespace) -> None:
    if args.window_hours < 1:
        raise PoolInputError("--window-hours must be positive")
    if not 1 <= args.comments_per_post <= 500:
        raise PoolInputError("--comments-per-post must be between 1 and 500")
    if args.min_interval_seconds < 0:
        raise PoolInputError("--min-interval-seconds must be non-negative")
    if args.retention_days < 1:
        raise PoolInputError("--retention-days must be positive")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        _validate_args(args)
        pool = load_pool(args.pool_file, args.tickers)
        as_of = _parse_as_of(args.as_of)
        analysis_date = _parse_analysis_date(args.analysis_date)
        subreddits = tuple(
            dict.fromkeys(sub.strip().lower() for sub in args.subreddits.split(",") if sub.strip())
        )
        if not subreddits:
            raise PoolInputError("--subreddits cannot be empty")
        if any(not re.fullmatch(r"[A-Za-z0-9_]{1,21}", sub) for sub in subreddits):
            raise PoolInputError("--subreddits contains an invalid subreddit name")
    except PoolInputError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2

    maximum_posts_per_ticker = len(subreddits) * 100
    maximum_requests = len(pool) * (len(subreddits) + maximum_posts_per_ticker)
    maximum_seconds = max(0, maximum_requests - 1) * args.min_interval_seconds
    if args.dry_run:
        summary = {
            "tickers": [item.ticker for item in pool],
            "ticker_count": len(pool),
            "maximum_requests": maximum_requests,
            "estimated_maximum_seconds": maximum_seconds,
            "maximum_search_results_per_ticker": maximum_posts_per_ticker,
            "selection_deferred": True,
            "analysis_date": analysis_date,
            "as_of": as_of.isoformat(),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    def progress(message: str) -> None:
        print(
            f"[{datetime.now().astimezone().strftime('%H:%M:%S')}] {message}",
            file=sys.stderr,
            flush=True,
        )

    try:
        cache = RedditCache(args.cache)
        cache.purge(before=utc_now() - timedelta(days=args.retention_days))
        client = RedditRSSClient(
            min_interval_seconds=args.min_interval_seconds,
            timeout=args.timeout,
            lock_path=args.rate_lock,
            state_path=args.rate_state,
        )
        report = prefetch_reddit(
            pool,
            client=client,
            cache=cache,
            as_of=as_of,
            analysis_date=analysis_date,
            subreddits=subreddits,
            window_hours=args.window_hours,
            comments_per_post=args.comments_per_post,
            thread_cache_hours=args.thread_cache_hours,
            reuse_existing=not args.force,
            progress=progress,
        )
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"Reddit 预抓取无法启动：{exc}", file=sys.stderr)
        return 2

    if args.json_summary:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"完成 Reddit 预抓取：{report.run_id}")
        for result in report.results:
            print(
                f"  {result.ticker}: {result.status} | 帖子 {result.posts_fetched}/"
                f"{result.posts_seen} | 原始评论 {result.comments_seen} | 未筛选"
            )
        print(f"缓存：{args.cache}")
    return 1 if report.has_degradation else 0


if __name__ == "__main__":
    raise SystemExit(main())
