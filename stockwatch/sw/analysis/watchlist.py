"""
观察池增量事件 —— 只报变化，不算评分（完整评分卡是 P4）。

三个信号源的用法按 HANDOFF 锁死，不许改：

**Reddit**：绝对排名前 10 的**直接排除** —— 榜单前十的时候你是接盘方。
有价值的只有变化率：从 50 名开外冲进前 20。单独用价值接近零，
必须与基本面交叉验证，所以这里只把它当「值得看一眼」的线索，不打分。

**Form 4**：滞后仅 1–2 个交易日，是最快的信号。但研究显示申报后 5 日
超额收益 +1.0%，**63 个交易日后中位数转为 −3.6%** ——
它是发现线索的触发器，不是长期持有的理由。这句话要出现在报告里。

**13F**：滞后 45 天，只认「≥2 家基金同季新建仓」的 cluster。
本期只把 13F-HR 加进抓取范围让数据攒起来，cluster 检测属于 P4。
"""

EXCLUDE_TOP_N = 10      # 绝对排名进前 10 的直接排除
JUMP_INTO = 20          # 冲进前 20 才算跃升
JUMP_FROM = 50          # 且此前在 50 名开外

FORM4_NOTE = ("内部人申报后 5 日超额收益中位数约 +1.0%，"
              "但 63 个交易日后转为约 −3.6% —— 这是线索触发器，不是持有理由")


def reddit_jumps(store, d, source="all-stocks"):
    """讨论热度跃升的票。返回 [{"ticker","rank","prev","delta"}]。"""
    rows = store.q(
        "SELECT ticker, rank, rank_24h_ago FROM reddit_rank "
        "WHERE d=? AND source=? AND rank IS NOT NULL "
        "AND COALESCE(TRIM(ticker), '') <> ''", (d, source))
    out = []
    for r in rows:
        rank, prev = r["rank"], r["rank_24h_ago"]
        if prev is None:
            continue                       # 没有前值就不猜
        if rank <= EXCLUDE_TOP_N:
            continue                       # 已在前 10 → 你是接盘方
        if rank <= JUMP_INTO and prev > JUMP_FROM:
            out.append({"ticker": r["ticker"], "rank": rank,
                        "prev": prev, "delta": prev - rank})
    return sorted(out, key=lambda x: -x["delta"])


def insider_filing_clusters(store, d, days=3, min_filings=2):
    """
    最近几天里有多份 Form 4 申报的票。

    这是明确的 filing-count heuristic：当前 edgar_filings 只有申报元数据，
    没有 owner/filer identity，也不解析买卖方向和金额。因此多份 accession
    只能说明多份申报集中，不能推断是多个申报人或买入。
    解析明细属于 P4，届时要区分买入/卖出和 10b5-1 预设计划。
    """
    from datetime import date, timedelta
    lo = (date.fromisoformat(d) - timedelta(days=days)).isoformat()
    rows = store.q(
        "SELECT ticker, COUNT(DISTINCT accession) n FROM edgar_filings "
        "WHERE form='4' AND COALESCE(TRIM(ticker), '') <> '' "
        "AND filed_at>=? AND filed_at<=? "
        "GROUP BY ticker HAVING n>=? ORDER BY n DESC", (lo, d, min_filings))
    return [{"ticker": r["ticker"], "filings": r["n"]} for r in rows]


def insider_buy_clusters(store, d, days=3, min_filers=2):
    """兼容 brief 遗留的误命名接口。

    `min_filers` 是历史参数名，当前语义直接映射到 canonical 函数的
    `min_filings`。这里没有申报人 identity，不能把它解释成申报人数；生产
    汇总固定调用 `insider_filing_clusters()`。
    """
    return insider_filing_clusters(store, d, days=days, min_filings=min_filers)


def collect(store, d, held_tickers=None):
    """汇总成日报里那几行人话。已持仓的要标出来 —— 含义完全不同。"""
    held = held_tickers or set()
    out = []

    for x in reddit_jumps(store, d)[:3]:
        tag = "（已持仓）" if x["ticker"] in held else ""
        out.append(f"{x['ticker']}{tag} 社区讨论量从第 {x['prev']} 名升至第 {x['rank']} 名")

    cl = insider_filing_clusters(store, d)[:3]
    for x in cl:
        tag = "（已持仓）" if x["ticker"] in held else ""
        out.append(f"{x['ticker']}{tag} 近 3 日有 {x['filings']} 份 Form 4 内部人申报")
    if cl:
        out.append(FORM4_NOTE)

    return out
