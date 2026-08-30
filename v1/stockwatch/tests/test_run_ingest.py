#!/usr/bin/env python3
"""run_ingest.py 里 EDGAR 写库逻辑的测试。运行：python3 tests/test_run_ingest.py

修复轮 2 · Critical 1：`if tres.ok and not a.dry_run` 这条写库门槛用错了
信号——fetch_filings_for_tickers 的 ok 反映的是「整个查询过程有没有出错」，
持仓有 25+ 只、每只查 2 种 form，任何一只限流/网络抖动都会让 ok=False；
如果门槛用 ok，其余几十只已经成功抓到的行会一行都不写。

修复轮 2 第一次修法是把判断抽成 `_should_write_edgar` 纯函数并单测它——
结果修复轮 3 的复审发现：这条纯函数本身没坏，坏的是 main() 里的调用点
被人绕开它、写回 `.ok` 门槛，而单测那个纯函数完全看不出来（8 个测试文件
全绿）。而且这类失败是**静默**的——不抛异常、不报错，只是 edgar_filings
悄悄没写入新数据，find_causes 因此恒返回 []。

修复轮 3 把整段 EDGAR 抓取与入库逻辑抽成 `ingest_edgar(st, email,
positions_tickers, dry_run, fetch_all, fetch_by_ticker)`，fetch_all /
fetch_by_ticker 可注入假实现——这样测的是这个函数真实的**行为**（库里
到底写没写、写了什么），而不是一个可能没被实际调用点使用的判断条件。
"""
import sys, tempfile, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_ingest as RI
from sw.store import Store
from sw.sources.base import SourceResult

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Store(tmp.name)


def _empty_ok(*a, **kw):
    """假的 fetch_all：什么都没抓到，但过程顺利。"""
    return SourceResult(source="edgar.filings", ok=True, rows=0, latency_ms=1,
                        detail="", data=[])


def test_partial_failure_still_writes():
    """
    核心场景：持仓 25 只里 24 只查询成功、1 只报错——fetch_by_ticker
    返回 ok=False 但 rows=3（另外几只查到的数据）。这 3 行必须被写进库，
    不能因为个别 ticker 出错就整批放弃（否则就是 Critical 1 的静默丢数据）。
    """
    print("\n部分失败但抓到了数据（ok=False, rows=3）时，这些行仍应写进库")
    st = fresh_store()
    rows = [
        ("acc-1", "2026-08-27", "AAPL", "1", "8-K", "5.02", "u1", "{}", "t"),
        ("acc-2", "2026-08-27", "NVDA", "2", "8-K", "2.02", "u2", "{}", "t"),
        ("acc-3", "2026-08-27", "MU", "3", "8-K", "7.01", "u3", "{}", "t"),
    ]

    def fake_by_ticker(*a, **kw):
        return SourceResult(source="edgar.filings_by_ticker", ok=False, rows=3,
                            latency_ms=1, detail="1 只 ticker 报错", data=rows)

    RI.ingest_edgar(st, "test@example.com", ["AAPL", "NVDA", "MU"], dry_run=False,
                    fetch_all=_empty_ok, fetch_by_ticker=fake_by_ticker)

    got = st.q("SELECT accession FROM edgar_filings ORDER BY accession")
    check("3 行都写进去了", [r["accession"] for r in got], ["acc-1", "acc-2", "acc-3"])
    st.close()


def test_market_wide_does_not_clobber():
    """
    真实生产场景：先靠按持仓查询写入一条完整的行（ticker/items 都有），
    过几天全市场扫描又扫到同一个 accession（它拿不到 ticker/items，只有
    cik），这时候按持仓那步这次没有再次成功抓到它（比如已经不在
    days_back 窗口内、或者恰好这次查询报错）——完整行不该被抹空。
    """
    print("\n全市场扫描不该抹空已有的完整行（分两次调用 ingest_edgar 模拟跨天场景）")
    st = fresh_store()

    def fake_by_ticker_full(*a, **kw):
        row = ("acc-clobber", "2026-08-27", "AAPL", "320193", "8-K",
              "2.02,9.01", "https://sec.gov/acc-clobber", "{}", "t1")
        return SourceResult(source="edgar.filings_by_ticker", ok=True, rows=1,
                            latency_ms=1, detail="", data=[row])

    def fake_by_ticker_empty(*a, **kw):
        return SourceResult(source="edgar.filings_by_ticker", ok=True, rows=0,
                            latency_ms=1, detail="", data=[])

    def fake_all_clobbers(*a, **kw):
        # 全市场扫描拿到同一个 accession，但没有 ticker/items
        row = ("acc-clobber", "2026-08-27", "", "320193", "8-K",
              "", "https://sec.gov/acc-clobber", "{}", "t2")
        return SourceResult(source="edgar.filings", ok=True, rows=1,
                            latency_ms=1, detail="", data=[row])

    # 第一次调用（模拟第 1 天）：按持仓查到完整行
    RI.ingest_edgar(st, "test@example.com", ["AAPL"], dry_run=False,
                    fetch_all=_empty_ok, fetch_by_ticker=fake_by_ticker_full)

    # 第二次调用（模拟第 2 天）：全市场扫描又扫到同一条（空 ticker/items），
    # 按持仓这次没有再次抓到它
    RI.ingest_edgar(st, "test@example.com", ["AAPL"], dry_run=False,
                    fetch_all=fake_all_clobbers, fetch_by_ticker=fake_by_ticker_empty)

    row = st.q("SELECT * FROM edgar_filings WHERE accession=?", ("acc-clobber",))[0]
    check("ticker 仍然完整（没被全市场扫描抹空）", row["ticker"], "AAPL")
    check("items 仍然完整（没被全市场扫描抹空）", row["items"], "2.02,9.01")
    st.close()


def test_log_health_gets_real_ok():
    """
    log_health 记的 ok 必须是数据源真实返回的 ok，不能被恒 True/False
    替换——这是运维排障时判断"是不是数据源真的出问题了"的依据。
    """
    print("\nlog_health 收到的应该是真实的 ok（哪怕是 False）")
    st = fresh_store()

    def fake_by_ticker_failed(*a, **kw):
        return SourceResult(source="edgar.filings_by_ticker", ok=False, rows=0,
                            latency_ms=1, detail="全部失败", data=[])

    RI.ingest_edgar(st, "test@example.com", ["AAPL"], dry_run=False,
                    fetch_all=_empty_ok, fetch_by_ticker=fake_by_ticker_failed)

    rec = st.q("SELECT ok FROM source_health WHERE source=? ORDER BY ts DESC LIMIT 1",
              ("edgar.filings_by_ticker",))
    check("找到健康记录", len(rec), 1)
    if rec:
        check("ok 是真实的 False（没被替换成恒 True）", rec[0]["ok"], 0)
    st.close()


def test_edgar_form_scope_keeps_13f_market_wide_only():
    """13F-HR 只走全市场扫描，按持仓查询仍只查 8-K / Form 4。"""
    print("\nEDGAR 表单范围：全市场含 13F-HR，按持仓不含 13F-HR")
    st = fresh_store()
    calls = []

    def fake_all(*a, **kw):
        calls.append(("all", kw.get("forms")))
        return SourceResult(source="edgar.filings", ok=True, rows=0,
                            latency_ms=1, detail="", data=[])

    def fake_by_ticker(*a, **kw):
        calls.append(("by_ticker", kw.get("forms")))
        return SourceResult(source="edgar.filings_by_ticker", ok=True, rows=0,
                            latency_ms=1, detail="", data=[])

    RI.ingest_edgar(st, "test@example.com", ["AAPL"], dry_run=False,
                    fetch_all=fake_all, fetch_by_ticker=fake_by_ticker)
    check("全市场查询包含 13F-HR",
          calls[0][1] if calls else None, ("8-K", "4", "13F-HR"))
    check("按持仓查询仍只查 8-K / 4",
          calls[1][1] if len(calls) > 1 else None, ("8-K", "4"))
    st.close()


def test_main_writes_edgar_rows_even_when_ticker_query_partially_failed():
    """
    修复轮 2 遗留的端到端护栏：ingest_edgar 本身的单测再全，也防不住
    main() 干脆不调用它、自己重新手写一遍写库逻辑这类回归。这里仍然真正
    跑一遍 main()（用 STOCKWATCH_DB 环境变量指向临时库，sw/config.py 的
    Config.db_path 本来就为这个场景设计），monkeypatch 掉所有网络数据源，
    只让 fetch_filings_for_tickers 返回"抓到 1 行但 ok=False"，断言这行
    确实被写进了库。全程不打网络、不碰 data/stockwatch.db。
    """
    print("\n端到端：main() 依然通过 ingest_edgar 正确写库")
    import os, sys as _sys

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()

    orig_env = os.environ.get("STOCKWATCH_DB")
    orig_argv = _sys.argv
    orig_fetch_prices = RI.P.fetch_prices
    orig_fetch_meta = RI.P.fetch_meta
    orig_fetch_reddit = RI.R.fetch_reddit
    orig_fetch_filings = RI.E.fetch_filings
    orig_fetch_filings_for_tickers = RI.E.fetch_filings_for_tickers

    def fake_tres(*a, **kw):
        row = ("acc-integration-test", "2026-08-27", "AAPL", "320193", "8-K",
              "5.02", "https://sec.gov/acc-integration-test",
              json.dumps({"company": "Apple Inc."}), "2026-08-27T00:00:00")
        return SourceResult(source="edgar.filings_by_ticker", ok=False, rows=1,
                            latency_ms=1, detail="模拟部分失败", data=[row])

    try:
        os.environ["STOCKWATCH_DB"] = tmp.name
        _sys.argv = ["run_ingest.py"]
        RI.P.fetch_prices = _empty_ok
        RI.P.fetch_meta = _empty_ok
        RI.R.fetch_reddit = _empty_ok
        RI.E.fetch_filings = _empty_ok
        RI.E.fetch_filings_for_tickers = fake_tres
        RI.main()
    finally:
        if orig_env is not None:
            os.environ["STOCKWATCH_DB"] = orig_env
        else:
            os.environ.pop("STOCKWATCH_DB", None)
        _sys.argv = orig_argv
        RI.P.fetch_prices = orig_fetch_prices
        RI.P.fetch_meta = orig_fetch_meta
        RI.R.fetch_reddit = orig_fetch_reddit
        RI.E.fetch_filings = orig_fetch_filings
        RI.E.fetch_filings_for_tickers = orig_fetch_filings_for_tickers

    st = Store(tmp.name)
    rows = st.q("SELECT * FROM edgar_filings WHERE accession=?", ("acc-integration-test",))
    check("端到端跑完 main() 后，部分失败但抓到的那行确实写进库了", len(rows), 1)
    if rows:
        check("ticker 字段完整", rows[0]["ticker"], "AAPL")
    st.close()


if __name__ == "__main__":
    test_partial_failure_still_writes()
    test_market_wide_does_not_clobber()
    test_log_health_gets_real_ok()
    test_edgar_form_scope_keeps_13f_market_wide_only()
    test_main_writes_edgar_rows_even_when_ticker_query_partially_failed()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
