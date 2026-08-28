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


if __name__ == "__main__":
    test_writes_when_rows_found_even_if_ok_is_false()
    test_does_not_write_when_no_rows()
    test_dry_run_never_writes()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
