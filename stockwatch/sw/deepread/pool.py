"""股票池构建：持仓异动 + ApeWisdom 前 20 + Form 4 申报集中。

**与 analysis/watchlist.py 的口径差异是有意的**：那个模块服务日报，
按 HANDOFF 的老规矩排除绝对排名前 10（榜单前十时用户是接盘方）。
这里服务深读，用户 2026-08-29 明确要求收前 20 含前 10 ——
接盘风险改为在报告里直接列出「当前排名 + 排名变化」让它可见，
而不是靠一条隐藏规则替用户做决定。

**13F 不在本期范围**：滞后 45 天，进不了日更。
"""
from datetime import date, timedelta

from ..analysis.causes import L1_ITEMS, ITEM_MEANING
from ..analysis.watchlist import insider_filing_clusters

DEFAULT_TOP_N = 20
DEFAULT_NEW_SLOTS = 8

# 持仓事件扫描的窗口与门槛。8-K 窗口沿用 causes.FILING_WINDOW_DAYS 的口径（4 天）。
EVENT_WINDOW_DAYS = 4
INSIDER_WINDOW_DAYS = 30
INSIDER_MIN_FILINGS = 3
EARNINGS_ITEM = "2.02"          # 8-K「披露季度业绩」= 财报刚发布


def _window(d, back):
    return (date.fromisoformat(d) - timedelta(days=int(back))).isoformat(), d


def _recent_8k_items(store, tickers, d, back=EVENT_WINDOW_DAYS):
    """窗口内每只持仓票的 8-K item 集合。一次查询，不按票循环。"""
    if not tickers:
        return {}
    lo, hi = _window(d, back)
    marks = ",".join("?" * len(tickers))
    rows = store.q(
        f"SELECT ticker, items FROM edgar_filings WHERE form='8-K' "
        f"AND ticker IN ({marks}) AND filed_at>=? AND filed_at<=?",
        (*tickers, lo, hi))
    out = {}
    for r in rows:
        items = [i.strip() for i in (r["items"] or "").split(",") if i.strip()]
        out.setdefault(r["ticker"], set()).update(items)
    return out


def _insider_counts(store, tickers, d, back=INSIDER_WINDOW_DAYS):
    """窗口内每只持仓票的 Form 4 申报份数。只数份数，不推断买卖方向。"""
    if not tickers:
        return {}
    lo, hi = _window(d, back)
    marks = ",".join("?" * len(tickers))
    rows = store.q(
        f"SELECT ticker, COUNT(DISTINCT accession) n FROM edgar_filings "
        f"WHERE form='4' AND ticker IN ({marks}) AND filed_at>=? AND filed_at<=? "
        f"GROUP BY ticker", (*tickers, lo, hi))
    return {r["ticker"]: r["n"] for r in rows}


def holding_events(store, d, held_tickers, attributions=None):
    """每天扫全部持仓，只把「出事了」的挑出来。

    用户 2026-08-29 的原话：「每天都看一下全部股票哪只有重大新闻就行……
    如果没什么事就别提醒了」。所以这里的返回值天然是稀疏的 —— 平静的持仓
    一条都不返回，**沉默是设计的一部分，不是漏掉了**。

    四条触发任一命中即可，全部用已抓的本地数据判定、不额外联网：
      1. 价格残差 >2σ（归因引擎给的 level）
      2. 近 4 天有 L1 级 8-K（会计问题 / 换审计师 / 破产 / 重大减值 /
         高管变动 / 网络安全事件）—— 价格没动但 CEO 走了，旧实现会漏掉
      3. 近 4 天有 item 2.02 的 8-K（财报刚发布）
      4. 近 30 天 Form 4 申报 ≥ 3 份

    ⚠️ 第 3 条为什么不用「财报日临近」：财报日历从未入库（`fundamentals`
    是空表，`run_ingest` 也不抓它），`prices.next_earnings()` 每次都要联网。
    为一个每天跑 26 次的扫描引入 26 次网络往返不划算，也会破坏干跑的离线保证。
    代价是只能捕获「刚发布」，捕获不到「即将发布」。
    """
    held = [t for t in (held_tickers or []) if t]
    if not held:
        return []
    anomaly = {a.get("ticker"): a for a in (attributions or [])
               if a.get("level") not in (None, "normal")}
    items_by_ticker = _recent_8k_items(store, held, d)
    insider = _insider_counts(store, held, d)

    out = []
    for ticker in held:
        reasons, strength = [], 0.0
        a = anomaly.get(ticker)
        if a:
            level_cn = {"extreme": "极端异动", "anomaly": "异动"}.get(
                a.get("level"), "异动")
            z = abs(float(a.get("z") or 0))
            reasons.append(f"持仓{level_cn}（残差 {z:.1f}σ）")
            strength += 100 + z
        items = items_by_ticker.get(ticker, set())
        l1 = sorted(i for i in items if i in L1_ITEMS)
        if l1:
            reasons.append("重大 8-K：" + "；".join(ITEM_MEANING[i] for i in l1))
            strength += 90
        if EARNINGS_ITEM in items:
            reasons.append("财报刚发布（8-K 2.02）")
            strength += 60
        n4 = insider.get(ticker, 0)
        if n4 >= INSIDER_MIN_FILINGS:
            reasons.append(f"近 30 天 {n4} 份 Form 4 申报")
            strength += 40
        if reasons:
            out.append({"ticker": ticker, "source": "holding_event",
                        "reason": " · ".join(reasons), "strength": strength})
    return sorted(out, key=lambda x: (-x["strength"], x["ticker"]))


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
          new_slots=DEFAULT_NEW_SLOTS, held_tickers=None):
    """今日深读名单：持仓事件全进（不占名额）+ 新票按信号强度取 new_slots 只。"""
    picked, order = {}, []

    def add(ticker, source, reason, strength):
        if not ticker or ticker in picked:
            return          # 先到先得：持仓事件最先加，天然优先
        picked[ticker] = {"ticker": ticker, "source": source,
                          "reason": reason, "strength": strength}
        order.append(ticker)

    # 持仓走四条触发的事件扫描；没有传持仓列表时退回「只看价格异动」的旧口径，
    # 保证直接库调用（不经 run_pool）仍然可用。
    if held_tickers:
        events = holding_events(store, d, held_tickers, attributions)
    else:
        events = holding_events(
            store, d, [a.get("ticker") for a in (attributions or [])],
            attributions)
    for e in events:
        add(e["ticker"], e["source"], e["reason"], e["strength"])

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
        if len([t for t in order if picked[t]["source"] not in ("holding_anomaly", "holding_event")]) >= slots:
            break
        add(ticker, source, reason, strength)

    return [picked[t] for t in order]
