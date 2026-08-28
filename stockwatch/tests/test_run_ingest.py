#!/usr/bin/env python3
"""run_ingest.py 里写库门槛判断的测试。运行：python3 tests/test_run_ingest.py

修复轮 2 · Critical 1：审查发现 `if tres.ok and not a.dry_run` 这条写库门槛
用错了信号 —— fetch_filings_for_tickers 的 ok 反映的是「整个查询过程有没
有出错」，持仓有 25+ 只、每只查 2 种 form，任何一只限流/网络抖动都会让
ok=False；如果门槛用 ok，其余几十只已经成功抓到的行会一行都不写，把
「find_causes 必现返回 []」变成「偶发、更难发现的空数据」。

正确的门槛应该只看「有没有抓到数据」（rows > 0），ok 只用来喂健康记录。
这里把判断抽成 `_should_write_edgar`，直接单测这个函数，不需要真的跑一遍
run_ingest.py（那需要网络和持仓 CSV，不适合做单元测试）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_ingest as RI

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def test_writes_when_rows_found_even_if_ok_is_false():
    """
    核心场景：25 只里 24 只成功、1 只报错 —— rows>0 但 ok=False。
    这条数据必须被写进去，不能因为个别 ticker 出错就整批放弃。
    """
    print("\n部分失败但抓到了数据（rows>0, ok=False）时，仍然应该写库")
    res = SimpleNamespace(rows=24, ok=False)
    check("应该写", RI._should_write_edgar(res, dry_run=False), True)


def test_does_not_write_when_no_rows():
    print("\n完全没抓到数据（rows=0）时，即使 ok=True 也没什么好写的")
    res = SimpleNamespace(rows=0, ok=True)
    check("不该写", RI._should_write_edgar(res, dry_run=False), False)


def test_dry_run_never_writes():
    print("\n--dry-run 时无论如何都不该写库")
    res = SimpleNamespace(rows=24, ok=False)
    check("dry-run 不写", RI._should_write_edgar(res, dry_run=True), False)
    res_ok = SimpleNamespace(rows=24, ok=True)
    check("dry-run 不写（即使 ok=True）", RI._should_write_edgar(res_ok, dry_run=True), False)


def test_main_writes_edgar_rows_even_when_ticker_query_partially_failed():
    """
    修复轮 2 · Critical 1（变异 2 专用）：只测 _should_write_edgar 这个
    辅助函数，抓不住"main() 里的调用点被人改回直接用 tres.ok"这类回归——
    毕竟辅助函数本身没坏，坏的是调用点绕过了它。这里补一个端到端集成测试，
    monkeypatch 掉所有网络数据源（价格/元数据/Reddit/EDGAR 全市场扫描），
    只让 fetch_filings_for_tickers 返回"抓到 1 行但 ok=False"（模拟 25 只
    里 24 只成功、1 只报错的真实场景），真正跑一遍 main()，断言这一行确实
    被写进了库——不是复用真实库，是用 STOCKWATCH_DB 环境变量指向的临时库
    （sw.config.Config.db_path 本来就是为这种场景设计的：优先读环境变量，
    "方便用测试库跑验证而不污染真实快照历史"）。
    """
    print("\n端到端：main() 里即使 tres.ok=False，只要 rows>0 也应该写库")
    import os, sys as _sys, tempfile, json as _json
    from sw.sources.base import SourceResult
    from sw.store import Store

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()

    orig_env = os.environ.get("STOCKWATCH_DB")
    orig_argv = _sys.argv
    orig_fetch_prices = RI.P.fetch_prices
    orig_fetch_meta = RI.P.fetch_meta
    orig_fetch_reddit = RI.R.fetch_reddit
    orig_fetch_filings = RI.E.fetch_filings
    orig_fetch_filings_for_tickers = RI.E.fetch_filings_for_tickers

    def fake_ok_empty(*a, **kw):
        return SourceResult(source="fake", ok=True, rows=0, latency_ms=1, detail="", data=[])

    def fake_tres(*a, **kw):
        # 模拟"抓到数据但过程不完全顺利"：AAPL 这一行成功，同时至少一只
        # ticker 报错——ok=False，rows>0，正是 Critical 1 描述的场景
        row = ("acc-integration-test", "2026-08-27", "AAPL", "320193", "8-K",
              "5.02", "https://sec.gov/acc-integration-test",
              _json.dumps({"company": "Apple Inc."}), "2026-08-27T00:00:00")
        return SourceResult(source="edgar.filings_by_ticker", ok=False, rows=1,
                            latency_ms=1, detail="模拟部分失败", data=[row])

    try:
        os.environ["STOCKWATCH_DB"] = tmp.name
        _sys.argv = ["run_ingest.py"]   # 不带 --positions、不带 --dry-run
        RI.P.fetch_prices = fake_ok_empty
        RI.P.fetch_meta = fake_ok_empty
        RI.R.fetch_reddit = fake_ok_empty
        RI.E.fetch_filings = fake_ok_empty
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
    test_writes_when_rows_found_even_if_ok_is_false()
    test_does_not_write_when_no_rows()
    test_dry_run_never_writes()
    test_main_writes_edgar_rows_even_when_ticker_query_partially_failed()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
