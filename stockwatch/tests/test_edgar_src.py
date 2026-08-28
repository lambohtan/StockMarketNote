#!/usr/bin/env python3
"""sw/sources/edgar_src.py 的轻量测试。运行：python3 tests/test_edgar_src.py

不打网络 —— 只测「把 filing 对象组装成入库行」这段纯函数逻辑（_assemble_row）。
修复轮 1 的根因是：全市场 get_filings() 的索引行本来就不带 ticker/items
（用真实请求核实过），causes.py 却要靠这两个字段匹配和判定 L1。
这里要确认按 ticker 查（Company(tk).get_filings()）产出的行格式正确，
尤其 items 无论来源是 list 还是逗号分隔字符串，最终都要规整成逗号分隔字符串。
"""
import sys, tempfile, json
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.sources import edgar_src as E
from sw.store import Store

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


def test_fetch_filings_for_tickers_drops_stale_filings():
    """
    不打真实网络：把 edgar.Company 换成假的，模拟一只 ticker 下有一条最近的
    申报和一条很久以前的申报，验证 days_back 窗口过滤只保留最近的那条 ——
    否则每天都会把好几年前的历史申报当成"最近发生的原因"重新入库。
    """
    print("\nfetch_filings_for_tickers 应该按 days_back 窗口过滤掉陈旧申报")
    import edgar as edgar_module
    from datetime import date, timedelta

    recent = (date.today() - timedelta(days=1)).isoformat()
    stale = (date.today() - timedelta(days=400)).isoformat()

    class FakeFilings:
        def __init__(self, items):
            self._items = items
        def __iter__(self):
            return iter(self._items)
        def __getitem__(self, k):
            return self._items[k]

    class FakeCompany:
        def __init__(self, ticker):
            self.ticker = ticker
        def get_filings(self, form=None):
            return FakeFilings([
                SimpleNamespace(accession_no="acc-recent", filing_date=recent,
                                items="5.02", cik="1", filing_url="", company="X"),
                SimpleNamespace(accession_no="acc-stale", filing_date=stale,
                                items="5.02", cik="1", filing_url="", company="X"),
            ])

    orig_company = getattr(edgar_module, "Company", None)
    edgar_module.Company = FakeCompany
    try:
        res = E.fetch_filings_for_tickers("test@example.com", ["FAKE"],
                                          forms=("8-K",), days_back=7, limit_per=10)
    finally:
        if orig_company is not None:
            edgar_module.Company = orig_company

    accs = sorted(r[0] for r in res.data)
    check("只保留窗口内的那条", accs, ["acc-recent"])


def test_fetch_filings_for_tickers_filters_before_truncating():
    """
    修复轮 2 · Important 5：之前的实现是 list(fl)[:limit_per] 先截断再按
    days_back 过滤，完全依赖"返回按时间倒序"这个未经验证的假设。这里故意
    让陈旧的申报排在列表前面，模拟这个假设不成立的情况：如果实现是先截断
    再过滤，窗口内的申报会被静默丢弃；必须先过滤再截断才能保住它们。
    """
    print("\nfetch_filings_for_tickers 应该先按窗口过滤，再按 limit_per 截断")
    import edgar as edgar_module
    from datetime import date, timedelta

    recent1 = (date.today() - timedelta(days=1)).isoformat()
    recent2 = (date.today() - timedelta(days=2)).isoformat()
    stale = (date.today() - timedelta(days=400)).isoformat()

    class FakeFilings:
        def __init__(self, items):
            self._items = items
        def __iter__(self):
            return iter(self._items)
        def __getitem__(self, k):
            return self._items[k]

    class FakeCompany:
        def __init__(self, ticker):
            self.ticker = ticker
        def get_filings(self, form=None):
            # 陈旧的排在最前面 —— 如果实现是先切片再过滤，
            # limit_per=2 会切到两条陈旧的，两条最近的被静默丢弃
            return FakeFilings([
                SimpleNamespace(accession_no="acc-stale-1", filing_date=stale,
                                items="5.02", cik="1", filing_url="", company="X"),
                SimpleNamespace(accession_no="acc-stale-2", filing_date=stale,
                                items="5.02", cik="1", filing_url="", company="X"),
                SimpleNamespace(accession_no="acc-stale-3", filing_date=stale,
                                items="5.02", cik="1", filing_url="", company="X"),
                SimpleNamespace(accession_no="acc-recent-1", filing_date=recent1,
                                items="5.02", cik="1", filing_url="", company="X"),
                SimpleNamespace(accession_no="acc-recent-2", filing_date=recent2,
                                items="5.02", cik="1", filing_url="", company="X"),
            ])

    orig_company = getattr(edgar_module, "Company", None)
    edgar_module.Company = FakeCompany
    try:
        res = E.fetch_filings_for_tickers("test@example.com", ["FAKE"],
                                          forms=("8-K",), days_back=7, limit_per=2)
    finally:
        if orig_company is not None:
            edgar_module.Company = orig_company

    accs = sorted(r[0] for r in res.data)
    check("窗口内最近的两条都保住了（没被过早截断吃掉）",
          accs, ["acc-recent-1", "acc-recent-2"])


def test_market_wide_does_not_clobber_ticker():
    """
    修复轮 2 · Critical 2：全市场扫描拿不到 ticker/items，只有 cik。
    如果它用 INSERT OR REPLACE 写同一个 accession，会把按持仓查询已经
    写好的完整行覆盖回空的。全市场扫描必须改用 insert_ignore_many
    （INSERT OR IGNORE），不覆盖已存在的行。
    """
    print("\n全市场扫描（insert_ignore_many）不该覆盖已经写好的完整行")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    st = Store(tmp.name)
    cols = ["accession", "filed_at", "ticker", "cik", "form",
            "items", "url", "raw_json", "seen_at"]

    # 第一步：按持仓查询写入完整行（INSERT OR REPLACE，正常路径）
    st.upsert_many("edgar_filings", cols,
                   [("acc-1", "2026-08-27", "AAPL", "320193", "8-K",
                     "2.02,9.01", "https://sec.gov/acc-1",
                     json.dumps({"company": "Apple Inc."}), "2026-08-27T09:00:00")])

    # 第二步：全市场扫描对同一个 accession 写入空的 ticker/items，
    # 但走 insert_ignore_many —— 不该覆盖已有的完整行
    st.insert_ignore_many("edgar_filings", cols,
                          [("acc-1", "2026-08-27", "", "320193", "8-K",
                            "", "https://sec.gov/acc-1",
                            json.dumps({"company": "Apple Inc."}), "2026-08-27T10:00:00")])

    row = st.q("SELECT * FROM edgar_filings WHERE accession=?", ("acc-1",))[0]
    check("ticker 仍然完整（没被全市场扫描抹空）", row["ticker"], "AAPL")
    check("items 仍然完整（没被全市场扫描抹空）", row["items"], "2.02,9.01")
    st.close()


if __name__ == "__main__":
    test_ticker_filled_when_passed_explicitly()
    test_items_as_list_gets_joined_with_comma()
    test_items_missing_becomes_empty_string()
    test_accession_falls_back_to_accession_number()
    test_default_ticker_falls_back_to_filing_attribute()
    test_default_ticker_empty_when_filing_has_none()
    test_fetch_filings_for_tickers_drops_stale_filings()
    test_fetch_filings_for_tickers_filters_before_truncating()
    test_market_wide_does_not_clobber_ticker()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
