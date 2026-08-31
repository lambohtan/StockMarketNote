#!/usr/bin/env python
"""数据源体检：把每个输入端真实调用一次，报告可用性、返回量和所需凭证。

为什么需要它
------------
上游的失败是"软"的：来源挂了通常仍返回一段文本，分析师照读不误。实测过一次
Reddit 被 429 限流，交给模型的文本却写着"没有找到帖子"——模型于是把抓取失败
当成了"市场无人讨论"的证据。这类失败在最终报告里看不出来。

这个脚本把每个来源单独调一次，并区分三种结果：
  可用      拿到了实质内容
  空但成功  来源正常响应，确实没有匹配内容（合法的空集合）
  失败      网络/限流/凭证/解析出错 —— 绝不能被当成"没有内容"
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lean.env import load_env  # noqa: E402

load_env()
RESULTS = REPO_ROOT / "local-data" / "tradingagents"

OK, EMPTY, FAIL, NOKEY = "可用", "空但成功", "失败", "缺凭证"

#: 返回内容里出现这些片段，说明来源其实没给出实质数据。
_EMPTY_MARKERS = ("no news found", "no posts found", "<no ", "not available",
                  "unavailable", "no data")


#: 正文里出现这些，说明来源明确报告了自己不可用（而不是"确实没有内容"）。
_FAIL_MARKERS = ("fetch failed", "unavailable —", "unavailable -", "fetch_failed")


def classify(text: str, stderr: str) -> tuple[str, str]:
    """把一次取数的结果分类。

    判据以**最终交给模型的正文**为准，不是 stderr。退避重试成功时 stderr 里
    仍留着 429 警告，早期版本据此误判为失败；真正要紧的是模型最后读到了什么。
    """
    lowered = (text or "").lower()
    noisy = [ln.strip() for ln in stderr.splitlines() if ln.strip()]

    if not text or not text.strip():
        return FAIL, "返回空字符串（既非数据也非说明）"
    if lowered.startswith("error") or "traceback" in lowered:
        return FAIL, text.strip().splitlines()[0][:80]
    if any(m in lowered for m in _FAIL_MARKERS):
        return FAIL, text.strip().splitlines()[0][:90]
    if any(m in lowered for m in _EMPTY_MARKERS):
        note = text.strip().splitlines()[0][:80]
        if noisy:
            return FAIL, f"{note}｜但来源同时报错：{noisy[0][:60]}"
        return EMPTY, note
    return OK, (f"（过程中有重试：{noisy[0][:60]}）" if noisy else "")


def probe(name: str, credential: str, fn) -> dict:
    if credential and not os.environ.get(credential):
        return {"name": name, "cred": credential, "status": NOKEY,
                "size": 0, "note": f"未设置 {credential}"}
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            text = str(fn())
    except Exception as exc:                       # noqa: BLE001
        return {"name": name, "cred": credential or "无需", "status": FAIL,
                "size": 0, "note": f"{type(exc).__name__}: {exc}"[:90]}
    status, note = classify(text, err.getvalue())
    return {"name": name, "cred": credential or "无需", "status": status,
            "size": len(text), "note": note}


def dwidth(text: str) -> int:
    return sum(2 if ord(c) > 0x2E80 else 1 for c in text)


def cell(text: str, width: int, right: bool = False) -> str:
    pad = " " * max(0, width - dwidth(text))
    return pad + text if right else text + pad


def main() -> int:
    ap = argparse.ArgumentParser(description="数据源体检")
    ap.add_argument("ticker", nargs="?", default="AMD")
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    from tradingagents.dataflows.config import set_config
    from tradingagents.default_config import DEFAULT_CONFIG

    config = DEFAULT_CONFIG.copy()
    config["results_dir"] = str(RESULTS / "logs")
    config["data_cache_dir"] = str(RESULTS / "cache")
    set_config(config)

    from tradingagents.agents.utils import agent_utils as A
    from tradingagents.dataflows.reddit import fetch_reddit_posts
    from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages

    tk, day = args.ticker.upper(), args.date
    start = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    start180 = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=180)).strftime("%Y-%m-%d")

    checks = [
        ("价格 OHLCV", "", lambda: A.get_stock_data.func(tk, start180, day)),
        ("技术指标 rsi", "", lambda: A.get_indicators.func(tk, "rsi", day, 10)),
        ("行情核验快照", "", lambda: A.get_verified_market_snapshot.func(tk, day)),
        ("基本面快照", "", lambda: A.get_fundamentals.func(tk, day)),
        ("利润表", "", lambda: A.get_income_statement.func(tk, "quarterly", day)),
        ("现金流量表", "", lambda: A.get_cashflow.func(tk, "quarterly", day)),
        ("资产负债表", "", lambda: A.get_balance_sheet.func(tk, "quarterly", day)),
        ("内部人交易", "", lambda: A.get_insider_transactions.func(tk)),
        ("个股新闻", "", lambda: A.get_news.func(tk, start, day)),
        ("全球宏观新闻", "", lambda: A.get_global_news.func(day, 7, 10)),
        ("StockTwits 舆情", "", lambda: fetch_stocktwits_messages(tk, limit=30)),
        ("Reddit 舆情", "", lambda: fetch_reddit_posts(tk)),
        ("预测市场 Polymarket", "", lambda: A.get_prediction_markets.func("Fed rate cut", 5)),
        ("FRED 宏观数据", "FRED_API_KEY", lambda: A.get_macro_indicators.func("cpi", day, 30)),
    ]

    print(f"\n数据源体检 · {tk} · {day}\n")
    cols = (22, 20, 10, 10)
    print("  " + "  ".join(cell(h, w) for h, w in
                           zip(("来源", "凭证", "状态", "返回量"), cols)))
    print("  " + "─" * (sum(cols) + 6))

    rows = [probe(n, c, f) for n, c, f in checks]
    for r in rows:
        mark = {OK: "✓", EMPTY: "○", FAIL: "✗", NOKEY: "✗"}[r["status"]]
        print("  " + "  ".join([
            cell(r["name"], cols[0]), cell(r["cred"], cols[1]),
            cell(f"{mark} {r['status']}", cols[2]),
            cell(f"{r['size']:,}" if r["size"] else "—", cols[3], right=True),
        ]))
        if r["note"]:
            print(f"      └ {r['note']}")

    counts = {s: sum(1 for r in rows if r["status"] == s) for s in (OK, EMPTY, FAIL, NOKEY)}
    print(f"\n  可用 {counts[OK]} ｜ 空但成功 {counts[EMPTY]} ｜ "
          f"失败 {counts[FAIL]} ｜ 缺凭证 {counts[NOKEY]}  （共 {len(rows)}）")

    if counts[FAIL] or counts[NOKEY]:
        print("\n  注意：标记为「失败」的来源，其返回文本仍会被送进分析师 prompt。")
        print("  若文本读起来像「没有找到内容」，模型会把抓取失败误当成事实上的空白。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
