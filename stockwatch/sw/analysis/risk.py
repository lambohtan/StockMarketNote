"""
风险与集中度指标。全部确定性计算，公式写在各函数 docstring 里。

⚠️ 这些指标描述的是"过去这段时间发生了什么"，不预测未来。
相关性和波动率都是时变的 —— 实测 NVDA/AAPL 60 日相关性 +0.108，
而历史常态是 0.4–0.6。任何基于单一窗口的结论都要标注窗口长度。
"""
import math
import numpy as np


# ---------- 价格 → 收益率面板 ----------

def price_panel(store, tickers, start=None, anchor="SPY", window=None):
    """
    返回 (dates, tickers, close 矩阵, dropped)。

    ⚠️ 这里踩过一个坑：原本用「所有票都有数据的交易日」做 inner join，
    结果只要组合里有一只新股（比如 2026-06 才上市的 SPCX，只有 53 天），
    整个面板就会被压缩到那 53 天 —— 其他 25 只票两年的历史被一只新股拖没了，
    而且是**静默**发生的。

    改成：先用 anchor（默认 SPY）确定交易日历，再把在这段日历上有缺失的票剔除，
    并把剔除名单返回给上层写进报告。宁可少几只票，不要静默缩短所有人的窗口。
    """
    rows = store.price_history(sorted(set(tickers)), start=start)
    if not rows:
        return [], [], np.zeros((0, 0)), []
    by_d, seen = {}, set()
    for r in rows:
        by_d.setdefault(r["d"], {})[r["ticker"]] = r["close"]
        seen.add(r["ticker"])

    # 交易日历：优先用 anchor，没有就用覆盖天数最多的那只
    if anchor in seen:
        cal = sorted(d for d in by_d if anchor in by_d[d])
    else:
        cnt = {}
        for d, m in by_d.items():
            for t in m:
                cnt[t] = cnt.get(t, 0) + 1
        best = max(cnt, key=cnt.get) if cnt else None
        cal = sorted(d for d in by_d if best in by_d[d]) if best else sorted(by_d)
    if window:
        cal = cal[-(window + 1):]
    if len(cal) < 2:
        return [], sorted(seen), np.zeros((0, len(seen))), []

    keep, dropped = [], []
    for t in sorted(seen):
        have = sum(1 for d in cal if t in by_d[d])
        (keep if have == len(cal) else dropped).append(
            t if have == len(cal) else (t, have, len(cal)))
    mat = np.array([[by_d[d][t] for t in keep] for d in cal], dtype=float)
    return cal, keep, mat, dropped


def daily_returns(mat):
    """简单日收益率。用简单收益（不是对数）因为要和权重线性加总。"""
    if len(mat) < 2:
        return np.zeros((0, mat.shape[1] if mat.ndim == 2 else 0))
    return mat[1:] / mat[:-1] - 1.0


def window(dates, rets, days):
    """取最近 N 个交易日的收益率。不足则返回全部。"""
    if len(rets) == 0:
        return [], rets
    n = min(days, len(rets))
    return dates[-n:], rets[-n:]


# ---------- 单票指标 ----------

def annualized_vol(r, periods=252):
    """年化波动率 = 日收益标准差 × √252。样本标准差（ddof=1）。"""
    if len(r) < 2:
        return None
    return float(np.std(r, ddof=1) * math.sqrt(periods))


def beta_alpha(r_asset, r_bench):
    """
    对基准做 OLS：r_asset = α + β·r_bench + ε
    返回 (β, α_日均, R², 残差序列)。残差是归因引擎（P3）的输入。
    """
    n = min(len(r_asset), len(r_bench))
    if n < 20:
        return None, None, None, np.zeros(0)
    y, x = np.asarray(r_asset[-n:], float), np.asarray(r_bench[-n:], float)
    vx = np.var(x, ddof=1)
    if vx == 0:
        return None, None, None, np.zeros(0)
    beta = float(np.cov(y, x, ddof=1)[0, 1] / vx)
    alpha = float(np.mean(y) - beta * np.mean(x))
    resid = y - (alpha + beta * x)
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = float(1 - np.sum(resid ** 2) / ss_tot) if ss_tot > 0 else None
    return beta, alpha, r2, resid


# ---------- 集中度 ----------

def hhi(weights):
    """赫芬达尔指数 Σwᵢ²。1 = 全押一只，越小越分散。"""
    return float(sum(w * w for w in weights.values()))


def effective_n(weights):
    """有效持仓数 1/Σwᵢ²。
    含义：你名义上持有 N 只，但集中度等价于持有这么多只等权股票。
    这是 P2 的核心指标 —— 「持有 9 只但有效只有 3.2 只」比「持有 9 只」信息量大得多。"""
    h = hhi(weights)
    return (1.0 / h) if h > 0 else None


def concentration(weights):
    ws = sorted(weights.values(), reverse=True)
    return {
        "n": len(ws),
        "effective_n": effective_n(weights),
        "hhi": hhi(weights),
        "top1": ws[0] if ws else 0.0,
        "top3": sum(ws[:3]),
        "top5": sum(ws[:5]),
    }


# ---------- 组合层 ----------

def corr_matrix(rets, tickers):
    """两两相关性。返回 {(a,b): ρ}，只存上三角。"""
    if len(rets) < 20:
        return {}
    c = np.corrcoef(rets, rowvar=False)
    out = {}
    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            v = c[i, j]
            out[(tickers[i], tickers[j])] = None if np.isnan(v) else float(v)
    return out


def avg_pairwise_corr(corrs, weights=None):
    """平均两两相关性。给了权重就按 wᵢ·wⱼ 加权（更反映实际风险暴露）。"""
    vals = [(k, v) for k, v in corrs.items() if v is not None]
    if not vals:
        return None
    if weights is None:
        return float(np.mean([v for _, v in vals]))
    num = den = 0.0
    for (a, b), v in vals:
        w = weights.get(a, 0) * weights.get(b, 0)
        num += w * v
        den += w
    return float(num / den) if den > 0 else None


def portfolio_vol(weights, rets, tickers, periods=252):
    """组合年化波动率 σ_p = √(wᵀΣw) × √252。"""
    idx = [i for i, t in enumerate(tickers) if t in weights]
    if len(idx) < 2 or len(rets) < 20:
        return None
    w = np.array([weights[tickers[i]] for i in idx], float)
    w = w / w.sum()
    cov = np.cov(rets[:, idx], rowvar=False, ddof=1)
    var = float(w @ cov @ w)
    return math.sqrt(max(var, 0.0)) * math.sqrt(periods)


def diversification_ratio(weights, rets, tickers, periods=252):
    """
    分散化比率 DR = (Σwᵢσᵢ) / σ_p
    =1 表示完全没有分散效果（全部同向），越大分散效果越好。
    直觉上比相关性矩阵更容易一眼看懂"我到底分散了没有"。
    """
    idx = [i for i, t in enumerate(tickers) if t in weights]
    if len(idx) < 2 or len(rets) < 20:
        return None
    w = np.array([weights[tickers[i]] for i in idx], float)
    w = w / w.sum()
    sig = np.array([np.std(rets[:, i], ddof=1) for i in idx], float) * math.sqrt(periods)
    sp = portfolio_vol(weights, rets, tickers, periods)
    if not sp:
        return None
    return float((w @ sig) / sp)


def portfolio_beta(weights, rets, tickers, bench_idx):
    """组合 β = Σwᵢβᵢ（对同一基准做的单变量回归）。"""
    if bench_idx is None or len(rets) < 20:
        return None
    rb = rets[:, bench_idx]
    tot = num = 0.0
    for i, t in enumerate(tickers):
        if t not in weights or i == bench_idx:
            continue
        b, _, _, _ = beta_alpha(rets[:, i], rb)
        if b is None:
            continue
        num += weights[t] * b
        tot += weights[t]
    return float(num / tot) if tot > 0 else None


def effective_bets(weights, rets, tickers):
    """
    有效独立赌注数 —— 把相关性也算进去的「我到底押了几个方向」。

    为什么需要它：「有效持仓数 1/Σwᵢ²」只看权重分散，完全没看相关性。
    26 只半导体股均分权重，1/Σwᵢ² 会漂亮地报 26 —— 但它们同涨同跌，
    实际上只是一个赌注。

    实现用 DR²（分散化比率的平方）。对等权、等波动、两两相关性均为 ρ 的组合，
    理论值是 1/(ρ + (1−ρ)/n)，DR² 在合成数据上与它逐点吻合（见 tests/）。

    ⚠️ 这里换过一次实现。最初用的是 Meucci 基于 PCA 的 ENB
    （exp(−Σpᵢlnpᵢ)，pᵢ 为各主成分的方差贡献占比），但用合成数据一校准就发现它是坏的：
    25 个**完全独立**的资产只给出 13.5（应为 25），ρ=0.3 时给出 1.01（应为约 3）。
    原因是等权组合天然对齐第一主成分，导致 97% 的方差全落在 PC1 上，
    与实际有多少独立风险源无关 —— Meucci 本人为此提出 minimum-torsion 变换。
    DR² 没有这个病，且能直接对照解析解，所以选它。
    """
    dr = diversification_ratio(weights, rets, tickers)
    return (dr ** 2) if dr else None
