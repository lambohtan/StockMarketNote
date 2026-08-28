"""
历史压力测试。

方法：把「今天的权重」放回历史上几段已知的下跌行情里，看这个组合当时会经历什么。

⚠️ 三条必须写进报告的前提：
  1. 用的是当前权重，不是当时的真实持仓 —— 这是「如果那时我持有现在这些票」的假想
  2. 当时不存在或未上市的票用行业 ETF 代理，报告里逐只标出
  3. 历史不重演。这是量级参照（"这类组合在那种环境里掉过 40%"），不是预测
"""
import numpy as np


def _series(store, ticker, start, end):
    rows = store.q(
        "SELECT d, close FROM prices WHERE ticker=? AND d>=? AND d<=? ORDER BY d",
        (ticker, start, end))
    return [(r["d"], float(r["close"])) for r in rows if r["close"] is not None]


def _window_stats(s):
    """返回 (区间收益, 区间内最大回撤)。"""
    if len(s) < 2:
        return None, None
    v = np.array([x[1] for x in s], float)
    ret = float(v[-1] / v[0] - 1.0)
    peak = np.maximum.accumulate(v)
    mdd = float(np.min(v / peak - 1.0))
    return ret, mdd


def _portfolio_path(series, weights, covered):
    """
    组合的日线归一化路径：Iₜ = Σ (wᵢ/covered) × (pᵢ,ₜ / pᵢ,₀) − 1

    只报区间端点收益会骗人 —— 2018Q4 如果窗口停在 12-26（+5% 反弹日），
    看起来比真实低点浅了一大截。所以必须同时给出区间内的最深处。
    只保留所有票都有报价的交易日，避免个别停牌日制造假跳空。
    """
    if not series:
        return [], np.zeros(0), None
    common = None
    for s in series.values():
        ds = {d for d, _ in s}
        common = ds if common is None else (common & ds)
    days = sorted(common or [])
    if len(days) < 2:
        return [], np.zeros(0), None
    path = np.zeros(len(days))
    for tk, s in series.items():
        m = dict(s)
        base = m[days[0]]
        if not base:
            continue
        path += (weights[tk] / covered) * np.array([m[d] / base for d in days], float)
    peak = np.maximum.accumulate(path)
    mdd = float(np.min(path / peak - 1.0))
    return days, path - 1.0, mdd


def run_scenario(store, name, start, end, weights, proxy_of=None, min_days=10):
    """
    proxy_of: {ticker: 代理ETF}，当票在该区间没有数据时使用。
    返回单个情景的结果 dict。
    """
    proxy_of = proxy_of or {}
    per, missing, proxied, series = {}, [], [], {}
    real_w = 0.0   # 用真实价格（非代理）覆盖到的权重
    for tk in weights:
        s = _series(store, tk, start, end)
        if len(s) < min_days:
            px = proxy_of.get(tk)
            s2 = _series(store, px, start, end) if px else []
            if len(s2) >= min_days:
                s = s2
                proxied.append((tk, px))
            else:
                missing.append(tk)
                continue
        else:
            real_w += weights[tk]
        per[tk] = _window_stats(s)
        series[tk] = s

    covered = sum(weights[t] for t in per)
    if covered <= 0:
        return {"name": name, "start": start, "end": end, "ok": False,
                "missing": missing, "coverage": 0.0}

    # 权重重新归一到有数据的部分，否则缺数据的票会被当成 0 收益，低估跌幅
    port_ret = sum(weights[t] / covered * per[t][0] for t in per)
    path_dates, path, port_mdd = _portfolio_path(series, weights, covered)

    return {
        "name": name, "start": start, "end": end, "ok": True,
        "coverage": covered,
        "real_coverage": real_w,
        "proxy_coverage": covered - real_w,
        "portfolio_return": port_ret,
        "portfolio_max_drawdown": port_mdd,
        "trough_date": (path_dates[int(np.argmin(path))] if len(path) else None),
        "trough_return": (float(np.min(path)) if len(path) else None),
        "per_ticker": {t: {"return": v[0], "max_drawdown": v[1]} for t, v in per.items()},
        "worst": sorted(((t, v[0]) for t, v in per.items()), key=lambda x: x[1])[:5],
        "best": sorted(((t, v[0]) for t, v in per.items()), key=lambda x: -x[1])[:3],
        "proxied": proxied,
        "missing": missing,
    }


def run_all(store, scenarios, weights, proxy_of=None, benchmarks=("SPY", "QQQ")):
    """scenarios: [{name, start, end}, ...]，来自 config.yaml。"""
    out = []
    for sc in scenarios:
        r = run_scenario(store, sc["name"], sc["start"], sc["end"], weights, proxy_of)
        r["benchmarks"] = {}
        for b in benchmarks:
            s = _series(store, b, sc["start"], sc["end"])
            ret, mdd = _window_stats(s)
            if ret is not None:
                r["benchmarks"][b] = {"return": ret, "max_drawdown": mdd}
        r["note"] = sc.get("note", "")
        out.append(r)
    return out
