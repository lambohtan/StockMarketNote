"""股票池构建：持仓异动 + ApeWisdom 前 20 + Form 4 申报集中。

**与 analysis/watchlist.py 的口径差异是有意的**：那个模块服务日报，
按 HANDOFF 的老规矩排除绝对排名前 10（榜单前十时用户是接盘方）。
这里服务深读，用户 2026-08-29 明确要求收前 20 含前 10 ——
接盘风险改为在报告里直接列出「当前排名 + 排名变化」让它可见，
而不是靠一条隐藏规则替用户做决定。

**13F 不在本期范围**：滞后 45 天，进不了日更。
"""
from ..analysis.watchlist import insider_filing_clusters

DEFAULT_TOP_N = 20
DEFAULT_NEW_SLOTS = 3


def reddit_top(store, d, n=DEFAULT_TOP_N, source="all-stocks"):
    """当日热度前 n 名，含前 10。空代码过滤掉。"""
    rows = store.q(
        "SELECT ticker, rank, rank_24h_ago FROM reddit_rank "
        "WHERE d=? AND source=? AND rank IS NOT NULL "
        "AND COALESCE(TRIM(ticker), '') <> '' "
        "AND rank<=? ORDER BY rank", (d, source, int(n)))
    out = []
    for r in rows:
        prev = r["rank_24h_ago"]
        delta = (prev - r["rank"]) if prev is not None else None
        out.append({"ticker": r["ticker"].strip(), "rank": r["rank"],
                    "prev": prev, "delta": delta})
    return out


def build(store, cfg, d, attributions=None, top_n=DEFAULT_TOP_N,
          new_slots=DEFAULT_NEW_SLOTS):
    """今日深读名单：持仓异动全进（不占名额）+ 新票按信号强度取 new_slots 只。"""
    picked, order = {}, []

    def add(ticker, source, reason, strength):
        if not ticker or ticker in picked:
            return          # 先到先得：持仓异动最先加，天然优先
        picked[ticker] = {"ticker": ticker, "source": source,
                          "reason": reason, "strength": strength}
        order.append(ticker)

    for a in (attributions or []):
        if a.get("level") in (None, "normal"):
            continue
        # level 来自 attribution.classify()：anomaly / extreme
        level_cn = {"extreme": "极端异动", "anomaly": "异动"}.get(a.get("level"), "异动")
        add(a.get("ticker"), "holding_anomaly",
            f"持仓{level_cn}（残差 {abs(float(a.get('z') or 0)):.1f}σ）",
            100 + abs(float(a.get("z") or 0)))

    candidates = []
    for x in reddit_top(store, d, n=top_n):
        delta = x["delta"]
        reason = (f"社区热度第 {x['rank']} 名"
                  + (f"（较 24h 前 {delta:+d}）" if delta is not None else "（无前值）"))
        # 排名越靠前、跃升越大越优先；没有前值的只按排名。
        candidates.append((max(0, top_n - x["rank"]) + max(0, delta or 0),
                           x["ticker"], "apewisdom", reason))
    for x in insider_filing_clusters(store, d, days=3, min_filings=2):
        candidates.append((x["filings"] * 5, x["ticker"], "form4",
                           f"近 3 日 {x['filings']} 份 Form 4 申报"))

    slots = max(0, int(new_slots))
    for strength, ticker, source, reason in sorted(
            candidates, key=lambda c: (-c[0], c[1])):
        if len([t for t in order if picked[t]["source"] != "holding_anomaly"]) >= slots:
            break
        add(ticker, source, reason, strength)

    return [picked[t] for t in order]
