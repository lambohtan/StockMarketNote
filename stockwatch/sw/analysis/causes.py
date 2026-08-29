"""
异动原因收集。

每个外部/本地信源都返回可观测的健康与覆盖状态。成功行仍可用于报告；
部分失败不会被伪装成「没有原因」，渲染层会标注不可用来源。
"""
from datetime import date, timedelta

from ..sources import news as N

L1_ITEMS = {
    "4.02": "前期财报不可信（会计问题，最严重的信号之一）",
    "4.01": "更换审计师",
    "1.03": "破产 / 接管",
    "2.06": "重大资产减值",
    "5.02": "董事或高管变动（CEO/CFO 离职尤其重要）",
    "1.05": "重大网络安全事件",
}
ITEM_MEANING = dict(L1_ITEMS, **{
    "2.02": "披露季度业绩", "7.01": "公司自愿披露（Regulation FD）",
    "8.01": "其他事件", "5.07": "股东投票结果", "1.01": "签订重大协议",
    "2.01": "完成收购或资产处置",
})
FILING_WINDOW_DAYS = 4
MIN_PEERS_FOR_SECTOR_CALL = 3


class CauseList(list):
    """保持旧 list 接口，同时携带 source health/coverage 元数据。"""

    def __init__(self, values=(), health=None, coverage=None):
        super().__init__(values)
        self.health = list(health or [])
        self.coverage = dict(coverage or {})


def _window(d, back=FILING_WINDOW_DAYS):
    dd = date.fromisoformat(d)
    return (dd - timedelta(days=back)).isoformat(), (dd + timedelta(days=1)).isoformat()


def _query_8k(store, ticker, d):
    lo, hi = _window(d)
    health = {"source": "edgar.local", "ok": True, "detail": "", "coverage": ticker}
    try:
        rows = store.q(
            "SELECT * FROM edgar_filings WHERE ticker=? AND form='8-K' "
            "AND filed_at>=? AND filed_at<=? ORDER BY filed_at DESC",
            (ticker, lo, hi))
    except Exception as exc:
        health.update(ok=False, detail=f"{type(exc).__name__}")
        return [], health
    out = []
    for r in rows:
        items = [i.strip() for i in (r.get("items") or "").split(",") if i.strip()]
        is_l1 = any(i in L1_ITEMS for i in items)
        desc = "；".join(ITEM_MEANING.get(i, f"Item {i}") for i in items) or "未标注 item"
        out.append({"source": "8-K", "summary": f"{r['filed_at']} 提交 8-K：{desc}",
                    "url": r.get("url") or "", "item": items[0] if items else None,
                    "is_l1": is_l1})
    health["detail"] = f"覆盖 {len(rows)} 条" if rows else "窗口内无本地申报"
    return out, health


def find_l1_causes(store, ticker, d):
    """只读取本地 8-K L1 项，供正常价格持仓的独立 L1 路径使用。"""
    causes, health = _query_8k(store, ticker, d)
    return CauseList([c for c in causes if c.get("is_l1")], [health], {ticker: True})


def _earnings_cause(ticker, lo, hi):
    health = {"source": "yfinance.earnings", "ok": True, "detail": "", "coverage": ticker}
    try:
        from ..sources.prices import fetch_earnings
        result = fetch_earnings(ticker)
        ed = (result.data or [None])[0]
        health.update(ok=result.ok, detail=result.detail or "")
    except Exception as exc:
        health.update(ok=False, detail=f"{type(exc).__name__}")
        return [], health
    if ed and lo <= str(ed)[:10] <= hi:
        return [{"source": "财报", "summary": f"财报日 {str(ed)[:10]}",
                 "url": "", "item": None, "is_l1": False}], health
    health["detail"] = "窗口内无财报日期"
    return [], health


def find_causes_result(store, ticker, d, peers=None, max_news=3):
    """返回 causes + 健康元数据；调用方可按需持久化 health。"""
    out, edgar_health = _query_8k(store, ticker, d)
    lo, hi = _window(d)
    health = [edgar_health]

    earnings, earnings_health = _earnings_cause(ticker, lo, hi)
    out.extend(earnings)
    health.append(earnings_health)

    # 新闻必须走 adapter，保留「有部分行但 ok=False」这一状态。
    if max_news > 0:
        try:
            news_res = N.fetch_news(ticker, max_news)
            health.append({"source": news_res.source, "ok": news_res.ok,
                           "detail": news_res.detail or f"覆盖 {news_res.rows} 条",
                           "coverage": ticker})
            for item in (news_res.data or [])[:max_news]:
                out.append({"source": "新闻", "summary": item.get("summary") or "",
                            "url": item.get("url") or "", "item": None,
                            "is_l1": False})
        except Exception as exc:
            health.append({"source": "yfinance.news", "ok": False,
                           "detail": type(exc).__name__, "coverage": ticker})

    # 分析师调整是计划中的阶段；当前依赖没有稳定、可复现的适配器，必须
    # 明确报告 unavailable，不能把它静默当成 no cause。
    health.append({"source": "analyst", "ok": False,
                   "detail": "本期未提供确定性分析师调整适配器（P4 scope）",
                   "coverage": ticker})

    if peers:
        pr = peers
        if pr.get("n_peers", 0) > 0:
            if pr["n_peers"] < MIN_PEERS_FOR_SECTOR_CALL:
                out.append({"source": "同行读数",
                            "summary": f"同行业仅有 {pr['n_peers']} 只可比标的，样本不足以判断是否为行业性事件",
                            "url": "", "item": None, "is_l1": False})
            elif pr["looks_sector_wide"]:
                out.append({"source": "同行读数",
                            "summary": f"同行业另有 {pr['n_moving']}/{pr['n_peers']} 只同样出现异动，更像行业性事件而非公司自身的事",
                            "url": "", "item": None, "is_l1": False})
            else:
                out.append({"source": "同行读数",
                            "summary": f"同行业另外 {pr['n_peers']} 只均无异动，这次波动集中在该公司自身",
                            "url": "", "item": None, "is_l1": False})

    coverage = {h["source"]: h.get("ok", False) for h in health}
    return CauseList(out, health=health, coverage=coverage)


def find_causes(store, ticker, d, peers=None, max_news=3):
    """兼容旧接口：返回带 health 属性的 list 子类。"""
    return find_causes_result(store, ticker, d, peers=peers, max_news=max_news)


def peer_readthrough(attributions, ticker, sector, z_thresh=2.0):
    if not sector:
        return None
    peers = [a for a in attributions
             if a.get("sector") == sector and a.get("ticker") != ticker]
    if not peers:
        return {"n_peers": 0, "n_moving": 0, "looks_sector_wide": False}
    moving = [a for a in peers if abs(a.get("z") or 0) >= z_thresh]
    return {"n_peers": len(peers), "n_moving": len(moving),
            "looks_sector_wide": (len(peers) >= MIN_PEERS_FOR_SECTOR_CALL
                                   and len(moving) * 2 >= len(peers)
                                   and len(moving) > 0)}
