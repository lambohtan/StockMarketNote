#!/usr/bin/env python3
"""
分析层的数值校准测试。运行：python3 tests/test_analysis.py

这些测试存在的理由：有效独立赌注数第一版用 Meucci 的 PCA-ENB 实现，
在真实组合上给出 1.21，看起来「很有洞察力」所以差点就那么发出去了。
用合成数据一校准才发现 25 个完全独立的资产它也只报 13.5 —— 指标是坏的。

凡是「一眼看不出对不对」的统计量，都必须有一个已知解析解的合成数据做锚。
"""
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from sw.analysis import risk as RK
from sw.analysis import stress as ST

FAIL = []

def check(name, got, want, tol):
    ok = got is not None and abs(got - want) <= tol
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got if got is None else round(got,3)}，"
          f"期望 {round(want,3)} ±{tol}")
    if not ok:
        FAIL.append(name)


def equicorr_returns(n, T, rho, seed=0, vol=0.02):
    """生成两两相关性均为 rho、等波动的 n 个资产的日收益。"""
    rng = np.random.default_rng(seed)
    f = rng.normal(0, 1, (T, 1))
    e = rng.normal(0, 1, (T, n))
    return (math.sqrt(rho) * f + math.sqrt(1 - rho) * e) * vol


def test_effective_bets():
    """等权等波动、两两相关性 ρ 时，独立赌注数的解析解是 1/(ρ + (1−ρ)/n)。"""
    print("\n有效独立赌注数 DR² vs 解析解 1/(ρ+(1−ρ)/n)")
    n, T = 25, 2000
    tks = [f"T{i}" for i in range(n)]
    w = {t: 1 / n for t in tks}
    for rho in (0.0, 0.1, 0.3, 0.6, 0.9):
        r = equicorr_returns(n, T, rho, seed=42)
        want = 1.0 / (rho + (1 - rho) / n)
        check(f"ρ={rho}", RK.effective_bets(w, r, tks), want, max(0.12 * want, 0.3))


def test_effective_n():
    """有效持仓数 1/Σwᵢ²：等权时应等于持仓数；单只独大时应趋近 1。"""
    print("\n有效持仓数 1/Σwᵢ²")
    check("10 只等权", RK.effective_n({f"T{i}": 0.1 for i in range(10)}), 10.0, 1e-9)
    check("一只占 99%", RK.effective_n({"A": 0.99, "B": 0.01}), 1.0203, 1e-3)


def test_vol_and_beta():
    """年化波动率与 β 的定义校验。"""
    print("\n波动率与 β")
    rng = np.random.default_rng(1)
    bench = rng.normal(0, 0.01, 3000)
    check("年化波动率（日波动 1%）", RK.annualized_vol(bench), 0.01 * math.sqrt(252), 0.01)
    asset = 1.5 * bench + rng.normal(0, 0.001, 3000)
    b, alpha, r2, resid = RK.beta_alpha(asset, bench)
    check("β（构造为 1.5）", b, 1.5, 0.02)
    check("R²（噪声很小应接近 1）", r2, 1.0, 0.02)


def test_corr():
    print("\n相关性")
    r = equicorr_returns(6, 3000, 0.5, seed=7)
    tks = [f"T{i}" for i in range(6)]
    c = RK.corr_matrix(r, tks)
    check("平均两两相关性（构造为 0.5）", RK.avg_pairwise_corr(c), 0.5, 0.05)


def test_panel_not_truncated_by_new_listing():
    """一只只有少量历史的新股，不能把整个面板压缩到它的长度。"""
    print("\n价格面板：新股不应拖短所有人的窗口")

    class FakeStore:
        def price_history(self, tickers, start=None):
            rows = []
            days = [f"2026-0{1 + i // 28}-{i % 28 + 1:02d}" for i in range(100)]
            for i, d in enumerate(days):
                for t in ("SPY", "OLD1", "OLD2"):
                    rows.append({"d": d, "ticker": t, "close": 100.0 + i})
                if i >= 90:                       # NEW 只有最后 10 天
                    rows.append({"d": d, "ticker": "NEW", "close": 50.0 + i})
            return rows

    dates, keep, mat, dropped = RK.price_panel(
        FakeStore(), ["SPY", "OLD1", "OLD2", "NEW"], window=60, anchor="SPY")
    check("保留的交易日数", float(len(dates)), 61.0, 0)
    print(f"  {'✅' if keep == ['OLD1','OLD2','SPY'] else '❌'} 保留的票: {keep}")
    print(f"  {'✅' if [d[0] for d in dropped] == ['NEW'] else '❌'} 剔除的票: {dropped}")
    if keep != ["OLD1", "OLD2", "SPY"]:
        FAIL.append("panel keep")
    if [d[0] for d in dropped] != ["NEW"]:
        FAIL.append("panel dropped")


def test_stress_path():
    """组合路径的最大回撤必须捕捉区间内的低点，而不是只看端点。"""
    print("\n压力测试：回撤要看过程不看端点")
    # 先跌 30% 再涨回来，端点收益 0%，但回撤应为 -30%
    days = [f"2020-01-{i:02d}" for i in range(1, 11)]
    prices = [100, 95, 90, 80, 70, 75, 85, 95, 100, 100]
    series = {"A": list(zip(days, prices))}
    _, path, mdd = ST._portfolio_path(series, {"A": 1.0}, 1.0)
    check("端点收益", float(path[-1]), 0.0, 1e-9)
    check("最大回撤", mdd, -0.30, 1e-9)


if __name__ == "__main__":
    test_effective_bets()
    test_effective_n()
    test_vol_and_beta()
    test_corr()
    test_panel_not_truncated_by_new_listing()
    test_stress_path()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
