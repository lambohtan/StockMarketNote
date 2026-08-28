"""
组合体检报告的 Markdown 渲染。

原则：只陈述事实和数据，不出现「建议买入/卖出」「目标价」这类字眼。
每个数字后面都能指到具体公式和窗口长度。最后一节固定写明局限。
"""

def pct(v, nd=1):
    return "—" if v is None else f"{v*100:.{nd}f}%"

def money(v, nd=0):
    return "—" if v is None else f"${v:,.{nd}f}"

def num(v, nd=2):
    return "—" if v is None else f"{v:.{nd}f}"

def _sign(v, nd=1):
    return "—" if v is None else f"{'+' if v >= 0 else ''}{v*100:.{nd}f}%"


def render(c):
    """c 是 run_portfolio 组装的上下文 dict。"""
    p = c["portfolio"]
    L = []
    A = L.append

    A(f"# 组合体检报告 · {p.snapshot_date}")
    A("")
    A("> 本报告只陈述事实与数据，不含买卖建议、不含目标价。所有计算为确定性 Python，"
      "可逐项复核；窗口长度已标注在各指标旁。")
    A("")

    # ---------- 1 概览 ----------
    A("## 1 · 概览")
    A("")
    A(f"| | |\n|---|---|")
    A(f"| 股票市值 | {money(p.equity_value)} |")
    A(f"| 现金/货币基金 | {money(p.cash)} |")
    A(f"| 账户合计 | {money(p.total_value)} |")
    A(f"| 持仓只数 | {len(p.holdings)} |")
    if p.cash and p.total_value:
        A(f"| 现金占比 | {pct(p.cash/p.total_value)} |")
    accts = sorted({a for h in p.holdings for a in h.accounts})
    A(f"| 账户 | {', '.join(accts) or '—'} |")
    A("")

    # ---------- 2 收益 ----------
    A("## 2 · 收益")
    A("")
    s = c["simple"]
    A("**持有期简单收益**（相对成本，Fidelity 口径）")
    A("")
    A(f"- 成本 {money(s['cost'])} → 市值 {money(s['market_value'])}，"
      f"浮盈亏 {money(s['gain'])}（{_sign(s['gain_pct'])}）")
    if s["missing_cost"]:
        A(f"- ⚠️ 以下持仓无成本数据，未计入：{', '.join(s['missing_cost'])}")
    A("")
    A("> 这个数字受加仓时点影响：下跌途中不断买入会拉低成本，让亏损看起来比实际体验小。"
      "要衡量选股本身，看下面的 TWR。")
    A("")

    t = c["twr"]
    A("**时间加权收益率 TWR**（剔除资金进出时点影响，可与指数直接比较）")
    A("")
    if not t.get("ok"):
        A(f"- ⏸ 无法计算：{t.get('reason')}")
        A("- 需要 Fidelity 的 **Activity / History CSV**（过去 12 个月交易记录）才能算这一项。")
    else:
        A(f"- 区间 {t['start']} → {t['end']}（{t['trading_days']} 个交易日）")
        if t.get("short_window"):
            A(f"- **TWR {_sign(t['twr'])}**")
            A("")
            A(f"> ⚠️ **这个区间太短，参考价值有限，且刻意不做年化。**"
              f"交易流水只覆盖了 {t['trading_days']} 个交易日"
              f"（低于 {t.get('min_annualize_days', 126)} 天的年化门槛）。"
              "把三周的结果外推成一年是把运气当能力 —— 这种数字比没有数字更有害。"
              "要拿到有意义的 TWR，请到 Fidelity → Accounts → Activity 重新导出"
              "**至少 12 个月**的交易记录。")
            A("")
        else:
            A(f"- **TWR {_sign(t['twr'])}**，年化 {_sign(t['annualized'])}")
        A(f"- 年化波动率 {pct(t['vol_annualized'])}，区间最大回撤 {_sign(t['max_drawdown'])}")
        for b, x in t["benchmarks"].items():
            d = t["excess_vs"][b]
            A(f"- vs {b}：{b} 同期 {_sign(x['return'])}，"
              f"你{'跑赢' if d >= 0 else '跑输'} {_sign(abs(d))}")
        if t["unpriced_tickers"]:
            A(f"- ⚠️ 无价格数据、未计入市值的代码：{', '.join(t['unpriced_tickers'])}")
        if t["splits_applied"]:
            A(f"- 已做拆股换算：{', '.join(t['splits_applied'])}")
    A("")

    # ---------- 3 持仓明细 ----------
    A("## 3 · 持仓明细")
    A("")
    w = p.weights()
    A("| 代码 | 名称 | 权重 | 市值 | 成本 | 浮盈亏 | 行业 |")
    A("|---|---|---:|---:|---:|---:|---|")
    for h in p.holdings:
        tag = " ⚙️" if h.is_core else ""
        A(f"| **{h.ticker}**{tag} | {(h.name or h.description)[:26]} | {pct(w.get(h.ticker))} "
          f"| {money(h.market_value)} | {money(h.cost_basis)} | {_sign(h.gain_pct)} "
          f"| {h.sector or ('ETF/基金' if h.is_fund else '—')} |")
    A("")
    A("⚙️ = 核心仓（config.yaml `portfolio.core_tickers`），下方集中度另给一份剔除后的视角。")
    A("")

    # ---------- 4 集中度 ----------
    A("## 4 · 集中度")
    A("")
    A("| 指标 | 全部持仓 | 剔除核心仓 |")
    A("|---|---:|---:|")
    a, b = c["conc_all"], c["conc_sat"]
    A(f"| 持仓只数 | {a['n']} | {b['n']} |")
    A(f"| **有效持仓数 1/Σwᵢ²** | **{num(a['effective_n'])}** | **{num(b['effective_n'])}** |")
    A(f"| HHI (Σwᵢ²) | {num(a['hhi'], 3)} | {num(b['hhi'], 3)} |")
    A(f"| 第一大权重 | {pct(a['top1'])} | {pct(b['top1'])} |")
    A(f"| 前三合计 | {pct(a['top3'])} | {pct(b['top3'])} |")
    A(f"| 前五合计 | {pct(a['top5'])} | {pct(b['top5'])} |")
    A(f"| **有效独立赌注数 DR²（含相关性）** | **{num(c['riskstats'].get('eff_bets'))}** | — |")
    A("")
    if a["effective_n"]:
        eb = c["riskstats"].get("eff_bets")
        A(f"> 三个数字要连起来读：名义 **{a['n']} 只** → 按权重折算 **{a['effective_n']:.1f} 只**"
          + (f" → 再把相关性算进去，只剩 **{eb:.1f} 个独立赌注**。" if eb else "。"))
        if eb:
            A(">")
            A(f"> 前两个数字只看「钱分得散不散」，第三个才看「押的方向散不散」。"
              f"{a['n']} 只票均分权重也可能只是一个赌注 —— 如果它们同涨同跌。"
              f"下面 §6 的相关性矩阵是这个差距的来源。")
    A("")

    # ---------- 5 行业分布 ----------
    A("## 5 · 行业分布")
    A("")
    A("| 行业 | 直接持仓 | 穿透 ETF 后 | QQQ 参照 |")
    A("|---|---:|---:|---:|")
    direct, thru, qqq_sec = c["sector_direct"], c["sector_through"], c["qqq_sectors"]
    for k in sorted(set(direct) | set(thru), key=lambda x: -(thru.get(x, 0) or direct.get(x, 0))):
        A(f"| {k} | {pct(direct.get(k)) if k in direct else '—'} "
          f"| {pct(thru.get(k)) if k in thru else '—'} "
          f"| {pct(qqq_sec.get(k)) if k in qqq_sec else '—'} |")
    A("")
    A("> 「穿透 ETF 后」把你持有的 QQQ/VOO/SPY 按其自身行业权重拆开。"
      "只有这样才看得出真实的行业敞口 —— 持有 QQQ 本身就是在加仓科技。")
    A("")

    # ---------- 6 相关性与波动 ----------
    A("## 6 · 相关性与波动")
    A("")
    r = c["riskstats"]
    A(f"- 窗口：最近 {r['window_days']} 个交易日（{r['start']} → {r['end']}）")
    A(f"- 组合年化波动率 σ_p **{pct(r['port_vol'])}**"
      + (f"，SPY 同期 {pct(r['bench_vol'].get('SPY'))}，QQQ {pct(r['bench_vol'].get('QQQ'))}"
         if r["bench_vol"] else ""))
    A(f"- 组合 β（对 SPY）**{num(r['beta'])}**")
    A(f"- 加权平均两两相关性 **{num(r['avg_corr_w'])}**（简单平均 {num(r['avg_corr'])}）")
    A(f"- 分散化比率 DR = Σwᵢσᵢ / σ_p = **{num(r['div_ratio'])}**"
      "（=1 表示完全没有分散效果，越大越分散）")
    A("")
    if r["corr_rows"]:
        A("**两两相关性矩阵**")
        A("")
        tks = r["corr_tickers"]
        A("| | " + " | ".join(tks) + " |")
        A("|---|" + "---:|" * len(tks))
        for row_t, row in zip(tks, r["corr_rows"]):
            A(f"| **{row_t}** | " + " | ".join("—" if v is None else f"{v:.2f}" for v in row) + " |")
        A("")
    if r["high_pairs"]:
        A("相关性 ≥ 0.7 的组合（买其中一只接近于加仓另一只）：")
        for x, y, v in r["high_pairs"]:
            A(f"- {x} ↔ {y}：{v:.2f}")
        A("")
    if r.get("dropped"):
        A(f"⚠️ **以下持仓在该窗口内历史不足，未纳入上述相关性/波动率/β 计算**："
          + "，".join(f"{d[0]}（{d[1]}/{d[2]} 天）" for d in r["dropped"]))
        A("")
        A(f"上述组合层指标覆盖了 **{pct(r.get('covered_weight'))}** 的权重，"
          "剩余部分按已纳入部分的表现等比例推算。新上市的票天然缺历史，这不是数据错误，"
          "但它意味着组合里最不确定的那部分恰好是最看不清的那部分。")
        A("")
    A("> ⚠️ 相关性是时变的。同一对股票在不同市场环境下可以从 0.1 变到 0.7，"
      "尤其在下跌行情里普遍上升 —— 也就是最需要分散的时候分散效果最差。不要把这个窗口的数字当常量。")
    A("")

    # ---------- 7 重叠度 ----------
    A("## 7 · 与指数的重叠")
    A("")
    for etf, ov in c["overlaps"].items():
        A(f"**vs {etf}**（前十大成分，覆盖该 ETF {pct(ov['etf_coverage'])} 的权重）")
        A("")
        if not ov["common"]:
            A(f"- 你的直接持仓与 {etf} 前十大没有交集")
        else:
            A(f"- 重合 {len(ov['common'])} 只：{', '.join(ov['common'])}")
            A(f"- 你的组合有 **{pct(ov['port_share'])}** 落在这几只上；{etf} 自身是 {pct(ov['etf_share'])}")
            A(f"- 标准重叠度 Σmin(w组合, w{etf}) = **{pct(ov['min_overlap'])}**")
        A("")
    lt, unpen = c["look_through"], c["unpenetrated"]
    A("**穿透后单票敞口**（直接持有 + 通过 ETF 间接持有，前 12）")
    A("")
    A("| 代码 | 穿透后权重 | 其中直接持有 |")
    A("|---|---:|---:|")
    for tk, v in list(lt.items())[:12]:
        A(f"| {tk} | {pct(v)} | {pct(w.get(tk)) if tk in w else '—'} |")
    A("")
    A(f"- ETF 中无法穿透的部分（前十大以外）：{pct(unpen)}")
    A("")

    # ---------- 8 压力测试 ----------
    A("## 8 · 历史压力测试")
    A("")
    A("把**当前权重**放回历史区间，看这个组合当时会经历什么。这是假想推演，不是预测。")
    A("")
    A("| 情景 | 区间 | 组合区间收益 | 组合最大回撤 | SPY | QQQ | 真实数据 | 行业ETF代理 |")
    A("|---|---|---:|---:|---:|---:|---:|---:|")
    for sc in c["stress"]:
        if not sc["ok"]:
            A(f"| {sc['name']} | {sc['start']}~{sc['end']} | 数据不足 | — | — | — | 0% | — |")
            continue
        bm = sc["benchmarks"]
        A(f"| {sc['name']} | {sc['start']}~{sc['end']} | **{_sign(sc['portfolio_return'])}** "
          f"| **{_sign(sc.get('portfolio_max_drawdown'))}** "
          f"| {_sign(bm.get('SPY', {}).get('return'))} | {_sign(bm.get('QQQ', {}).get('return'))} "
          f"| {pct(sc.get('real_coverage'))} | {pct(sc.get('proxy_coverage'))} |")
    A("")
    for sc in c["stress"]:
        if not sc["ok"]:
            continue
        worst = "，".join(f"{t} {_sign(v)}" for t, v in sc["worst"][:3])
        A(f"- **{sc['name']}**{('：' + sc['note']) if sc['note'] else ''}")
        if sc.get("trough_date"):
            A(f"  - 区间最低点 {sc['trough_date']}，较起点 {_sign(sc['trough_return'])}")
        A(f"  - 跌幅最大：{worst}")
        if sc.get("real_coverage") is not None and sc["real_coverage"] < 0.6:
            A(f"  - ⚠️ **只有 {pct(sc['real_coverage'])} 的权重有真实历史数据**，"
              f"其余 {pct(sc['proxy_coverage'])} 是行业 ETF 代理 —— "
              "这一行更接近「当年那些行业跌了多少」，而不是「你这个组合跌了多少」")
        if sc["proxied"]:
            A(f"  - 用行业 ETF 代理（当时无数据）：{', '.join(f'{a}→{b}' for a, b in sc['proxied'])}")
        if sc["missing"]:
            A(f"  - 无数据且无代理、已从权重中剔除：{', '.join(sc['missing'])}")
    A("")

    # ---------- 9 局限 ----------
    A("## 9 · 这份报告的局限")
    A("")
    A("- **不预测涨跌。** 所有指标描述的都是已经发生的事。")
    A("- **压力测试的「区间收益」看的是端点，「最大回撤」才是过程中最深处** —— "
      "区间端点是人为选的，回撤更接近当时的真实体验。")
    A("- **压力测试用的是当前权重**，不是你当时的真实持仓；缺数据的票用行业 ETF 代理，已逐条标出。")
    A("- **ETF 穿透只能看到前十大成分**（yfinance 限制），剩余权重无法拆解，覆盖率已标注。")
    A("- **相关性、波动率、β 都是时变的**，换个窗口结论可能相反。")
    A("- **数据源为 yfinance（非官方接口）与 Fidelity 手动导出**，可能有缺失或延迟；"
      "数据源健康状态记录在 `source_health` 表。")
    A("- 本工具为个人研究用途，**所有产出不构成投资建议**。")
    A("")
    A(f"*生成于 {c['generated_at']} · StockWatch P2*")
    return "\n".join(L)
