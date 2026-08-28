#!/usr/bin/env python3
"""交易日历与半根 K 线剔除的测试。运行：python3 tests/test_market_time.py

为什么这个模块值得单独测：把未收盘的半根 K 线当成一整天喂进 60 日回归，
会污染 β 和残差 σ，而且是**静默的** —— 不报错，只让结论悄悄变歪。
这类 bug 靠肉眼永远发现不了，只能靠测试挡住。
"""
import sys
from datetime import datetime, date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from zoneinfo import ZoneInfo
from sw import market_time as MT

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def test_trading_day():
    print("\n交易日判定")
    check("2026-08-27 周四是交易日", MT.is_trading_day(date(2026, 8, 27)), True)
    check("2026-08-29 周六不是", MT.is_trading_day(date(2026, 8, 29)), False)
    check("2026-08-30 周日不是", MT.is_trading_day(date(2026, 8, 30)), False)


def test_session_closed():
    print("\n收盘判定（ET 16:00 为界，输入直接用 ET 构造）")
    et = ZoneInfo("America/New_York")
    # 当天 09:00 ET —— 还没开盘，今天这根 bar 不完整
    now = datetime(2026, 8, 27, 9, 0, tzinfo=et)
    check("当天 09:00 ET，当天未收盘", MT.session_closed(date(2026, 8, 27), now), False)
    # 当天 15:59 ET —— 盘中，仍未收盘
    now = datetime(2026, 8, 27, 15, 59, tzinfo=et)
    check("当天 15:59 ET，当天未收盘", MT.session_closed(date(2026, 8, 27), now), False)
    # 当天 16:00 ET —— 收盘
    now = datetime(2026, 8, 27, 16, 0, tzinfo=et)
    check("当天 16:00 ET，当天已收盘", MT.session_closed(date(2026, 8, 27), now), True)
    # 过去的日子永远算收盘
    check("昨天永远算已收盘", MT.session_closed(date(2026, 8, 26), now), True)


def test_session_closed_timezone_conversion():
    """本模块存在的唯一理由就是本机时区（PT）到市场时区（ET）的换算。
    上面 test_session_closed 传入的 now 直接就是 ET，根本没有触发
    astimezone(ET) 的转换逻辑 —— 删掉 session_closed 里的
    `now.astimezone(ET)` 那一行，上面的用例照样全绿。这个函数才是
    真正锁住换算正确性的测试：用非 ET 时区构造 now，验证换算后的结果。
    """
    print("\n跨时区换算（本机时区 → ET，这才是本模块存在的理由）")
    pt = ZoneInfo("America/Los_Angeles")
    utc = ZoneInfo("UTC")
    d = date(2026, 8, 27)

    # PT 06:00 = ET 09:00（夏令时 PT 与 ET 相差 3 小时）—— 开盘前，未收盘
    now = datetime(2026, 8, 27, 6, 0, tzinfo=pt)
    check("PT 06:00 = ET 09:00，当天未收盘", MT.session_closed(d, now), False)

    # PT 12:59 = ET 15:59 —— 盘中，未收盘
    now = datetime(2026, 8, 27, 12, 59, tzinfo=pt)
    check("PT 12:59 = ET 15:59，当天未收盘", MT.session_closed(d, now), False)

    # PT 13:00 = ET 16:00 —— 正好收盘
    now = datetime(2026, 8, 27, 13, 0, tzinfo=pt)
    check("PT 13:00 = ET 16:00，当天已收盘", MT.session_closed(d, now), True)

    # UTC 20:00 = ET 16:00（夏令时）—— 不止 PT，UTC 也要验证换算正确
    now = datetime(2026, 8, 27, 20, 0, tzinfo=utc)
    check("UTC 20:00 = ET 16:00，当天已收盘", MT.session_closed(d, now), True)

    # UTC 19:59 = ET 15:59 —— 盘中，未收盘
    now = datetime(2026, 8, 27, 19, 59, tzinfo=utc)
    check("UTC 19:59 = ET 15:59，当天未收盘", MT.session_closed(d, now), False)


def test_session_closed_rejects_naive_datetime():
    """naive datetime（没有 tzinfo）不能被当成"反正就是 ET"悄悄接受 ——
    这种隐含假设一旦某天调用方传入本机 naive 时间（比如 datetime.now()，
    在 PT 机器上跑就是 PT 时间），会被误判成 ET，产生一个看起来合理但
    错误的收盘判定，而且不报错。所以这里要求显式抛异常。"""
    print("\nnaive datetime 必须显式拒绝（不能默认当成 ET）")
    naive_now = datetime(2026, 8, 27, 9, 0)  # 没有 tzinfo
    try:
        MT.session_closed(date(2026, 8, 27), naive_now)
        ok = False
        got = "未抛异常"
    except ValueError:
        ok = True
        got = "ValueError"
    print(f"  {'✅' if ok else '❌'} naive datetime 触发 ValueError: 得到 {got!r}，期望 'ValueError'")
    if not ok:
        FAIL.append("naive datetime 触发 ValueError")


def test_drop_incomplete_bars():
    print("\n半根 K 线剔除")
    et = ZoneInfo("America/New_York")
    rows = [
        ("2026-08-25", "AAPL", 300.0, 1e6),
        ("2026-08-26", "AAPL", 305.0, 1e6),
        ("2026-08-27", "AAPL", 310.0, 3e5),   # ← 今天，盘中的半根
    ]
    # ET 09:00，开盘前（PT→ET 的换算正确性由 test_session_closed_timezone_conversion 单独锁住），
    # 今天这根必须被剔除
    now = datetime(2026, 8, 27, 9, 0, tzinfo=et)
    kept, dropped = MT.drop_incomplete_bars(rows, now)
    check("剔除后剩 2 行", len(kept), 2)
    check("被剔除的是今天", dropped, ["2026-08-27"])
    check("保留的最后一天是 08-26", kept[-1][0], "2026-08-26")

    # 收盘后跑，今天这根是完整的，必须保留
    now = datetime(2026, 8, 27, 16, 30, tzinfo=et)
    kept, dropped = MT.drop_incomplete_bars(rows, now)
    check("收盘后 3 行全留", len(kept), 3)
    check("收盘后无剔除", dropped, [])

    # 空输入不能炸
    check("空输入返回空", MT.drop_incomplete_bars([], now), ([], []))


if __name__ == "__main__":
    test_trading_day()
    test_session_closed()
    test_session_closed_timezone_conversion()
    test_session_closed_rejects_naive_datetime()
    test_drop_incomplete_bars()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
