#!/usr/bin/env python3
"""
环境与数据源可行性检查
在你的 Mac 上运行：python3 check_env.py

先安装依赖：
  pip3 install yfinance edgartools requests pandas numpy litellm

它会逐项验证这个项目需要的每个数据源是否真的能用，最后打印一份报告。
不需要任何 API key 也能跑（LLM 那项会显示未配置）。
"""
import sys, time, json, traceback
from datetime import datetime

# ============ 改这里 ============
SEC_IDENTITY = "your.email@example.com"   # SEC 要求提供联系邮箱，必填
NTFY_TOPIC   = ""                          # 填你的 ntfy topic 名才会测推送，留空跳过
TEST_TICKERS = ["NVDA", "AAPL", "XOM", "KO", "SPY"]
# ================================

R = []
def rec(name, ok, detail=""):
    R.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  —  {detail}" if detail else ""))

def section(t):
    print(f"\n{'='*62}\n{t}\n{'='*62}")

print(f"数据源可行性检查   {datetime.now():%Y-%m-%d %H:%M:%S}")
print(f"Python {sys.version.split()[0]}")

# ---------- 1. 依赖 ----------
section("1 / 6   依赖包")
mods = {}
for m in ["yfinance", "pandas", "numpy", "requests", "edgar", "litellm"]:
    try:
        mods[m] = __import__(m)
        v = getattr(mods[m], "__version__", "?")
        rec(m, True, f"v{v}")
    except Exception as e:
        rec(m, False, f"未安装 — pip3 install {'edgartools' if m=='edgar' else m}")

# ---------- 2. yfinance ----------
section("2 / 6   yfinance（行情 / 基本面 / 新闻 / 财报日历）")
if "yfinance" in mods:
    yf = mods["yfinance"]
    import warnings; warnings.filterwarnings("ignore")

    # 2.1 批量历史价格 —— 归因引擎和相关性分析的基础
    try:
        t0 = time.time()
        df = yf.download(TEST_TICKERS, period="1y", interval="1d",
                         progress=False, auto_adjust=True)["Close"]
        df = df.dropna(how="all")
        ok = df.shape[0] > 200 and df.shape[1] == len(TEST_TICKERS)
        rec("批量历史日线", ok,
            f"{df.shape[0]} 天 × {df.shape[1]} 只，耗时 {time.time()-t0:.1f}s")
        if ok:
            import numpy as np
            r = df.pct_change().dropna()
            c = r.corr()
            print(f"\n     相关性抽样（验证对冲逻辑是否成立）:")
            for a, b in [("NVDA","AAPL"), ("NVDA","XOM"), ("NVDA","KO")]:
                if a in c.columns and b in c.columns:
                    print(f"       {a} vs {b:5s}  {c.loc[a,b]:+.3f}")
            print(f"     年化波动率:")
            for t in df.columns:
                print(f"       {t:5s}  {r[t].std()*np.sqrt(252)*100:5.1f}%")
            print()
    except Exception as e:
        rec("批量历史日线", False, str(e)[:90])

    tk = yf.Ticker("AAPL")
    # 2.2 行业分类 —— 风险对冲推荐需要
    try:
        info = tk.info
        s, i = info.get("sector"), info.get("industry")
        rec("行业分类 (sector/industry)", bool(s), f"AAPL → {s} / {i}")
    except Exception as e:
        rec("行业分类", False, str(e)[:90])
    # 2.3 财报日历
    try:
        ed = tk.get_earnings_dates(limit=4)
        rec("财报日历", ed is not None and len(ed) > 0, f"取到 {len(ed)} 条")
    except Exception as e:
        rec("财报日历", False, str(e)[:90])
    # 2.4 新闻 —— 归因引擎需要
    try:
        nw = tk.get_news()
        rec("新闻", bool(nw), f"取到 {len(nw)} 条")
    except Exception as e:
        rec("新闻", False, str(e)[:90])
    # 2.5 分析师评级
    try:
        rc = tk.get_recommendations()
        rec("分析师评级", rc is not None and len(rc) > 0, f"{len(rc)} 行")
    except Exception as e:
        rec("分析师评级", False, str(e)[:90])
    # 2.6 财务报表
    try:
        fin = tk.quarterly_income_stmt
        rec("季度利润表", fin is not None and fin.shape[1] > 0, f"{fin.shape[1]} 个季度")
    except Exception as e:
        rec("季度利润表", False, str(e)[:90])
    # 2.7 内部人交易
    try:
        ins = tk.get_insider_transactions()
        rec("内部人交易", ins is not None, f"{0 if ins is None else len(ins)} 行")
    except Exception as e:
        rec("内部人交易", False, str(e)[:90])
else:
    rec("yfinance 全部检查", False, "包未安装，跳过")

# ---------- 3. SEC EDGAR ----------
section("3 / 6   SEC EDGAR（8-K / Form 4 / 13F）")
if "edgar" in mods:
    try:
        from edgar import set_identity, Company, get_filings
        if "@" not in SEC_IDENTITY:
            rec("SEC identity", False, "请先在脚本顶部填写你的邮箱（SEC 强制要求）")
        else:
            set_identity(SEC_IDENTITY)
            rec("SEC identity", True, SEC_IDENTITY)
            try:
                f = get_filings(form="8-K").head(5)
                rec("8-K 最新申报", len(f) > 0, f"取到 {len(f)} 条")
            except Exception as e:
                rec("8-K 最新申报", False, str(e)[:90])
            try:
                f4 = get_filings(form="4").head(5)
                rec("Form 4 内部人申报", len(f4) > 0, f"取到 {len(f4)} 条")
            except Exception as e:
                rec("Form 4 内部人申报", False, str(e)[:90])
            try:
                f13 = get_filings(form="13F-HR").head(3)
                rec("13F 机构持仓", len(f13) > 0, f"取到 {len(f13)} 条")
            except Exception as e:
                rec("13F 机构持仓", False, str(e)[:90])
    except Exception as e:
        rec("edgartools", False, str(e)[:90])
else:
    rec("SEC EDGAR 全部检查", False, "edgartools 未安装，跳过")

# ---------- 4. ApeWisdom ----------
section("4 / 6   ApeWisdom（Reddit 讨论热度）")
if "requests" in mods:
    rq = mods["requests"]
    for filt in ["all-stocks", "wallstreetbets"]:
        try:
            u = f"https://apewisdom.io/api/v1.0/filter/{filt}/page/1"
            d = rq.get(u, timeout=15).json()
            res = d.get("results", [])
            ok = len(res) > 0
            top = ", ".join(f"{x['ticker']}({x['mentions']})" for x in res[:3])
            rec(f"{filt}", ok, f"{d.get('count','?')} 只 | 前三: {top}")
            # 验证变化率字段 —— 我们的模型只用变化率，不用绝对排名
            if ok:
                has = all(k in res[0] for k in ("rank_24h_ago", "mentions_24h_ago"))
                rec(f"  └ 24h 变化率字段", has,
                    "rank_24h_ago / mentions_24h_ago 存在" if has else "缺失，模型需调整")
        except Exception as e:
            rec(f"{filt}", False, str(e)[:90])
else:
    rec("ApeWisdom", False, "requests 未安装")

# ---------- 5. ntfy ----------
section("5 / 6   ntfy 推送")
if NTFY_TOPIC and "requests" in mods:
    try:
        r = mods["requests"].post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data="环境检查通过 — 这是一条测试推送".encode("utf-8"),
            headers={"Title": "StockWatch", "Priority": "default", "Tags": "chart_with_upwards_trend"},
            timeout=15)
        rec("ntfy 推送", r.status_code == 200, f"HTTP {r.status_code}，检查手机是否收到")
    except Exception as e:
        rec("ntfy 推送", False, str(e)[:90])
else:
    rec("ntfy 推送", False, "未配置 NTFY_TOPIC，跳过（不影响其他功能）")

# ---------- 6. LLM ----------
section("6 / 6   LLM 网关（LiteLLM）")
import os
keys = {k: bool(os.environ.get(k)) for k in
        ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"]}
found = [k for k, v in keys.items() if v]
rec("环境变量里的 API key", bool(found), ", ".join(found) if found else "一个都没有（可稍后配）")
if "litellm" in mods and found:
    try:
        from litellm import completion
        m = {"ANTHROPIC_API_KEY": "claude-sonnet-4-5",
             "OPENAI_API_KEY": "gpt-4o-mini",
             "GEMINI_API_KEY": "gemini/gemini-2.0-flash",
             "DEEPSEEK_API_KEY": "deepseek/deepseek-chat"}[found[0]]
        resp = completion(model=m, messages=[{"role":"user","content":"回复两个字：通过"}], max_tokens=20)
        rec(f"实际调用 {m}", True, resp.choices[0].message.content.strip()[:40])
    except Exception as e:
        rec("LLM 调用", False, str(e)[:110])

# ---------- 汇总 ----------
section("汇总")
ok_n = sum(1 for _, o, _ in R if o)
print(f"  通过 {ok_n} / {len(R)}\n")
bad = [(n, d) for n, o, d in R if not o]
if bad:
    print("  未通过：")
    for n, d in bad:
        print(f"    ✗ {n}  {d}")
    print()
print("  把这份输出发给 Claude，据此决定架构里哪些数据源可用、哪些要换。")
