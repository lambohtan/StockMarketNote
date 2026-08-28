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


def _prices(store, ticker, start):
    """返回 {日期: 收盘价} —— 只是原始报价，不在这一层算收益率。

    收益率要按「交易日历上相邻的两天」算，不能按「价格表里相邻的两行」算
    （见 _calendar_returns 的说明），所以这里先只取价格，对齐交给上层。
    """
    rows = store.q("SELECT d, close FROM prices WHERE ticker=? AND d>=? ORDER BY d",
                   (ticker, start))
    return {r["d"]: float(r["close"]) for r in rows}


def _calendar_returns(prices, cal):
    """
    按权威交易日历 cal 逐日计算收益率：只有当 cal 里相邻的两天
    （不是价格表里随便相邻的两行）都能在 prices 里查到报价，才为后一天
    生成一个收益率观测点；否则跳过那一天，不生成观测点。

    ⚠️ 这里踩过一个真实的坑：原来的实现直接拿价格表里"相邻两行"算收益率，
    从不检查这两行的日期是不是真的相邻。个股某天因数据源抓取失败缺一行
    （source_health 机制预期会发生、也会容忍的常态，不是假设），下一行
    记录的"单日收益"就会变成两天的累计收益，而同一天的 SPY/XLK 因子仍是
    正常单日收益 —— 两者错位比较，会凭空造出异动、或者把真正发生在缺口
    那天的异动完全稀释掉，而且全程没有任何报错。
    改成按 cal 上的相邻日期算，缺口天直接不产出观测点，不会被静默折叠。
    """
    ret = {}
    for i in range(1, len(cal)):
        d_prev, d_cur = cal[i - 1], cal[i]
        if d_prev in prices and d_cur in prices:
            ret[d_cur] = prices[d_cur] / prices[d_prev] - 1.0
    return ret


def attribute(store, ticker, sector_etf, market_etf="SPY", window=60, start=None):
    """
    对单只票做归因。返回最后一个交易日的拆解结果，数据不足返回 None。

    sector_etf 为 None 时（行业未知、或是 ETF 本身）退化为单因子，
    b2 记 0 —— 不要为了凑双因子硬塞一个不相关的代理。

    交易日历以 market_etf（默认 SPY）的报价日期为权威 —— 和
    sw.analysis.risk.price_panel(anchor="SPY") 同一套模式，不发明第二套。
    个股/行业序列只在日历上「相邻两天都有报价」时才生成一个收益率观测点，
    某只序列的缺口天不会被静默折叠进下一天的收益率。
    """
    if start is None:
        from datetime import date, timedelta
        start = (date.today() - timedelta(days=int(window * 2.2) + 60)).isoformat()

    px_t = _prices(store, ticker, start)
    px_m = _prices(store, market_etf, start)
    if len(px_t) < 2 or len(px_m) < 2:
        return None

    cal = sorted(px_m.keys())           # 权威交易日历：SPY 的报价日期
    r_t = _calendar_returns(px_t, cal)
    r_m = _calendar_returns(px_m, cal)

    if sector_etf:
        px_s = _prices(store, sector_etf, start)
        r_s = _calendar_returns(px_s, cal)
    else:
        r_s = {d: 0.0 for d in r_m}

    # 只保留三者都能在日历上生成有效收益率的交易日；
    # 因为某只序列缺口而被排除的交易日记进 skipped_days，便于以后排查。
    common_all = sorted(set(r_t) & set(r_m) & set(r_s))
    if len(common_all) < MIN_OBS:
        return None
    skipped_days = sorted(set(cal[1:]) - set(common_all))
    common = common_all[-window:]
    y = np.array([r_t[d] for d in common], float)
    x1 = np.array([r_m[d] for d in common], float)
    x2 = np.array([r_s[d] for d in common], float)

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
        "skipped_days": skipped_days,
    }
