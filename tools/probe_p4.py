#!/usr/bin/env python3
"""P4 实现前的一次性探测。运行：python3 tools/probe_p4.py

回答三个问题：
  1. EDGAR 10-Q 正文里 MD&A / 风险因素两节能不能稳定抽出来？
  2. yfinance 的季度现金流表拿不拿得到？（决定标准是 6 条还是 5 条）
  3. 抽出来的正文有多长？（决定 stage1 要不要先压缩）

这是探测脚本，不是生产代码。结论写进 findings 文档后它就不再维护。
"""
import re
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "stockwatch"))
warnings.filterwarnings("ignore")

TICKERS = ["AAPL", "NVDA", "MU"]

# 章节标题在各家公司的写法不统一：大小写、Item 后的空格、撇号可能是弯的。
MDNA_START = re.compile(
    r"item\s*2\s*[.\-–—:]?\s*management[’'`’]?s\s+discussion", re.I)
MDNA_END = re.compile(r"item\s*3\s*[.\-–—:]?\s*quantitative", re.I)
RISK_START = re.compile(r"item\s*1a\s*[.\-–—:]?\s*risk\s+factors", re.I)
RISK_END = re.compile(r"item\s*(1b|2)\s*[.\-–—:]?", re.I)


def probe_edgar(email):
    from edgar import Company, set_identity
    set_identity(email)
    for tk in TICKERS:
        try:
            filings = Company(tk).get_filings(form="10-Q")
            latest = filings.latest(1)
            text = latest.text() if hasattr(latest, "text") else str(latest)
        except Exception as exc:
            print(f"  ❌ {tk}: 取正文失败 {type(exc).__name__}: {exc}")
            continue
        n = len(text or "")
        # 起点取最后一次出现：目录里也会出现同样的标题，正文在后面。
        starts = list(MDNA_START.finditer(text or ""))
        risks = list(RISK_START.finditer(text or ""))
        print(f"  {tk}: 全文 {n:,} 字符；"
              f"MD&A 标题命中 {len(starts)} 次；风险因素标题命中 {len(risks)} 次")
        if starts:
            s = starts[-1].start()
            e = MDNA_END.search(text, s)
            seg = text[s:e.start() if e else min(s + 120000, n)]
            print(f"     → MD&A 抽出 {len(seg):,} 字符，开头：{seg[:80]!r}")
        if risks:
            s = risks[-1].start()
            e = RISK_END.search(text, s + 10)
            seg = text[s:e.start() if e else min(s + 120000, n)]
            print(f"     → 风险因素抽出 {len(seg):,} 字符，开头：{seg[:80]!r}")


def probe_cashflow():
    import yfinance as yf
    for tk in TICKERS:
        try:
            cf = yf.Ticker(tk).quarterly_cashflow
        except Exception as exc:
            print(f"  ❌ {tk}: {type(exc).__name__}: {exc}")
            continue
        if cf is None or getattr(cf, "empty", True):
            print(f"  ❌ {tk}: 现金流表为空")
            continue
        idx = [str(i) for i in cf.index]
        hit = [i for i in idx
               if "operating" in i.lower() and "cash" in i.lower()]
        print(f"  {tk}: {cf.shape[0]} 行 × {cf.shape[1]} 列；"
              f"经营现金流行名命中 {hit or '无'}")
        if hit:
            print(f"     → 最近一期值：{cf.loc[hit[0]].iloc[0]}")


def main():
    from sw.config import CFG
    email = CFG.get("identity.sec_email")
    print("=== 1. EDGAR 10-Q 正文与章节抽取 ===")
    probe_edgar(email)
    print("\n=== 2. yfinance 季度现金流表 ===")
    probe_cashflow()
    print("\n结论请写进 docs/superpowers/plans/2026-08-29-p4-probe-findings.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
