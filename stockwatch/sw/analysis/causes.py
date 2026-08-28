"""
找异动原因。

顺序是按**可靠性**排的，不是按方便程度：
  1. SEC 8-K       —— 4 个工作日内必须申报，按 item 编号分类，最硬的信源
  2. 财报日历      —— 是不是刚出财报
  3. 新闻          —— 媒体转述，可能失真
  4. 分析师调整    —— 滞后，且常常是跟随价格而非引导价格
  5. 同行读数      —— 区分「行业性事件」和「公司自己的事」
  6. 都找不到      —— 老实写「未找到明确原因」

⚠️ 第 6 条是硬性要求。编一个看似合理的理由比承认不知道有害得多 ——
用户会拿它当决策依据。
"""
import json
from datetime import date, timedelta

# 触发 L1 的 8-K item。来源：02-系统设计.md
L1_ITEMS = {
    "4.02": "前期财报不可信（会计问题，最严重的信号之一）",
    "4.01": "更换审计师",
    "1.03": "破产 / 接管",
    "2.06": "重大资产减值",
    "5.02": "董事或高管变动（CEO/CFO 离职尤其重要）",
    "1.05": "重大网络安全事件",
}

ITEM_MEANING = dict(L1_ITEMS, **{
    "2.02": "披露季度业绩",
    "7.01": "公司自愿披露（Regulation FD）",
    "8.01": "其他事件",
    "5.07": "股东投票结果",
    "1.01": "签订重大协议",
    "2.01": "完成收购或资产处置",
})

FILING_WINDOW_DAYS = 4      # 异动日前后几天内的申报才算相关


def _window(d, back=FILING_WINDOW_DAYS):
    dd = date.fromisoformat(d)
    return (dd - timedelta(days=back)).isoformat(), (dd + timedelta(days=1)).isoformat()


def find_causes(store, ticker, d, peers=None, max_news=3):
    """返回按可靠性降序的原因列表。找不到返回 []。"""
    out = []
    lo, hi = _window(d)

    # ---------- 1. 8-K ----------
    rows = store.q(
        "SELECT * FROM edgar_filings WHERE ticker=? AND form='8-K' "
        "AND filed_at>=? AND filed_at<=? ORDER BY filed_at DESC",
        (ticker, lo, hi))
    for r in rows:
        items = [i.strip() for i in (r["items"] or "").split(",") if i.strip()]
        is_l1 = any(i in L1_ITEMS for i in items)
        desc = "；".join(ITEM_MEANING.get(i, f"Item {i}") for i in items) or "未标注 item"
        out.append({
            "source": "8-K",
            "summary": f"{r['filed_at']} 提交 8-K：{desc}",
            "url": r["url"] or "",
            "item": items[0] if items else None,
            "is_l1": is_l1,
        })

    # ---------- 2. 财报日历 ----------
    try:
        from ..sources.prices import next_earnings
        ed = next_earnings(ticker)
        if ed and str(ed)[:10] >= lo and str(ed)[:10] <= hi:
            out.append({"source": "财报", "summary": f"财报日 {str(ed)[:10]}",
                        "url": "", "item": None, "is_l1": False})
    except Exception:
        pass        # 数据源失败不中断，符合全局约束

    # ---------- 3. 新闻 ----------
    if max_news > 0:
        try:
            import warnings; warnings.filterwarnings("ignore")
            import yfinance as yf
            for n in (yf.Ticker(ticker).news or [])[:max_news]:
                c = n.get("content") or n
                title = c.get("title") or ""
                if not title:
                    continue
                link = ((c.get("canonicalUrl") or {}).get("url")
                        if isinstance(c.get("canonicalUrl"), dict) else c.get("link")) or ""
                out.append({"source": "新闻", "summary": title,
                            "url": link, "item": None, "is_l1": False})
        except Exception:
            pass

    # ---------- 4. 同行读数 ----------
    if peers:
        pr = peers
        if pr and pr.get("n_peers", 0) > 0:
            if pr["looks_sector_wide"]:
                out.append({
                    "source": "同行读数",
                    "summary": f"同行业另有 {pr['n_moving']}/{pr['n_peers']} 只同样出现异动"
                               f"，更像行业性事件而非公司自身的事",
                    "url": "", "item": None, "is_l1": False})
            else:
                out.append({
                    "source": "同行读数",
                    "summary": f"同行业另外 {pr['n_peers']} 只均无异动，"
                               f"这次波动集中在该公司自身",
                    "url": "", "item": None, "is_l1": False})
    return out


def peer_readthrough(attributions, ticker, sector, z_thresh=2.0):
    """
    同行是不是也在动。
    attributions: [{"ticker","sector","z"}, ...]（整个组合的归因结果）
    """
    if not sector:
        return None
    peers = [a for a in attributions
             if a.get("sector") == sector and a.get("ticker") != ticker]
    if not peers:
        return {"n_peers": 0, "n_moving": 0, "looks_sector_wide": False}
    moving = [a for a in peers if abs(a.get("z") or 0) >= z_thresh]
    return {
        "n_peers": len(peers),
        "n_moving": len(moving),
        # 过半同行同时异动 → 更像行业性事件
        "looks_sector_wide": len(moving) * 2 >= len(peers) and len(moving) > 0,
    }
