#!/usr/bin/env python3
"""
P2 组合分析入口 —— 生成组合体检报告。

用法：
  python3 run_portfolio.py                                  # 用库里最近一次持仓快照
  python3 run_portfolio.py --activity ~/Downloads/History.csv   # 带交易流水，可算 TWR
  python3 run_portfolio.py --dry-run --activity xxx.csv     # 只校验流水解析，不出报告
  python3 run_portfolio.py --no-fetch                       # 完全离线，只用库里已有数据

报告同时写入 reports 表和 reports/ 目录。
"""
import argparse, sys, json
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sw.config import CFG
from sw.store import Store
from sw.analysis import portfolio as PF, risk as RK, overlap as OV, stress as ST, returns as RT
from sw.analysis.report import render
from sw.sources import prices as P, etf as ETF

PRICE_COLS = ["d", "ticker", "close", "volume"]


def log(msg):
    print(msg, flush=True)


def ensure_prices(st, tickers, period, dry):
    """补齐行情。缺失或超过 3 天没更新就重抓。"""
    if dry:
        return
    res = P.fetch_prices(sorted(tickers), period=period)
    st.log_health(res.source, res.ok, res.latency_ms, res.detail)
    if res.ok:
        st.upsert_many("prices", PRICE_COLS, res.data)
        log(f"  ✅ 行情 {res.rows} 行 · {res.detail}")
    else:
        log(f"  ❌ 行情抓取失败：{res.detail}（继续用库里已有数据）")


def ensure_window(st, tickers, start, end, label, dry):
    """压力测试区间的历史行情。库里已经覆盖就跳过，避免重复请求。"""
    have = {r["ticker"] for r in st.q(
        "SELECT DISTINCT ticker FROM prices WHERE d>=? AND d<=?", (start, end))}
    need = sorted(set(tickers) - have)
    if not need or dry:
        return
    res = P.fetch_prices_range(need, start, (date.fromisoformat(end) + timedelta(days=1)).isoformat())
    st.log_health(res.source, res.ok, res.latency_ms, f"{label} {res.detail}")
    if res.ok:
        st.upsert_many("prices", PRICE_COLS, res.data)
        log(f"  ✅ {label}：补 {len(need)} 只 / {res.rows} 行")
    else:
        log(f"  ⚠️ {label}：{res.detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--activity", help="Fidelity Activity/History CSV，用于算 TWR")
    ap.add_argument("--snapshot-date", help="指定持仓快照日期，默认最近一次")
    ap.add_argument("--corr-window", type=int, default=60, help="相关性窗口（交易日），默认 60")
    ap.add_argument("--no-fetch", action="store_true", help="完全离线，不联网补数据")
    ap.add_argument("--dry-run", action="store_true", help="只校验解析，不出报告不写库")
    ap.add_argument("--out", help="报告输出路径，默认 reports/portfolio_<日期>.md")
    a = ap.parse_args()
    dry = a.dry_run or a.no_fetch

    st = Store(CFG.db_path)

    # ---------- 交易流水（可选，先校验解析再干别的） ----------
    txns, act_rep = [], None
    if a.activity:
        log("=" * 60 + "\n交易流水解析\n" + "=" * 60)
        from sw.fidelity_activity import parse_file as parse_act
        txns, act_rep = parse_act(a.activity)
        log(f"  解析 {act_rep['parsed']} 条，区间 {act_rep['date_range']}")
        log(f"  识别到的字段：{sorted(set(act_rep['mapped'].values()))}")
        log(f"  原始列名：{act_rep['detected_columns']}")
        log(f"  动作分类：{act_rep['kinds']}")
        if act_rep["unclassified"]:
            log(f"  ⚠️ 未能分类的动作（可能需要补关键词）：{act_rep['unclassified']}")
        if act_rep["skipped_total"]:
            log(f"  跳过 {act_rep['skipped_total']} 行：{act_rep['skipped'][:5]}")
        if a.dry_run:
            log("\n（dry-run：只校验流水解析，未生成报告）")
            return

    # ---------- 组合 ----------
    log("=" * 60 + "\n1 / 6  持仓\n" + "=" * 60)
    try:
        p = PF.build(st, CFG, a.snapshot_date)
    except ValueError as e:
        log(f"  ❌ {e}")
        return
    log(f"  快照 {p.snapshot_date} · 股票 {len(p.holdings)} 只 · "
        f"市值 ${p.equity_value:,.0f} · 现金 ${p.cash:,.0f}")
    if not p.holdings:
        log("  ❌ 快照里没有股票持仓，无法分析")
        return

    bm = CFG.get("benchmarks", {}) or {}
    sector_etf = bm.get("sectors") or {}
    bench = [bm.get("market", "SPY"), bm.get("growth", "QQQ")]
    lt_etfs = [t for t in (CFG.get("portfolio.look_through_etfs") or []) ]
    universe = set(p.tickers) | set(bench) | set(sector_etf.values()) | set(lt_etfs)

    # ---------- 行情 ----------
    log("\n" + "=" * 60 + "\n2 / 6  行情\n" + "=" * 60)
    if a.no_fetch:
        log("  跳过（--no-fetch）")
    else:
        ensure_prices(st, universe, CFG.get("history.price_period", "2y"), False)

    # ---------- ETF 成分 ----------
    log("\n" + "=" * 60 + "\n3 / 6  ETF 成分与穿透\n" + "=" * 60)
    etf_map, qqq_sectors = {}, {}
    held_etfs = [h.ticker for h in p.holdings if h.ticker in lt_etfs or h.is_fund]
    want = sorted(set(lt_etfs) | set(held_etfs))
    if not a.no_fetch and want:
        eres = ETF.fetch_top_holdings(want)
        st.log_health(eres.source, eres.ok, eres.latency_ms, eres.detail)
        if eres.ok:
            etf_map = eres.data
            log(f"  ✅ {len(etf_map)} 个 ETF 的前十大成分 {eres.detail}")
        else:
            log(f"  ⚠️ 取不到 ETF 成分：{eres.detail}（重叠度与穿透将留空）")
    else:
        log("  跳过")
    qqq_sectors = OV.sector_look_through({"QQQ": 1.0}, [], {"QQQ": etf_map["QQQ"]}) if "QQQ" in etf_map else {}

    # 穿透只展开「组合里真的持有的」ETF
    held_map = {k: v for k, v in etf_map.items() if k in set(p.tickers)}

    # ---------- 风险指标 ----------
    log("\n" + "=" * 60 + "\n4 / 6  风险与相关性\n" + "=" * 60)
    w_all = p.weights()
    w_sat = p.weights(exclude_core=True)
    start = (date.today() - timedelta(days=int(a.corr_window * 2.2) + 60)).isoformat()
    panel_tks = sorted(set(p.tickers) | set(bench))
    dates, tks, mat, dropped = RK.price_panel(
        st, panel_tks, start=start, anchor=bm.get("market", "SPY"),
        window=a.corr_window)
    rets = RK.daily_returns(mat)
    rdates, rets = RK.window(dates[1:], rets, a.corr_window)
    if dropped:
        log(f"  ⚠️ 历史不足、已排除出相关性面板：{[d[0] for d in dropped]}")

    corr_tks = [t for t in tks if t in w_all]
    idx = [tks.index(t) for t in corr_tks]
    corrs, corr_rows, high_pairs = {}, [], []
    if len(rets) >= 20 and len(idx) >= 2:
        sub = rets[:, idx]
        corrs = RK.corr_matrix(sub, corr_tks)
        import numpy as np
        cm = np.corrcoef(sub, rowvar=False)
        corr_rows = [[None if np.isnan(cm[i, j]) else float(cm[i, j])
                      for j in range(len(corr_tks))] for i in range(len(corr_tks))]
        high_pairs = sorted(((x, y, v) for (x, y), v in corrs.items() if v is not None and v >= 0.7),
                            key=lambda z: -z[2])

    bench_idx = tks.index(bm.get("market", "SPY")) if bm.get("market", "SPY") in tks else None
    riskstats = {
        "window_days": len(rets),
        "start": rdates[0] if len(rdates) else "—",
        "end": rdates[-1] if len(rdates) else "—",
        "port_vol": RK.portfolio_vol(w_all, rets, tks),
        "bench_vol": {b: RK.annualized_vol(rets[:, tks.index(b)]) for b in bench if b in tks},
        "beta": RK.portfolio_beta(w_all, rets, tks, bench_idx),
        "avg_corr": RK.avg_pairwise_corr(corrs),
        "avg_corr_w": RK.avg_pairwise_corr(corrs, w_all),
        "div_ratio": RK.diversification_ratio(w_all, rets, tks),
        "eff_bets": RK.effective_bets(w_all, rets, tks),
        "corr_tickers": corr_tks, "corr_rows": corr_rows, "high_pairs": high_pairs,
        "dropped": dropped,
        "covered_weight": sum(w_all.get(t, 0) for t in corr_tks),
    }
    log(f"  窗口 {riskstats['window_days']} 日 · 组合波动率 "
        f"{(riskstats['port_vol'] or 0)*100:.1f}% · β {riskstats['beta'] or float('nan'):.2f} · "
        f"加权平均相关性 {riskstats['avg_corr_w'] or float('nan'):.2f}")

    # ---------- 压力测试 ----------
    log("\n" + "=" * 60 + "\n5 / 6  历史压力测试\n" + "=" * 60)
    scenarios = CFG.get("stress_scenarios") or []
    proxy_of = {h.ticker: (sector_etf.get(h.sector) or bm.get("market", "SPY"))
                for h in p.holdings}
    need_tks = sorted(set(p.tickers) | set(proxy_of.values()) | set(bench))
    if not a.no_fetch:
        for sc in scenarios:
            ensure_window(st, need_tks, sc["start"], sc["end"], sc["name"], False)
    stress = ST.run_all(st, scenarios, w_all, proxy_of, benchmarks=tuple(bench))
    for s in stress:
        log(f"  {s['name']:<16} {'组合 ' + format(s['portfolio_return']*100, '+.1f') + '%' if s['ok'] else '数据不足'}")

    # ---------- TWR ----------
    log("\n" + "=" * 60 + "\n6 / 6  收益率\n" + "=" * 60)
    simple = RT.simple_return(p)
    log(f"  简单收益 {(simple['gain_pct'] or 0)*100:+.1f}%")
    twr = {"ok": False, "reason": "未提供 Activity/History CSV（用 --activity 指定）"}
    if txns:
        # 只取真实买卖标的：SPAXX 这类货币基金 sweep 不是投资决策，
        # 喂给 yfinance 还会报 "Period 'max' is invalid"
        trade_tks = sorted({t["ticker"] for t in txns
                            if t["ticker"] and t["kind"] in ("buy", "sell")})
        first = min(t["date"] for t in txns)
        if not a.no_fetch:
            ensure_window(st, sorted(set(trade_tks) | set(bench)), first,
                          p.snapshot_date, "TWR 区间", False)
        splits, serr = ({}, [])
        if not a.no_fetch:
            splits, serr = RT.split_factors(trade_tks, first)
            if splits:
                log(f"  拆股记录：{splits}")
        twr = RT.twr(st, p, txns, bench=tuple(bench), splits=splits)
        log(f"  TWR {'%.1f%%' % (twr['twr']*100) if twr.get('ok') else twr.get('reason')}")

    # ---------- 渲染 ----------
    ctx = {
        "portfolio": p, "simple": simple, "twr": twr,
        "conc_all": RK.concentration(w_all), "conc_sat": RK.concentration(w_sat),
        "sector_direct": p.sector_weights(),
        "sector_through": OV.sector_look_through(w_all, p.holdings, held_map),
        "qqq_sectors": qqq_sectors,
        "riskstats": riskstats,
        "overlaps": {e: OV.overlap(w_all, etf_map[e]["holdings"]) | {"etf_coverage": etf_map[e]["coverage"]}
                     for e in lt_etfs if e in etf_map},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    lt, unpen = OV.look_through(w_all, held_map)
    ctx["look_through"], ctx["unpenetrated"] = lt, unpen
    ctx["stress"] = stress

    md = render(ctx)
    out = Path(a.out) if a.out else Path(__file__).resolve().parent / "reports" / f"portfolio_{p.snapshot_date}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    st.upsert_many("reports", ["d", "kind", "body_md", "created_at"],
                   [(p.snapshot_date, "portfolio", md, ctx["generated_at"])])
    log(f"\n✅ 报告已生成：{out}")
    log(f"   （同时写入 reports 表，d={p.snapshot_date} kind=portfolio）")
    st.close()


if __name__ == "__main__":
    main()
