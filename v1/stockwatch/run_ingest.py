#!/usr/bin/env python3
"""
P1 数据底座入口 —— 每日抓取并入库。

用法：
  python3 run_ingest.py --positions ~/Downloads/Portfolio_Positions.csv   # 首次：带上持仓
  python3 run_ingest.py                                                   # 之后：只更新行情等
  python3 run_ingest.py --dry-run --positions xxx.csv                     # 只解析不写库
"""
import argparse, sys, json
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sw.config import CFG
from sw.store import Store
from sw.fidelity import parse_file
from sw.sources import prices as P, reddit as R, edgar_src as E

def hr(t):
    print(f"\n{'='*60}\n{t}\n{'='*60}")


EDGAR_COLS = ["accession", "filed_at", "ticker", "cik", "form", "items",
             "url", "raw_json", "seen_at"]


def ingest_edgar(st, email, positions_tickers, dry_run=False,
                 fetch_all=None, fetch_by_ticker=None):
    """
    EDGAR 抓取与入库。两条路径：

    - 全市场扫描：拿不到 ticker/items（索引行结构如此，修复轮 1 实测确认），
      用 insert_ignore_many 写入，绝不覆盖按持仓查询已经填好的完整行
      （修复轮 2 · Critical 2）。
    - 按持仓逐个查：能拿到 items，ticker 是查询时已知的，用 upsert_many
      覆盖（内容本来就该以这条路径为准）。

    写库门槛一律是 rows > 0，不是 ok —— ok 反映的是「查询过程是否顺利」
    （len(errs)==0），而 25+ 只持仓里任一只限流/报错都会让 ok=False，
    那会把其余几十只已经成功抓到的数据全部丢弃，而且是**静默的**、不报错、
    不打印异常，只是 edgar_filings 悄悄没有新数据（修复轮 2 · Critical 1）。
    ok 只喂 log_health，不参与写库判断。

    抽成这个函数、把 fetch_all / fetch_by_ticker 做成可注入依赖，是因为
    修复轮 2 里把判断单独抽成 _should_write_edgar 之后，仍然抓不住
    「main() 里的调用点绕开辅助函数、又写回 .ok 门槛」这类回归——纯函数
    本身没坏，坏的是调用点。这里直接测这个函数的**行为**（真的往库里写
    了什么），而不是只测判断条件本身。

    返回 (eres, tres) 方便调用方在需要时检查结果（比如测试里断言
    log_health 收到的 ok 是不是真实值）。
    """
    fetch_all = fetch_all or E.fetch_filings
    fetch_by_ticker = fetch_by_ticker or E.fetch_filings_for_tickers

    # 13F-HR 本期不做 cluster 检测（属 P4），但先把数据攒起来 ——
    # 和 Reddit 同理，早一天开始攒就早一天能回看
    eres = fetch_all(email, forms=("8-K", "4", "13F-HR"))
    print(f"  {'✅' if eres.ok else '❌'} 全市场 {eres.rows} 条 ({eres.latency_ms}ms) {eres.detail}")
    if eres.rows > 0 and not dry_run:
        st.insert_ignore_many("edgar_filings", EDGAR_COLS, eres.data)
    if not dry_run:
        st.log_health(eres.source, eres.ok, eres.latency_ms, eres.detail)

    tres = fetch_by_ticker(email, sorted(positions_tickers), forms=("8-K", "4"))
    print(f"  {'✅' if tres.ok else '❌'} 按持仓 {tres.rows} 条 ({tres.latency_ms}ms) {tres.detail}")
    if tres.rows > 0 and not dry_run:
        st.upsert_many("edgar_filings", EDGAR_COLS, tres.data)
    if not dry_run:
        st.log_health(tres.source, tres.ok, tres.latency_ms, tres.detail)

    return eres, tres


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", help="Fidelity Positions CSV 路径")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-edgar", action="store_true")
    a = ap.parse_args()

    st = Store.open_read_only(CFG.db_path) if a.dry_run else Store(CFG.db_path)
    today = date.today().isoformat()
    print(f"StockWatch ingest · {today} · db={CFG.db_path}")

    # ---------- 1. 持仓 ----------
    universe = set()
    hr("1 / 4  持仓")
    if a.positions:
        rows, rep = parse_file(a.positions, today)
        print(f"  解析 {rep['parsed']} 行（股票 {rep['equity']} · 现金 {rep['cash']}）"
              f"，跳过 {rep['skipped_total']}")
        print(f"  识别到的字段: {sorted(set(rep['mapped'].values()))}")
        for r in rows:
            flag = "" if r["asset_type"] == "equity" else "  [现金]"
            print(f"    {r['ticker']:8s} qty={r['quantity']!s:>10s} "
                  f"mv={r['market_value']!s:>10s} cost={r['cost_basis_total']!s:>10s}{flag}")
        if rep["skipped"]:
            print(f"  跳过明细: {rep['skipped']}")
        if not a.dry_run:
            cols = ["snapshot_date","ticker","description","quantity","last_price",
                    "market_value","cost_basis_total","avg_cost","total_gain",
                    "asset_type","account","loaded_at"]
            from datetime import datetime
            now = datetime.now().isoformat(timespec="seconds")
            st.upsert_many("positions", cols,
                           [tuple(r.get(c) for c in cols[:-1]) + (now,) for r in rows])
            print(f"  ✅ 已写入 {len(rows)} 行持仓快照")
        universe |= {r["ticker"] for r in rows if r["asset_type"] == "equity"}
    else:
        prev = st.latest_positions()
        universe |= {r["ticker"] for r in prev if r["asset_type"] == "equity"}
        print(f"  未提供 CSV，沿用最近一次快照：{len(universe)} 只")

    # 只含真实持仓的 ticker（不含基准/行业 ETF）——EDGAR 按 ticker 查申报时只查这些，
    # 对 SPY/XLK 这类 ETF 查 8-K 没有意义
    positions_tickers = set(universe)

    # 基准也要入库，归因和相关性要用
    bm = CFG.get("benchmarks", {}) or {}
    bench = {v for k, v in bm.items() if isinstance(v, str)}
    bench |= set((bm.get("sectors") or {}).values())
    universe |= bench
    print(f"  行情抓取范围（含基准/行业 ETF）：{len(universe)} 只")

    # ---------- 2. 行情 ----------
    hr("2 / 4  行情与元数据")
    res = P.fetch_prices(sorted(universe), period=CFG.get("history.price_period", "2y"))
    print(f"  {'✅' if res.ok else '❌'} 价格 {res.rows} 行 ({res.latency_ms}ms) {res.detail}")
    if res.ok:
        from sw.market_time import drop_incomplete_bars
        res.data, dropped = drop_incomplete_bars(res.data)
        if dropped:
            print(f"  ⚠️ 剔除未收盘的当日 bar：{dropped}（避免污染回归）")
    if res.ok and not a.dry_run:
        st.upsert_many("prices", ["d","ticker","close","volume"], res.data)
    if not a.dry_run:
        st.log_health(res.source, res.ok, res.latency_ms, res.detail)

    mres = P.fetch_meta(sorted(universe))
    print(f"  {'✅' if mres.ok else '❌'} 元数据 {mres.rows} 只 ({mres.latency_ms}ms) {mres.detail}")
    if mres.ok and not a.dry_run:
        st.upsert_many("meta", ["ticker","sector","industry","name","market_cap","updated_at"],
                       mres.data)
    if not a.dry_run:
        st.log_health(mres.source, mres.ok, mres.latency_ms, mres.detail)

    # ---------- 3. Reddit ----------
    hr("3 / 4  Reddit 热度")
    rres = R.fetch_reddit(tuple(CFG.get("reddit.filters", ["all-stocks"])),
                          CFG.get("reddit.top_n", 100), today)
    print(f"  {'✅' if rres.ok else '❌'} {rres.rows} 行 ({rres.latency_ms}ms) {rres.detail}")
    if rres.ok and not a.dry_run:
        st.upsert_many("reddit_rank",
                       ["d","source","ticker","rank","mentions","upvotes",
                        "rank_24h_ago","mentions_24h_ago"], rres.data)
    if not a.dry_run:
        st.log_health(rres.source, rres.ok, rres.latency_ms, rres.detail)

    # ---------- 4. EDGAR ----------
    hr("4 / 4  SEC EDGAR")
    if a.skip_edgar:
        print("  跳过")
    else:
        # 修复轮 3：全市场扫描 + 按持仓查询这两条路径的写库逻辑抽成
        # ingest_edgar（见函数注释），只对真实持仓查，不对基准/行业 ETF 查。
        ingest_edgar(st, CFG.get("identity.sec_email"), positions_tickers,
                    dry_run=a.dry_run)

    hr("数据库状态")
    for k, v in st.stats().items():
        print(f"  {k:16s} {v:>8d} 行")
    st.close()
    print("\n完成。" + ("（dry-run，未写库）" if a.dry_run else ""))

if __name__ == "__main__":
    main()
