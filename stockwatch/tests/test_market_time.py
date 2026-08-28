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
    print("\n收盘判定（ET 16:00 为界）")
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


def test_drop_incomplete_bars():
    print("\n半根 K 线剔除")
    et = ZoneInfo("America/New_York")
    rows = [
        ("2026-08-25", "AAPL", 300.0, 1e6),
        ("2026-08-26", "AAPL", 305.0, 1e6),
        ("2026-08-27", "AAPL", 310.0, 3e5),   # ← 今天，盘中的半根
    ]
    # 本机 06:00 PT = 09:00 ET，开盘前，今天这根必须被剔除
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
    test_drop_incomplete_bars()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
