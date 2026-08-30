"""
收益率计算。

两个数字，含义完全不同，报告里必须都给：

1. 持有期简单收益 = (市值 - 成本)/成本
   Fidelity 直接给这个。它受加仓时点影响：一路下跌时不断买入会拉低成本，
   看起来「只亏了 5%」，但你在亏损过程中投入了更多钱，实际体验远比这个难受。

2. 时间加权收益率 TWR
   把每天的收益率连乘，剔除资金进出的时点影响 —— 这是「你的选股本身表现如何」，
   也是唯一能和 SPY/QQQ 公平比较的口径。需要交易流水才能算。

TWR 的口径：只算股票仓（不含现金）。
  V_t = 当日股票市值，CF_t = 当日买入金额(+)/卖出金额(−)
  r_t = (V_t − CF_t) / V_{t−1} − 1，  TWR = Π(1+r_t) − 1
资金流按当日开盘计入。价格用 yfinance 复权价，所以分红已隐含在价格里，
现金分红不再单独计入（避免重复计算）。
"""
import math
import numpy as np


def simple_return(portfolio):
    """持有期简单收益。缺成本数据的持仓单独列出，不当成 0。"""
    cost = portfolio.cost_total
    known = [h for h in portfolio.holdings if h.cost_basis is not None]
    mv = sum(h.market_value for h in known)
    return {
        "cost": cost,
        "market_value": mv,
        "gain": (mv - cost) if cost is not None else None,
        "gain_pct": ((mv - cost) / cost) if cost else None,
        "missing_cost": portfolio.missing_cost,
        "per_ticker": sorted(
            [{"ticker": h.ticker, "gain": h.gain, "gain_pct": h.gain_pct,
              "market_value": h.market_value} for h in known],
            key=lambda x: -(x["gain_pct"] or 0)),
    }


def split_factors(tickers, since):
    """
    {ticker: [(日期, 比例), ...]} —— 拆股记录。
    交易流水里的股数是「当时的股数」，而复权价是按当前股数换算的。
    不做这个换算，拆股前的持仓市值会算错好几倍。
    """
    import warnings; warnings.filterwarnings("ignore")
    import yfinance as yf
    out, errs = {}, []
    for tk in tickers:
        try:
            s = yf.Ticker(tk).splits
            if s is None or len(s) == 0:
                continue
            evs = [(str(i.date()), float(v)) for i, v in s.items()
                   if str(i.date()) >= since and float(v) not in (0.0, 1.0)]
            if evs:
                out[tk] = sorted(evs)
        except Exception as e:
            errs.append(f"{tk}:{type(e).__name__}")
    return out, errs


def _to_current_units(qty, tk, txn_date, splits):
    """把「当时的股数」换算成当前股数口径：乘上该日之后发生的所有拆股比例。"""
    f = 1.0
    for d, ratio in splits.get(tk, []):
        if d > txn_date:
            f *= ratio
    return qty * f


def twr(store, portfolio, txns, bench=("SPY", "QQQ"), splits=None):
    """
    返回 dict，含 TWR、年化、区间、基准对比、以及所有降级说明。
    txns 缺失或数据不足时返回 {"ok": False, "reason": ...}，上层照常出报告。
    """
    trades = [t for t in txns if t["kind"] in ("buy", "sell") and t["ticker"] and t["quantity"]]
    if not trades:
        return {"ok": False, "reason": "交易流水里没有可用的买卖记录"}

    splits = splits if splits is not None else {}
    end = portfolio.snapshot_date
    start = min(t["date"] for t in trades)

    # 基准也要一起取价，否则 SPY 这类不在持仓里的基准会静默缺失
    tickers = sorted({t["ticker"] for t in trades} | set(portfolio.tickers) | set(bench))
    px = {}
    for r in store.price_history(tickers, start=start):
        px.setdefault(r["ticker"], {})[r["d"]] = r["close"]
    days = sorted({d for m in px.values() for d in m if start <= d <= end})
    if len(days) < 20:
        return {"ok": False, "reason": f"库里 {start}~{end} 只有 {len(days)} 个交易日的价格，"
                                       f"先跑 run_ingest.py 补历史行情"}

    # 每日股数变动（换算到当前股数口径）
    delta = {}
    for t in trades:
        q = _to_current_units(t["quantity"], t["ticker"], t["date"], splits)
        delta.setdefault(t["date"], {}).setdefault(t["ticker"], 0.0)
        delta[t["date"]][t["ticker"]] += q

    # 从今天的持仓倒推区间起点的持仓
    qty = {h.ticker: h.quantity for h in portfolio.holdings}
    for d, m in delta.items():
        for tk, q in m.items():
            qty[tk] = qty.get(tk, 0.0) - q

    # 每日现金流：买入为正（钱进股票仓），卖出为负
    cf = {}
    for t in trades:
        amt = t.get("amount")
        if amt is not None:
            v = -float(amt)          # Fidelity 买入 Amount 为负
        elif t.get("price") is not None:
            v = float(t["quantity"]) * float(t["price"])
        else:
            continue
        cf[t["date"]] = cf.get(t["date"], 0.0) + v

    last_px, vals, flows, unpriced = {}, [], [], set()
    for d in days:
        for tk, m in delta.get(d, {}).items():      # 当日交易在当日收盘已生效
            qty[tk] = qty.get(tk, 0.0) + m
        v = 0.0
        for tk, q in qty.items():
            if abs(q) < 1e-9:
                continue
            p = px.get(tk, {}).get(d, last_px.get(tk))
            if p is None:
                unpriced.add(tk)
                continue
            last_px[tk] = p
            v += q * p
        vals.append(v)
        flows.append(cf.get(d, 0.0))

    rs = []
    for i in range(1, len(vals)):
        if vals[i - 1] <= 0:
            continue
        rs.append((vals[i] - flows[i]) / vals[i - 1] - 1.0)
    if len(rs) < 20:
        return {"ok": False, "reason": "有效收益日不足 20 天"}

    cum = float(np.prod([1 + r for r in rs]) - 1)
    yrs = max((len(rs) / 252.0), 1e-6)
    # ⚠️ 不足半年不做年化。20 个交易日的 +11.6% 外推成「年化 +299%」是假精确，
    # 拿三周的运气当成一年的能力 —— 这类数字比没有数字更有害。
    MIN_ANNUALIZE_DAYS = 126
    short = len(rs) < MIN_ANNUALIZE_DAYS
    ann = None if (short or cum <= -1) else float((1 + cum) ** (1 / yrs) - 1)

    bres = {}
    for b in bench:
        s = sorted((d, p) for d, p in px.get(b, {}).items() if days[0] <= d <= days[-1])
        if len(s) >= 20:
            br = s[-1][1] / s[0][1] - 1
            bres[b] = {"return": float(br),
                       "annualized": None if short else float((1 + br) ** (1 / yrs) - 1)}

    # 回撤必须算在 TWR 指数上，不能算在市值上 ——
    # 每月定投会把市值推高，用市值算出来的回撤是被新资金掩盖过的假数字。
    idx = np.cumprod([1.0 + r for r in rs])
    peak = np.maximum.accumulate(idx)
    mdd = float(np.min(idx / peak - 1.0))

    return {
        "ok": True, "start": days[0], "end": days[-1], "trading_days": len(rs),
        "twr": cum, "annualized": ann,
        "short_window": short, "min_annualize_days": MIN_ANNUALIZE_DAYS,
        "vol_annualized": float(np.std(rs, ddof=1) * math.sqrt(252)),
        "max_drawdown": mdd,
        "benchmarks": bres,
        "excess_vs": {b: cum - x["return"] for b, x in bres.items()},
        "unpriced_tickers": sorted(unpriced),
        "splits_applied": {k: v for k, v in splits.items()},
    }
