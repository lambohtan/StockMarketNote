"""
与指数 ETF 的重叠度 + 穿透敞口。

要回答的问题：我以为自己买了 9 只不同的票 + 一个指数基金，
但穿透之后，我实际上有多少钱压在 NVDA 一只上？
"""

def overlap(port_w, etf_w):
    """
    port_w: {ticker: 组合权重}，etf_w: {ticker: ETF 内权重}（只有前十大）
    返回：
      common        两边都有的票
      port_share    你的组合中落在这些票上的权重合计
      etf_share     ETF 在这些票上的权重合计
      min_overlap   Σ min(wᵢ_组合, wᵢ_ETF) —— 标准重叠度定义，可直接比较
      etf_coverage  ETF 前十大合计权重（穿透覆盖率，低于 1 说明看不全）
    """
    common = sorted(set(port_w) & set(etf_w), key=lambda t: -port_w[t])
    return {
        "common": common,
        "detail": [(t, port_w[t], etf_w[t]) for t in common],
        "port_share": sum(port_w[t] for t in common),
        "etf_share": sum(etf_w[t] for t in common),
        "min_overlap": sum(min(port_w[t], etf_w[t]) for t in common),
        "etf_coverage": sum(etf_w.values()),
    }


def look_through(port_w, etf_map):
    """
    把组合里持有的 ETF 按其前十大成分展开，算出每只股票的真实敞口。

    port_w:  {ticker: 权重}（含 ETF 本身）
    etf_map: {etf: {"holdings": {...}, "coverage": x}}
    返回 (exposure, unpenetrated)
      exposure     {股票: 穿透后总权重}，直接持有 + 通过 ETF 间接持有
      unpenetrated ETF 中无法穿透的权重合计（前十大以外的部分）
    """
    exposure, unpen = {}, 0.0
    for tk, w in port_w.items():
        info = etf_map.get(tk)
        if not info:
            exposure[tk] = exposure.get(tk, 0.0) + w
            continue
        for sym, sw in info["holdings"].items():
            exposure[sym] = exposure.get(sym, 0.0) + w * sw
        unpen += w * max(0.0, 1.0 - info["coverage"])
    return dict(sorted(exposure.items(), key=lambda kv: -kv[1])), unpen


def sector_look_through(port_w, holdings, etf_map):
    """行业分布的穿透版：ETF 用它自己的 sector_weightings 拆开。"""
    # yfinance 的 sector key 是 snake_case，映射回 yfinance 单股用的 sector 名
    KEY = {
        "technology": "Technology", "healthcare": "Healthcare",
        "financial_services": "Financial Services", "consumer_cyclical": "Consumer Cyclical",
        "consumer_defensive": "Consumer Defensive", "energy": "Energy",
        "industrials": "Industrials", "utilities": "Utilities",
        "realestate": "Real Estate", "basic_materials": "Basic Materials",
        "communication_services": "Communication Services",
    }
    sec_of = {h.ticker: h.sector for h in holdings}
    out, unknown = {}, 0.0
    for tk, w in port_w.items():
        info = etf_map.get(tk)
        if info and info.get("sectors"):
            for k, sw in info["sectors"].items():
                name = KEY.get(k, k)
                out[name] = out.get(name, 0.0) + w * float(sw)
            continue
        s = sec_of.get(tk)
        if s:
            out[s] = out.get(s, 0.0) + w
        else:
            unknown += w
    if unknown > 0:
        out["未知/无法穿透"] = unknown
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
