#!/usr/bin/env python
"""Build the day's candidate stock pool from public heat sources.

Examples:

    .venv/bin/python build_pool.py --top 20
    .venv/bin/python build_pool.py --as-of 2026-08-31T14:00:00Z --json

Each run stores one immutable snapshot in the pool database, then moves the
"latest" pointer, so readers always see a whole pool and a failed update leaves
the previous pool in place.

The command performs no LLM calls and places no orders.  It exits 0 when every
source answered, 1 when the pool is usable but at least one source failed, and
2 when the US listing whitelist is unavailable or the input is invalid — in
that last case nothing is stored or written at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from lean.pool import (  # noqa: E402
    HEALTH_FAILED,
    HEALTH_OK,
    ListingUnavailableError,
    PoolHttpClient,
    PoolReport,
    build_pool,
)
from lean.pool_config import PoolConfigError, load_config  # noqa: E402
from lean.pool_store import PoolStore  # noqa: E402

_HEALTH_MARK = {HEALTH_OK: "✓", "empty": "○", HEALTH_FAILED: "✗"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从公开热度来源构建当天美股候选池；不调用 LLM，不产生交易指令")
    parser.add_argument("--as-of", help="证据截止时间（ISO-8601，默认当前 UTC）")
    parser.add_argument("--analysis-date", help="归属的分析日（YYYY-MM-DD，默认 as-of 当天）")
    parser.add_argument("--top", type=int, help="打印/导出前 N 只（数据库始终存全量）")
    parser.add_argument("--min-price", type=float, help="最低股价")
    parser.add_argument("--min-market-cap", type=float, help="最低市值")
    parser.add_argument("--keep-runs", type=int, help="数据库保留最近几次快照")
    parser.add_argument("--db", type=Path, help="池子数据库路径")
    parser.add_argument("--config", type=Path, help="配置文件路径")
    parser.add_argument("--no-store", action="store_true", help="只跑不写数据库")
    parser.add_argument("--out", type=Path, help="额外导出一份 JSON 到该路径（可选）")
    parser.add_argument("--json", action="store_true", help="只打印 JSON 摘要")
    return parser.parse_args(argv)


def _dwidth(text: str) -> int:
    return sum(2 if ord(c) > 0x2E80 else 1 for c in text)


def _cell(text: str, width: int, right: bool = False) -> str:
    text = text if _dwidth(text) <= width else text[: max(1, width - 1)] + "…"
    pad = " " * max(0, width - _dwidth(text))
    return pad + text if right else text + pad


def render(report: PoolReport) -> str:
    lines = [f"\n候选股票池 · {report.analysis_date} · as-of {report.as_of}\n"]
    cols = (4, 8, 30, 8, 8, 9, 9)
    headers = ("#", "代码", "名称", "交易所", "热度分", "来源数", "提及数")
    lines.append("  " + "  ".join(_cell(h, w) for h, w in zip(headers, cols)))
    lines.append("  " + "─" * (sum(cols) + 2 * len(cols)))
    for index, entry in enumerate(report.ranked, start=1):
        lines.append("  " + "  ".join([
            _cell(str(index), cols[0], right=True),
            _cell(entry.ticker, cols[1]),
            _cell(entry.name or "—", cols[2]),
            _cell(entry.exchange, cols[3]),
            _cell(f"{entry.score:.3f}", cols[4], right=True),
            _cell(str(len(entry.sources)), cols[5], right=True),
            _cell("—" if entry.mentions is None else str(entry.mentions), cols[6], right=True),
        ]))

    lines.append("\n  来源健康")
    for outcome in report.outcomes:
        mark = _HEALTH_MARK.get(outcome.health, "?")
        note = f"  └ {outcome.note}" if outcome.note else ""
        lines.append(f"    {mark} {outcome.source}  {outcome.health}  {outcome.count}{note}")

    if report.complete:
        lines.append(f"\n  全部来源应答；候选 {len(report.considered)} 只，输出前 {len(report.ranked)} 只。")
    else:
        lines.append(f"\n  ⚠ 结果不完整：{', '.join(report.degraded_sources)} 抓取失败。")
        lines.append("  失败不是「没人讨论」；下游阶段必须把这批池子当作部分输入处理。")
    return "\n".join(lines)


def main(argv: list[str] | None = None, client=None) -> int:
    args = parse_args(argv)
    try:
        as_of = (datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
                 if args.as_of else datetime.now(timezone.utc))
    except ValueError as exc:
        print(f"invalid --as-of: {exc}", file=sys.stderr)
        return 2
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    try:
        config = load_config(path=args.config, overrides={
            "top_n": args.top, "min_price": args.min_price,
            "min_market_cap": args.min_market_cap, "keep_runs": args.keep_runs,
            "db_path": args.db})
    except PoolConfigError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2

    analysis_date = args.analysis_date or as_of.astimezone(timezone.utc).date().isoformat()

    try:
        report = build_pool(
            client or PoolHttpClient(),
            as_of=as_of,
            analysis_date=analysis_date,
            top_n=config.top_n,
            min_price=config.min_price,
            min_market_cap=config.min_market_cap,
            weights=config.weights,
        )
    except ListingUnavailableError as exc:
        # Fail closed: the previous stored pool stays the current pool.
        print(f"fail-closed: {exc}", file=sys.stderr)
        return 2

    run_id = None
    if not args.no_store:
        run_id = PoolStore(config.db_path).save_report(
            report, config=config.to_dict(), keep_runs=config.keep_runs)

    written: list[str] = []
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report.to_pool_payload(), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        provenance_path = args.out.with_name(f"{args.out.stem}-provenance.json")
        provenance_path.write_text(
            json.dumps(report.to_provenance(), ensure_ascii=False, indent=2), encoding="utf-8")
        written = [str(args.out), str(provenance_path)]

    if args.json:
        print(json.dumps({
            "complete": report.complete,
            "degraded_sources": list(report.degraded_sources),
            "run_id": run_id,
            "database": None if args.no_store else str(config.db_path),
            "stored_candidates": len(report.considered),
            "exported_files": written,
            "pool": [{"rank": i, "ticker": e.ticker, "score": round(e.score, 6),
                      "sources": list(e.sources)}
                     for i, e in enumerate(report.ranked, start=1)],
        }, ensure_ascii=False, indent=2))
    else:
        print(render(report))
        print(f"\n  配置：{config.source}")
        if run_id:
            print(f"  已存入：{config.db_path}（run {run_id[:8]}，全量 {len(report.considered)} 只）")
            print("  读取：read_pool.py --top 20")
        else:
            print("  未写入数据库（--no-store）")
        for path in written:
            print(f"  导出：{path}")
    return 0 if report.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
