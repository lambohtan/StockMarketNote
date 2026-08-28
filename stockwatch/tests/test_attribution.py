#!/usr/bin/env python3
"""归因引擎的校准测试。运行：python3 tests/test_attribution.py

沿用 tests/test_analysis.py 确立的规矩：
凡是一眼看不出对错的统计量，必须有已知解析解的合成数据做锚。

这里构造「已知 β、已知残差」的序列，验证引擎能把它们还原出来，
并且能在注入冲击时检出 2σ / 4σ。
"""
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from sw.analysis import attribution as AT

FAIL = []

def check(name, got, want, tol):
    ok = got is not None and abs(got - want) <= tol
    print(f"  {'✅' if ok else '❌'} {name}: 得到 "
          f"{'None' if got is None else round(got, 4)}，期望 {round(want, 4)} ±{tol}")
    if not ok:
        FAIL.append(name)

def check_eq(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def test_ols2_recovers_known_betas():
    """构造 y = 0.0004 + 1.30·mkt + 0.75·sector + 噪声，看能不能还原。"""
    print("\n双因子回归还原已知 β")
    rng = np.random.default_rng(11)
    n = 400
    mkt = rng.normal(0, 0.010, n)
    sec = rng.normal(0, 0.008, n)
    noise = rng.normal(0, 0.004, n)
    y = 0.0004 + 1.30 * mkt + 0.75 * sec + noise

    r = AT.ols2(y, mkt, sec)
    check("β_市场（构造 1.30）", r["b1"], 1.30, 0.05)
    check("β_行业（构造 0.75）", r["b2"], 0.75, 0.06)
    check("α（构造 0.0004）", r["alpha"], 0.0004, 0.0005)
    check("残差标准差（构造 0.004）", float(np.std(r["resid"], ddof=1)), 0.004, 0.0005)


def test_detects_injected_shock():
    """最后一天注入一个 3σ 的个股冲击，必须被检出为 anomaly。"""
    print("\n注入冲击的检出")
    rng = np.random.default_rng(7)
    n = 200
    sigma = 0.004
    mkt = rng.normal(0, 0.010, n)
    sec = rng.normal(0, 0.008, n)
    noise = rng.normal(0, sigma, n)
    # 把注入日自身的随机噪声清零，只留下我们要检验的 3σ 冲击 ——
    # 否则那天自己的噪声draw会和注入的冲击叠加/抵消（这里 seed=7 时该噪声
    # 恰好是 -1.0σ，几乎抵消了一整个 σ 的注入量，z 只剩 ~2.19，
    # 让"注入 3σ 应检出约 3σ"这个断言变得看运气而非看实现对不对）。
    noise[-1] = 0.0
    y = 1.20 * mkt + 0.60 * sec + noise
    y[-1] += 3.0 * sigma                      # ← 3σ 的个股独立冲击

    r = AT.ols2(y, mkt, sec)
    z = r["resid"][-1] / np.std(r["resid"][:-1], ddof=1)
    check("最后一天的 z 值约为 3", float(z), 3.0, 0.6)
    check_eq("被分类为 anomaly", AT.classify(float(z)), "anomaly")


def test_classify_thresholds():
    print("\n阈值分类")
    check_eq("1.5σ 是常规波动", AT.classify(1.5), "normal")
    check_eq("-1.5σ 也是常规", AT.classify(-1.5), "normal")
    check_eq("2.5σ 是异动", AT.classify(2.5), "anomaly")
    check_eq("-2.5σ 也是异动", AT.classify(-2.5), "anomaly")
    check_eq("4.5σ 是极端", AT.classify(4.5), "extreme")
    check_eq("-4.5σ 也是极端", AT.classify(-4.5), "extreme")


def test_decomposition_adds_up():
    """收益必须能拆成三块且加得回去 —— 报告里每个数字都要可追溯。"""
    print("\n收益分解的自洽性")
    rng = np.random.default_rng(3)
    n = 150
    mkt = rng.normal(0, 0.010, n)
    sec = rng.normal(0, 0.008, n)
    y = 0.0002 + 1.10 * mkt + 0.90 * sec + rng.normal(0, 0.003, n)
    r = AT.ols2(y, mkt, sec)
    # 最后一天：ret ≈ alpha + b1·mkt + b2·sec + resid
    recon = r["alpha"] + r["b1"] * mkt[-1] + r["b2"] * sec[-1] + r["resid"][-1]
    check("分解后能还原当日收益", float(recon), float(y[-1]), 1e-9)


def test_insufficient_data_returns_none():
    print("\n数据不足时诚实返回 None")
    rng = np.random.default_rng(1)
    short = rng.normal(0, 0.01, 5)
    r = AT.ols2(short, short, short)
    check_eq("样本太少返回 None", r, None)


if __name__ == "__main__":
    test_ols2_recovers_known_betas()
    test_detects_injected_shock()
    test_classify_thresholds()
    test_decomposition_adds_up()
    test_insufficient_data_returns_none()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
