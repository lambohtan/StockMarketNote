#!/usr/bin/env python
"""Read the current candidate stock pool from the local store.

This command never fetches and never writes: it answers "what is the pool right
now" from the last update, whenever that happened.  Other applications can use
the same data through :func:`lean.pool_store.load_latest_pool`.

Examples:

    .venv/bin/python read_pool.py                        # 表格，读 config 里的 top_n
    .venv/bin/python read_pool.py --top 30 --format tickers
    .venv/bin/python read_pool.py --format pool-file > today-pool.json

Exit codes: 0 pool available and fresh, 1 pool returned but older than
``max_age_hours``, 2 no pool matches the request (or the input is invalid).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from lean.pool_config import PoolConfigError, load_config  # noqa: E402
from lean.pool_store import PoolSnapshot, load_latest_pool  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="读取当前股票池；只读，不联网")
    parser.add_argument("--top", type=int, help="读取前 N 只（默认取 config 的 top_n）")
    parser.add_argument("--all", action="store_true", help="返回全部候选，忽略 top_n")
    parser.add_argument("--format", choices=("table", "json", "tickers", "pool-file"),
                        default="table", help="输出格式，默认 table")
    parser.add_argument("--require-complete", action="store_true",
                        help="只接受所有来源都健康的快照；没有就退出码 2")
    parser.add_argument("--max-age-hours", type=float,
                        help="超过这个年龄视为陈旧（退出码 1）；0 表示不检查")
    parser.add_argument("--db", type=Path, help="池子数据库路径")
    parser.add_argument("--config", type=Path, help="配置文件路径")
    return parser.parse_args(argv)


def _dwidth(text: str) -> int:
    return sum(2 if ord(c) > 0x2E80 else 1 for c in text)


def _cell(text: str, width: int, right: bool = False) -> str:
    text = text if _dwidth(text) <= width else text[: max(1, width - 1)] + "…"
    pad = " " * max(0, width - _dwidth(text))
    return pad + text if right else text + pad


def render(snapshot: PoolSnapshot, stale: bool, max_age_hours: float) -> str:
    age = snapshot.age_hours()
    lines = [f"\n当前股票池 · {snapshot.analysis_date} · as-of {snapshot.as_of}",
             f"状态 {snapshot.status} ｜ 写入 {snapshot.written_at} ｜ 距今 {age:.1f} 小时 ｜ "
             f"候选 {snapshot.total_candidates} 只，显示 {len(snapshot.entries)} 只\n"]
    cols = (4, 8, 30, 8, 8, 9, 9)
    headers = ("#", "代码", "名称", "交易所", "热度分", "来源数", "提及数")
    lines.append("  " + "  ".join(_cell(h, w) for h, w in zip(headers, cols)))
    lines.append("  " + "─" * (sum(cols) + 2 * len(cols)))
    for entry in snapshot.entries:
        lines.append("  " + "  ".join([
            _cell(str(entry.rank), cols[0], right=True),
            _cell(entry.ticker, cols[1]),
            _cell(entry.name or "—", cols[2]),
            _cell(entry.exchange, cols[3]),
            _cell(f"{entry.score:.3f}", cols[4], right=True),
            _cell(str(len(entry.sources)), cols[5], right=True),
            _cell("—" if entry.mentions is None else str(entry.mentions), cols[6], right=True),
        ]))
    if snapshot.degraded_sources:
        lines.append(f"\n  ⚠ 这份池子是降级快照：{', '.join(snapshot.degraded_sources)} 抓取失败。")
        lines.append("  失败不是「没人讨论」；下游必须把它当作部分输入。")
    if stale:
        lines.append(f"\n  ⚠ 池子已超过 {max_age_hours} 小时未更新，请先运行 build_pool.py。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(path=args.config, overrides={
            "db_path": args.db, "top_n": args.top, "max_age_hours": args.max_age_hours})
    except PoolConfigError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2

    limit = None if args.all else config.top_n
    snapshot = load_latest_pool(limit, require_complete=args.require_complete,
                                db_path=config.db_path)
    if snapshot is None:
        detail = "没有完整快照（所有来源健康）" if args.require_complete else "池子还没建过"
        print(f"no pool available: {detail}；数据库 {config.db_path}", file=sys.stderr)
        return 2

    stale = snapshot.is_stale(config.max_age_hours)
    if args.format == "tickers":
        print("\n".join(snapshot.tickers()))
    elif args.format == "pool-file":
        print(json.dumps(snapshot.to_pool_payload(), ensure_ascii=False, indent=2))
    elif args.format == "json":
        print(json.dumps({
            "run_id": snapshot.run_id,
            "as_of": snapshot.as_of,
            "analysis_date": snapshot.analysis_date,
            "written_at": snapshot.written_at,
            "status": snapshot.status,
            "degraded_sources": list(snapshot.degraded_sources),
            "age_hours": round(snapshot.age_hours(), 3),
            "stale": stale,
            "total_candidates": snapshot.total_candidates,
            "pool": [{
                "rank": e.rank, "ticker": e.ticker, "name": e.name, "exchange": e.exchange,
                "market": e.market, "country": e.country, "score": round(e.score, 6),
                "sources": list(e.sources), "reasons": list(e.reasons),
                "freshness": list(e.freshness), "source_health": dict(e.source_health),
                "mentions": e.mentions, "momentum": e.momentum,
                "price": e.price, "market_cap": e.market_cap,
            } for e in snapshot.entries],
        }, ensure_ascii=False, indent=2))
    else:
        print(render(snapshot, stale, config.max_age_hours))
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
