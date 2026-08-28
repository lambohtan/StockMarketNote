#!/usr/bin/env python3
"""sw/sources/edgar_src.py 的轻量测试。运行：python3 tests/test_edgar_src.py

不打网络 —— 只测「把 filing 对象组装成入库行」这段纯函数逻辑（_assemble_row）。
修复轮 1 的根因是：全市场 get_filings() 的索引行本来就不带 ticker/items
（用真实请求核实过），causes.py 却要靠这两个字段匹配和判定 L1。
这里要确认按 ticker 查（Company(tk).get_filings()）产出的行格式正确，
尤其 items 无论来源是 list 还是逗号分隔字符串，最终都要规整成逗号分隔字符串。
"""
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.sources import edgar_src as E

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def test_ticker_filled_when_passed_explicitly():
    print("\n按 ticker 查询时，ticker 应该被明确写进行里")
    f = SimpleNamespace(accession_no="0000320193-26-000018", filing_date="2026-08-27",
                        items="2.02,9.01", cik="320193",
                        filing_url="https://sec.gov/x", company="Apple Inc.")
    row = E._assemble_row(f, "8-K", "2026-08-27T09:00:00", ticker="AAPL")
    # 行结构：accession, filed_at, ticker, cik, form, items, url, raw_json, seen_at
    check("ticker 非空", row[2], "AAPL")
    check("items 保持逗号分隔", row[5], "2.02,9.01")


def test_items_as_list_gets_joined_with_comma():
    print("\nitems 若是 list，要规整成逗号分隔字符串")
    f = SimpleNamespace(accession_no="acc-1", filing_date="2026-08-27",
                        items=["4.02", "9.01"], cik="1", filing_url="", company="X")
    row = E._assemble_row(f, "8-K", "2026-08-27T09:00:00", ticker="MU")
    check("items 逗号连接", row[5], "4.02,9.01")


def test_items_missing_becomes_empty_string():
    print("\nitems 拿不到时应该是空字符串，不是 None")
    f = SimpleNamespace(accession_no="acc-2", filing_date="2026-08-27",
                        cik="1", filing_url="", company="X")   # 没有 items 属性
    row = E._assemble_row(f, "8-K", "2026-08-27T09:00:00", ticker="MU")
    check("items 为空字符串", row[5], "")


def test_accession_falls_back_to_accession_number():
    print("\naccession_no 拿不到时退回 accession_number")
    f = SimpleNamespace(accession_number="0000320193-26-000099", filing_date="2026-08-27",
                        items="", cik="1", filing_url="", company="X")
    row = E._assemble_row(f, "8-K", "2026-08-27T09:00:00", ticker="MU")
    check("accession 取到 fallback 值", row[0], "0000320193-26-000099")


def test_default_ticker_falls_back_to_filing_attribute():
    print("\n不传 ticker 时（全市场扫描路径）沿用 filing 对象自带的 ticker 属性")
    f = SimpleNamespace(accession_no="acc-3", filing_date="2026-08-27",
                        items="8.01", cik="1", filing_url="", company="X", ticker="ZZZ")
    row = E._assemble_row(f, "8-K", "2026-08-27T09:00:00")   # 不传 ticker
    check("沿用 filing.ticker", row[2], "ZZZ")


def test_default_ticker_empty_when_filing_has_none():
    print("\n不传 ticker 且 filing 对象也没有 ticker 属性时，应为空字符串（不是 None）")
    f = SimpleNamespace(accession_no="acc-4", filing_date="2026-08-27",
                        items="8.01", cik="1", filing_url="", company="X")   # 没有 ticker 属性
    row = E._assemble_row(f, "8-K", "2026-08-27T09:00:00")
    check("ticker 为空字符串", row[2], "")


if __name__ == "__main__":
    test_ticker_filled_when_passed_explicitly()
    test_items_as_list_gets_joined_with_comma()
    test_items_missing_becomes_empty_string()
    test_accession_falls_back_to_accession_number()
    test_default_ticker_falls_back_to_filing_attribute()
    test_default_ticker_empty_when_filing_has_none()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
