"""原始数字采集。

**取不到就是 None，绝不用 0 冒充。** 0 会被下游判成「现金流为负」，
把「不知道」伪装成「不符合」—— 这是这个模块唯一的硬规矩。
"""
import warnings
from datetime import date, timedelta

FIELDS = ("revenue_yoy", "operating_cash_flow", "gross_margin_delta_pt",
          "avg_corr_to_holdings", "rank", "pe", "forward_pe")


def financials(ticker):
    """从 yfinance 取季度财务派生量。取不到的 key 直接不出现。"""
    warnings.filterwarnings("ignore")
    import yfinance as yf

    out = {}
    tk = yf.Ticker(ticker)
    try:
        inc = tk.quarterly_income_stmt
        rev = _row(inc, "Total Revenue")
        if rev is not None and len(rev) >= 5:
            # 第 0 列是最近一季，第 4 列是去年同季（季度表按时间倒序）。
            if rev[4]:
                out["revenue_yoy"] = float(rev[0]) / float(rev[4]) - 1.0
        gp = _row(inc, "Gross Profit")
        if rev is not None and gp is not None and len(rev) >= 2 and rev[0] and rev[1]:
            now_m = float(gp[0]) / float(rev[0])
            prev_m = float(gp[1]) / float(rev[1])
            out["gross_margin_delta_pt"] = (now_m - prev_m) * 100.0
    except Exception:
        pass
    try:
        cf = tk.quarterly_cashflow
        ocf = _row(cf, "Operating Cash Flow")
        if ocf is not None and len(ocf) >= 1:
            out["operating_cash_flow"] = float(ocf[0])
    except Exception:
        pass
    try:
        info = tk.info or {}
        for key, name in (("pe", "trailingPE"), ("forward_pe", "forwardPE")):
            v = info.get(name)
            if isinstance(v, (int, float)):
                out[key] = float(v)
    except Exception:
        pass
    return out


def _row(df, label):
    """从 pandas DataFrame 里按行名模糊取一行，返回 list。取不到返回 None。"""
    if df is None or getattr(df, "empty", True):
        return None
    for idx in df.index:
        if str(idx).strip().lower() == label.lower():
            return list(df.loc[idx].values)
    return None


def _corr_to_holdings(store, ticker, held, market):
    """与全部持仓的平均 60 日相关性。持仓为空或数据不足返回 None。"""
    from ..analysis import risk as RK
    peers = [t for t in held if t and t != ticker]
    if not peers:
        return None
    try:
        # price_panel 返回的是四元组 (dates, tickers, matrix, dropped)，不是 dict。
        dates, tickers, mat, dropped = RK.price_panel(
            store, [ticker] + peers, anchor=market, window=60)
        if ticker not in tickers or len(tickers) < 2:
            return None
        rets = RK.daily_returns(mat)
        # corr_matrix 返回 {(a, b): ρ}，只有上三角，所以两个方向都要查。
        corrs = RK.corr_matrix(rets, tickers)
        vals = []
        for other in tickers:
            if other == ticker:
                continue
            v = corrs.get((ticker, other), corrs.get((other, ticker)))
            if v is not None:
                vals.append(v)
        return sum(vals) / len(vals) if vals else None
    except Exception:
        return None


def collect(store, cfg, ticker, d, held_tickers=None, fin=None):
    """汇总一只票的全部原始数字。取不到的字段是 None 并登记进 missing。"""
    held = list(held_tickers or [])
    market = cfg.get("benchmarks.market", "SPY")

    try:
        f = (fin or financials)(ticker) or {}
    except Exception:
        f = {}

    rows = store.q(
        "SELECT rank, rank_24h_ago FROM reddit_rank WHERE d=? AND ticker=? "
        "AND COALESCE(TRIM(ticker),'')<>'' ORDER BY source LIMIT 1", (d, ticker))
    rank = rows[0]["rank"] if rows else None
    prev = rows[0]["rank_24h_ago"] if rows else None

    lo = (date.fromisoformat(d) - timedelta(days=30)).isoformat()
    n4 = store.q(
        "SELECT COUNT(DISTINCT accession) c FROM edgar_filings "
        "WHERE form='4' AND ticker=? AND filed_at>=? AND filed_at<=?",
        (ticker, lo, d))[0]["c"]

    out = {
        "ticker": ticker,
        "revenue_yoy": f.get("revenue_yoy"),
        "operating_cash_flow": f.get("operating_cash_flow"),
        "gross_margin_delta_pt": f.get("gross_margin_delta_pt"),
        "avg_corr_to_holdings": _corr_to_holdings(store, ticker, held, market),
        "form4_filings_30d": int(n4),        # 查得到但没有申报就是 0，不是缺失
        "rank": rank,
        "rank_prev": prev,
        "rank_delta": (prev - rank) if (rank is not None and prev is not None) else None,
        "pe": f.get("pe"),
        "forward_pe": f.get("forward_pe"),
    }
    out["missing"] = [k for k in FIELDS if out.get(k) is None]
    return out
