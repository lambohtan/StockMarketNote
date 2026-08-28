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


class _FakeStore:
    """最小假 Store：只实现 attribute() 依赖的 .q(sql, args)。

    直接用价格序列驱动，绕开 ols2/classify 这两个已经被单测覆盖的
    低层函数，专门盯着 attribute() 内部那行「σ 用除当日以外的残差算」
    有没有被写对 —— 前面几个测试都只调 AT.ols2 后在测试代码里手算 z，
    从没真正跑过 attribute() 本体，这行代码改坏了也不会被测出来。
    """
    def __init__(self, rows_by_ticker):
        self.rows_by_ticker = rows_by_ticker

    def q(self, sql, args):
        ticker, start = args
        return [{"d": d, "close": c} for d, c in self.rows_by_ticker.get(ticker, [])
                if d >= start]


def _prices_from_returns(dates, rets, base=100.0):
    """给定收益率序列，倒推出对应的价格序列（首日价格为 base）。"""
    out = [(dates[0], base)]
    px = base
    for d, r in zip(dates[1:], rets):
        px = px * (1.0 + r)
        out.append((d, px))
    return out


def test_attribute_end_to_end_uses_out_of_sample_sigma():
    """端到端跑一次 attribute()：σ 必须是「除当日以外」算出来的那一个。

    这是唯一一个真正调用 AT.attribute() 的测试。之前的测试只测
    ols2/classify 这些零件，没有测总装——如果有人把 attribute() 里
    「σ 用除当日以外的残差算」这行改成把当日也算进去，前面所有测试
    依然全绿，因为它们根本没跑到这行代码。

    这里不用「z 大约是 3，容差 ±0.6」这种统计上松的判据 —— 用 n=120
    这种正常样本量时，多算进 1 个点对 σ 的影响本来就只有几个百分点，
    松容差根本分辨不出「排除当日」和「包含当日」两种写法的差异
    （实测：这个 bug 曾经在这样的松判据下全绿放过）。
    改用已知解析解：在测试代码里独立跑一遍同样的 ols2，手算出
    「排除当日」的 σ_correct，要求 attribute() 返回的 z 与
    resid[-1]/σ_correct 精确相等（容差 1e-9）——这样任何把 σ 算错
    （包括当日、少减一个自由度、算错切片方向等）都会被精确捕获，
    不依赖注入冲击的大小或样本量凑巧放大差异。
    """
    print("\nattribute() 端到端：σ 排除当日（解析解精确锚定）")
    rng = np.random.default_rng(42)
    n = 120
    sigma = 0.005
    dates = [f"2026-01-{i:04d}" for i in range(n + 1)]   # 只需单调递增，不必是真日历
    mkt_ret = rng.normal(0, 0.010, n)
    sec_ret = rng.normal(0, 0.008, n)
    noise = rng.normal(0, sigma, n)
    noise[-1] = 0.0                       # 排除同日噪声对注入冲击的污染（理由见上一测试）
    stock_ret = 1.10 * mkt_ret + 0.70 * sec_ret + noise
    stock_ret[-1] += 3.0 * sigma          # 注入 3σ 的个股独立冲击

    rows = {
        "TICK": _prices_from_returns(dates, stock_ret),
        "SPY": _prices_from_returns(dates, mkt_ret),
        "XLK": _prices_from_returns(dates, sec_ret),
    }
    store = _FakeStore(rows)

    r = AT.attribute(store, "TICK", "XLK", market_etf="SPY", window=n, start=dates[0])

    # 解析解：独立地对同一组序列做同样的 OLS，手算「排除当日」与
    # 「包含当日」两种 σ，验证 attribute() 用的是前者、且明显不同于后者。
    fit_ref = AT.ols2(stock_ret, mkt_ret, sec_ret)
    resid_ref = fit_ref["resid"]
    sigma_correct = float(np.std(resid_ref[:-1], ddof=1))
    sigma_wrong = float(np.std(resid_ref, ddof=1))
    z_correct = float(resid_ref[-1] / sigma_correct)
    z_wrong = float(resid_ref[-1] / sigma_wrong)
    print(f"    （自查：σ_排除当日={sigma_correct:.6f} vs σ_包含当日={sigma_wrong:.6f}，"
          f"对应 z_correct={z_correct:.4f} vs z_wrong={z_wrong:.4f}）")

    check("端到端 z 与「排除当日」解析解精确一致", r["z"], z_correct, 1e-9)
    if abs(z_wrong - z_correct) < 1e-9:
        FAIL.append("测试锚点无区分度（σ_correct 与 σ_wrong 几乎相等，换随机种子/样本量）")
        print("  ❌ 测试锚点无区分度：σ_correct 与 σ_wrong 几乎相等")
    check_eq("端到端分类为 anomaly", r["level"], "anomaly")
    recon = r["mkt_part"] + r["sector_part"] + r["idio"]
    check("端到端三块拆解加回当日收益", recon, r["ret"], 1e-9)


def test_attribute_respects_min_obs_on_common_dates():
    """attribute() 里「共同交易日 < MIN_OBS 就返回 None」这道门槛必须真的挡住。

    这道门槛只在 attribute() 内部、对 common（三者交集后的交易日）判断，
    跟 ols2() 自己对样本量的把关是两码事——单靠 test_insufficient_data_
    returns_none（直接怼 ols2，n=5）测不到它：这里的 39 天已经远超
    ols2 的 MIN_OBS 检查所需的最少行数，唯一能拦住它的只有 attribute()
    这道「宁可不说」的门槛。之前没有测试真正构造出「样本刚好不够」的
    共同交易日场景，这道门槛改坏了（比如判断条件写成 < 2）不会被发现。
    """
    print("\nattribute() 的 MIN_OBS 门槛（共同交易日数）")
    rng = np.random.default_rng(5)
    n_short = AT.MIN_OBS - 1     # 39 天：刚好不够，必须被拒
    dates = [f"2026-02-{i:04d}" for i in range(n_short + 1)]
    mkt_ret = rng.normal(0, 0.010, n_short)
    sec_ret = rng.normal(0, 0.008, n_short)
    stock_ret = 1.0 * mkt_ret + 0.5 * sec_ret + rng.normal(0, 0.003, n_short)
    rows = {
        "TICK": _prices_from_returns(dates, stock_ret),
        "SPY": _prices_from_returns(dates, mkt_ret),
        "XLK": _prices_from_returns(dates, sec_ret),
    }
    store = _FakeStore(rows)
    r = AT.attribute(store, "TICK", "XLK", market_etf="SPY", window=60, start=dates[0])
    check_eq(f"共同交易日只有 {n_short} 天（< MIN_OBS={AT.MIN_OBS}）时诚实返回 None", r, None)

    # 反证：只多补 1 天凑够 MIN_OBS，同样的门槛就该放行，证明上面不是
    # 因为别的原因（比如日期格式、股票代码拼错）意外返回 None。
    n_ok = AT.MIN_OBS
    dates2 = [f"2026-03-{i:04d}" for i in range(n_ok + 1)]
    mkt2 = rng.normal(0, 0.010, n_ok)
    sec2 = rng.normal(0, 0.008, n_ok)
    stock2 = 1.0 * mkt2 + 0.5 * sec2 + rng.normal(0, 0.003, n_ok)
    rows2 = {
        "TICK": _prices_from_returns(dates2, stock2),
        "SPY": _prices_from_returns(dates2, mkt2),
        "XLK": _prices_from_returns(dates2, sec2),
    }
    r2 = AT.attribute(_FakeStore(rows2), "TICK", "XLK", market_etf="SPY",
                       window=60, start=dates2[0])
    ok = r2 is not None
    print(f"  {'✅' if ok else '❌'} 共同交易日凑够 {n_ok} 天（=MIN_OBS）时应放行: "
          f"得到 {'非 None' if ok else 'None'}")
    if not ok:
        FAIL.append(f"共同交易日凑够 {n_ok} 天（=MIN_OBS）时应放行")


if __name__ == "__main__":
    test_ols2_recovers_known_betas()
    test_detects_injected_shock()
    test_classify_thresholds()
    test_decomposition_adds_up()
    test_insufficient_data_returns_none()
    test_attribute_end_to_end_uses_out_of_sample_sigma()
    test_attribute_respects_min_obs_on_common_dates()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
