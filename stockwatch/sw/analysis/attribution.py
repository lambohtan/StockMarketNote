"""
归因引擎 —— 本系统最有价值的部分。

要回答的问题：我这只票今天动了 5%，是大盘的事、行业的事，还是它自己的事？
人工查这个每只票要花 15–20 分钟，而且经常查不到。

    个股当日收益 = α + β_市场 × SPY收益 + β_行业 × 行业ETF收益 + 残差

残差在正常范围 → 报告里**不提**。日报一半的价值来自它不说什么。
残差 > 2σ → 标为异动，触发找原因（见 causes.py）
残差 > 4σ → 升级为 L1

⚠️ 未经回测验证。这是描述工具，不预测涨跌。
"""
import numpy as np

ANOMALY_Z = 2.0
EXTREME_Z = 4.0
MIN_OBS = 40          # 少于这个样本量不给结论，宁可不说


def ols2(y, x1, x2):
    """
    二元 OLS：y = α + b1·x1 + b2·x2 + ε

    返回 {"alpha","b1","b2","resid","r2","n"}；样本不足返回 None。
    用 lstsq 而不是手写正规方程 —— 前者在因子高度共线时数值更稳
    （SPY 和 XLK 的相关性经常在 0.9 以上，这不是假设，是常态）。
    """
    y = np.asarray(y, float)
    x1 = np.asarray(x1, float)
    x2 = np.asarray(x2, float)
    n = min(len(y), len(x1), len(x2))
    if n < MIN_OBS:
        return None
    y, x1, x2 = y[-n:], x1[-n:], x2[-n:]
    X = np.column_stack([np.ones(n), x1, x2])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = float(1 - np.sum(resid ** 2) / ss_tot) if ss_tot > 0 else None
    return {"alpha": float(coef[0]), "b1": float(coef[1]), "b2": float(coef[2]),
            "resid": resid, "r2": r2, "n": n}


def classify(z):
    """按 |z| 分三档。注意用绝对值 —— 暴涨和暴跌都值得解释。"""
    if z is None:
        return "normal"
    a = abs(z)
    if a >= EXTREME_Z:
        return "extreme"
    if a >= ANOMALY_Z:
        return "anomaly"
    return "normal"


def _returns(store, ticker, start):
    rows = store.q("SELECT d, close FROM prices WHERE ticker=? AND d>=? ORDER BY d",
                   (ticker, start))
    ds = [r["d"] for r in rows]
    px = np.array([float(r["close"]) for r in rows], float)
    if len(px) < 2:
        return [], np.zeros(0)
    return ds[1:], px[1:] / px[:-1] - 1.0


def attribute(store, ticker, sector_etf, market_etf="SPY", window=60, start=None):
    """
    对单只票做归因。返回最后一个交易日的拆解结果，数据不足返回 None。

    sector_etf 为 None 时（行业未知、或是 ETF 本身）退化为单因子，
    b2 记 0 —— 不要为了凑双因子硬塞一个不相关的代理。
    """
    if start is None:
        from datetime import date, timedelta
        start = (date.today() - timedelta(days=int(window * 2.2) + 60)).isoformat()

    d_t, r_t = _returns(store, ticker, start)
    d_m, r_m = _returns(store, market_etf, start)
    if len(r_t) == 0 or len(r_m) == 0:
        return None

    if sector_etf:
        d_s, r_s = _returns(store, sector_etf, start)
    else:
        d_s, r_s = d_m, np.zeros(len(r_m))

    # 只保留三者都有报价的交易日，避免个别停牌日制造假跳空
    common = sorted(set(d_t) & set(d_m) & set(d_s))
    if len(common) < MIN_OBS:
        return None
    common = common[-window:]
    mt, mm, ms = dict(zip(d_t, r_t)), dict(zip(d_m, r_m)), dict(zip(d_s, r_s))
    y = np.array([mt[d] for d in common], float)
    x1 = np.array([mm[d] for d in common], float)
    x2 = np.array([ms[d] for d in common], float)

    fit = ols2(y, x1, x2)
    if fit is None:
        return None

    resid = fit["resid"]
    # ⚠️ σ 要用「除当日以外」的残差算 —— 否则今天这个大冲击会把 σ 自己抬高，
    # 把 4σ 事件压成 2σ，越是极端的事件越检不出来。
    sigma = float(np.std(resid[:-1], ddof=1)) if len(resid) > 2 else None
    z = (float(resid[-1]) / sigma) if sigma else None

    return {
        "ticker": ticker,
        "d": common[-1],
        "ret": float(y[-1]),
        "mkt_part": float(fit["b1"] * x1[-1]),
        "sector_part": float(fit["b2"] * x2[-1]),
        "idio": float(fit["alpha"] + resid[-1]),
        "sigma": sigma,
        "z": z,
        "level": classify(z),
        "beta_mkt": fit["b1"],
        "beta_sector": fit["b2"],
        "r2": fit["r2"],
        "n": fit["n"],
        "sector_etf": sector_etf,
        "market_etf": market_etf,
    }
