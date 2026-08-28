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


def _should_write_edgar(res, dry_run):
    """
    修复轮 2 · Critical 1：写库门槛只看「有没有抓到数据」（res.rows > 0），
    不能看 res.ok —— fetch_filings_for_tickers 的 ok 反映的是「整个查询
    过程有没有出错」（任何一只 ticker/表单失败都会让 ok=False），但那不该
    连累其余几十只已经成功抓到的行一行都不写。抽成函数是为了这条判断本身
    能被单测覆盖，而不是靠跑一次真正的 run_ingest.py 才能验证。
    """
    return res.rows > 0 and not dry_run

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", help="Fidelity Positions CSV 路径")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-edgar", action="store_true")
    a = ap.parse_args()

    st = Store(CFG.db_path)
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
    st.log_health(res.source, res.ok, res.latency_ms, res.detail)

    mres = P.fetch_meta(sorted(universe))
    print(f"  {'✅' if mres.ok else '❌'} 元数据 {mres.rows} 只 ({mres.latency_ms}ms) {mres.detail}")
    if mres.ok and not a.dry_run:
        st.upsert_many("meta", ["ticker","sector","industry","name","market_cap","updated_at"],
                       mres.data)
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
    st.log_health(rres.source, rres.ok, rres.latency_ms, rres.detail)

    # ---------- 4. EDGAR ----------
    hr("4 / 4  SEC EDGAR")
    if a.skip_edgar:
        print("  跳过")
    else:
        eres = E.fetch_filings(CFG.get("identity.sec_email"), forms=("8-K","4"))
        print(f"  {'✅' if eres.ok else '❌'} 全市场 {eres.rows} 条 ({eres.latency_ms}ms) {eres.detail}")
        # 修复轮 2 · Critical 2：全市场扫描拿不到 ticker/items（只有 cik），
        # 必须用 insert_ignore_many（INSERT OR IGNORE），不能覆盖按持仓查询
        # 已经写好的完整行——否则同一条申报会被这条路径连续几天重新抹空。
        if _should_write_edgar(eres, a.dry_run):
            st.insert_ignore_many("edgar_filings",
                                  ["accession","filed_at","ticker","cik","form","items",
                                   "url","raw_json","seen_at"], eres.data)
        st.log_health(eres.source, eres.ok, eres.latency_ms, eres.detail)

        # 修复轮 1：全市场索引不带 ticker/items（已实测确认），causes.py 找原因
        # 要靠这两个字段匹配 —— 所以对持仓再按 ticker 逐个查一遍，补上这两个字段。
        # 只对真实持仓查，不对基准/行业 ETF 查。
        tres = E.fetch_filings_for_tickers(CFG.get("identity.sec_email"),
                                           sorted(positions_tickers), forms=("8-K","4"))
        print(f"  {'✅' if tres.ok else '❌'} 按持仓 {tres.rows} 条 ({tres.latency_ms}ms) {tres.detail}")
        # 修复轮 2 · Critical 1：写库门槛不能用 tres.ok —— ok 反映的是
        # "查询过程是否顺利"（len(errs)==0），25+ 只持仓 × 2 种 form 里
        # 任何一只限流/报错都会让 ok=False，但其余几十只已经抓到的行不该被
        # 因此一行都不写。写库门槛只看有没有抓到数据，ok 只用来喂健康记录。
        if _should_write_edgar(tres, a.dry_run):
            st.upsert_many("edgar_filings",
                           ["accession","filed_at","ticker","cik","form","items",
                            "url","raw_json","seen_at"], tres.data)
        st.log_health(tres.source, tres.ok, tres.latency_ms, tres.detail)

    hr("数据库状态")
    for k, v in st.stats().items():
        print(f"  {k:16s} {v:>8d} 行")
    st.close()
    print("\n完成。" + ("（dry-run，未写库）" if a.dry_run else ""))

if __name__ == "__main__":
    main()
