#!/usr/bin/env python3
"""
财报日历取数方式探测
check_env 里唯一真正失败的一项。大概率是 pandas 3.0 与 yfinance 的兼容问题
（yfinance 内部按列名取 'Earnings Date'，pandas 3.x 改了行为）。

这个脚本把 5 种取法全试一遍，告诉我们哪条路能走。
运行：python3 probe_earnings.py
"""
import traceback, warnings, sys
warnings.filterwarnings("ignore")
import yfinance as yf, pandas as pd

print(f"pandas {pd.__version__} · yfinance {yf.__version__}\n")
T = "AAPL"
tk = yf.Ticker(T)

def try_(name, fn):
    print(f"── {name}")
    try:
        v = fn()
        print(f"   ✅ 成功  type={type(v).__name__}")
        if isinstance(v, pd.DataFrame):
            print(f"   shape={v.shape}")
            print(f"   index={v.index.name} ({v.index.dtype})")
            print(f"   columns={list(v.columns)}")
            print("   " + v.head(3).to_string().replace("\n", "\n   "))
        elif isinstance(v, dict):
            for k, val in list(v.items())[:8]:
                print(f"   {k}: {val}")
        else:
            print(f"   {str(v)[:300]}")
        return True
    except Exception as e:
        print(f"   ❌ {type(e).__name__}: {e}")
        tb = traceback.format_exc().strip().split("\n")
        for line in tb[-4:]:
            print(f"      {line}")
        return False
    finally:
        print()

ok = {}
ok["calendar"]        = try_("方式1  tk.calendar（推荐的前瞻日期来源）", lambda: tk.calendar)
ok["get_earnings"]    = try_("方式2  tk.get_earnings_dates(limit=8)", lambda: tk.get_earnings_dates(limit=8))
ok["prop"]            = try_("方式3  tk.earnings_dates 属性", lambda: tk.earnings_dates)
ok["info"]            = try_("方式4  tk.info 里的财报字段", lambda: {
    k: tk.info.get(k) for k in
    ["earningsTimestamp","earningsTimestampStart","earningsTimestampEnd",
     "earningsCallTimestampStart","mostRecentQuarter"]})

print("── 方式5  SEC EDGAR 8-K Item 2.02（已发生的财报事件，最可靠）")
try:
    from edgar import set_identity, Company
    set_identity("lambohtan@gmail.com")
    c = Company(T)
    f = c.get_filings(form="8-K").head(5)
    print(f"   ✅ 成功  取到 {len(f)} 条 AAPL 的 8-K")
    print(f"   {f}"[:600])
    ok["edgar"] = True
except Exception as e:
    print(f"   ❌ {type(e).__name__}: {e}")
    ok["edgar"] = False
print()

print("="*60)
print("结论")
print("="*60)
for k, v in ok.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("\n把这段输出发给 Claude。")
