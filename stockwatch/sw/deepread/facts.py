"""原始数字采集。

**取不到就是 None，绝不用 0 冒充。** 0 会被下游判成「现金流为负」，
把「不知道」伪装成「不符合」—— 这是这个模块唯一的硬规矩。

NaN 是这条规矩的一个隐蔽变种：`bool(float('nan'))` 是 True，`nan / x`
不抛异常，如果不拦截，NaN 会被当成一个「确定的」float 值漏给下游，
比显式的 0 更难发现。所以 `collect()` 统一用 `_nan_to_none()` 把 NaN
折回 None，不管它来自哪个数据源。
"""
import math
import warnings
from datetime import date, timedelta

FIELDS = ("revenue_yoy", "operating_cash_flow", "gross_margin_delta_pt",
          "avg_corr_to_holdings", "form4_filings_30d", "rank", "rank_prev",
          "rank_delta", "pe", "forward_pe")


def financials(ticker):
    """从 yfinance 取季度财务派生量。取不到的 key 直接不出现。

    单块失败会被记进 `_errors`（供 collect() 写入 source_health 留痕），
    不会中断其他块的采集，也不会抛给调用方。
    """
    warnings.filterwarnings("ignore")
    import yfinance as yf

    out, errors = {}, []
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
    except Exception as e:
        errors.append(f"income_stmt {type(e).__name__}: {e}")
    try:
        cf = tk.quarterly_cashflow
        ocf = _row(cf, "Operating Cash Flow")
        if ocf is not None and len(ocf) >= 1:
            out["operating_cash_flow"] = float(ocf[0])
    except Exception as e:
        errors.append(f"cashflow {type(e).__name__}: {e}")
    try:
        info = tk.info or {}
        for key, name in (("pe", "trailingPE"), ("forward_pe", "forwardPE")):
            v = info.get(name)
            if isinstance(v, (int, float)):
                out[key] = float(v)
    except Exception as e:
        errors.append(f"info {type(e).__name__}: {e}")
    if errors:
        out["_errors"] = errors
    return out


def _nan_to_none(v):
    """把 NaN 当作「取不到」处理，不是一个合法的数值。

    NaN 的真值判断恒为 True、参与四则运算不抛异常 —— 如果不在这里拦截，
    它会绕过「取不到就是 None」的检查，被当成一个「确定的」float 漏给
    下游，比显式的 0 更隐蔽（0 至少还会被 `>= 阈值` 挡住，NaN 连挡都挡不住，
    任何比较都是 False，「不知道」就这样伪装成了「不符合」）。
    """
    try:
        if v is not None and math.isnan(float(v)):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _row(df, label):
    """从 pandas DataFrame 里按行名精确匹配（忽略大小写与首尾空白）取一行，
    返回 list。取不到返回 None。"""
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
    """汇总一只票的全部原始数字。取不到的字段是 None 并登记进 missing。

    财务源（无论是整体抛异常，还是 `financials()` 内部单块失败）都会留痕到
    `source_health`，方便日后排查「这个字段为什么永远是 None」，而不是让
    失败悄悄消失在一个 `except: pass` 里。
    """
    held = list(held_tickers or [])
    market = cfg.get("benchmarks.market", "SPY")

    errors = []
    try:
        f = (fin or financials)(ticker) or {}
    except Exception as e:
        f = {}
        errors.append(f"{type(e).__name__}: {e}")
    else:
        inner = f.pop("_errors", None)
        if inner:
            errors.extend(inner)
    if errors:
        _log_financials_health(store, ticker, errors)

    # 显式写 source='all-stocks'，不要靠 ORDER BY source LIMIT 1 碰运气拿到它。
    # 这个源名是用户批准「前 20 含前 10」时换来的接盘风险披露口径
    # （pool.reddit_top 用的也是这个 source），源名一变这里就该跟着断，
    # 而不是字典序悄悄取到别的榜单。
    rows = store.q(
        "SELECT rank, rank_24h_ago FROM reddit_rank WHERE d=? AND ticker=? "
        "AND source='all-stocks' AND COALESCE(TRIM(ticker),'')<>'' LIMIT 1",
        (d, ticker))
    rank = rows[0]["rank"] if rows else None
    prev = rows[0]["rank_24h_ago"] if rows else None

    lo = (date.fromisoformat(d) - timedelta(days=30)).isoformat()
    # run_ingest 只对持仓票按需查 EDGAR；全市场扫描行的 ticker 是空串
    # （CIK_TICKER_MAP 生产里为空）。所以「这只票在窗口内完全没有任何
    # edgar_filings 行」和「查过、确实没有 Form 4 申报」是两码事：前者是
    # 「没查过」，绝不能报 0 —— 那会被 criteria.evaluate 判成硬「未命中」，
    # 而实际上根本没有取数（spec §6：绝不把取不到数伪装成不符合）。
    # 只有该 ticker 在窗口内确实有过至少一条（任意 form）申报记录时，
    # 才说明这只票在这段时间被 EDGAR 覆盖到了，0 份 Form 4 才是一个真判定。
    covered = store.q(
        "SELECT COUNT(*) c FROM edgar_filings WHERE ticker=? "
        "AND filed_at>=? AND filed_at<=?", (ticker, lo, d))[0]["c"]
    if covered:
        n4 = store.q(
            "SELECT COUNT(DISTINCT accession) c FROM edgar_filings "
            "WHERE form='4' AND ticker=? AND filed_at>=? AND filed_at<=?",
            (ticker, lo, d))[0]["c"]
        form4_filings_30d = int(n4)
    else:
        form4_filings_30d = None    # 这只票在窗口内没有任何 EDGAR 覆盖，不是「查到 0 份」

    out = {
        "ticker": ticker,
        "revenue_yoy": _nan_to_none(f.get("revenue_yoy")),
        "operating_cash_flow": _nan_to_none(f.get("operating_cash_flow")),
        "gross_margin_delta_pt": _nan_to_none(f.get("gross_margin_delta_pt")),
        "avg_corr_to_holdings": _corr_to_holdings(store, ticker, held, market),
        "form4_filings_30d": form4_filings_30d,
        "rank": rank,
        "rank_prev": prev,
        "rank_delta": (prev - rank) if (rank is not None and prev is not None) else None,
        "pe": _nan_to_none(f.get("pe")),
        "forward_pe": _nan_to_none(f.get("forward_pe")),
    }
    out["missing"] = [k for k in FIELDS if out.get(k) is None]
    return out


def _log_financials_health(store, ticker, errors):
    """把财务源失败写进 source_health；只读库或写入本身出错都不能击穿主链路。"""
    if getattr(store, "read_only", False):
        return
    try:
        store.log_health("yfinance.financials", False, 0,
                          f"{ticker}: " + "; ".join(errors))
    except Exception:
        # 健康记录是旁路观测，绝不能让它挡住 facts 采集的主流程。
        pass
