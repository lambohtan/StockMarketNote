#!/usr/bin/env python3
"""股票池深读入口（launchd 06:45 调这个，排在 run_daily 之后）。

排在 run_daily 之后不是为了读它的结果 —— 归因结果不落库，这里就地重算 ——
而是要等它触发的 ingest 把当日价格、Reddit 排名、EDGAR 申报写进库。

单只票失败只跳过那一只，不影响其余票；深读整体失败也不影响日报推送。

**--dry-run 严格离线只读**（裁定 1）：只用库里已有的数据构建今日池子和
py 六条硬指标，不抓 EDGAR 财报正文、不抓新闻正文、不调 LLM（stage1/stage2
两次真实调用一次都不发生），全程不写任何东西 —— 不写 source_health、
不写 filing_texts、不写 outbox、不写报告文件。这跟 P3 的
``run_daily.py --dry-run`` 修复后的做法是同一个形状：干跑只换 Store 是
只读还不够，被只读 Store 包着的正常路径仍然会尝试联网、花钱调 LLM，
必须在干跑分支里显式换成一条不联网、不调 LLM 的单独路径
（见 ``one_item_dry_run``）。
"""
import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sw.config import CFG
from sw.store import Store
from sw import outbox as OB
from sw.clock import logical_date as local_logical_date
from sw.analysis import portfolio as PF, attribution as AT
from sw.sources import filings_text as FT, news_full as NF
from sw.deepread import pool as PL, facts as FA, criteria as CR
from sw.deepread import deepread as DR, render as RD

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def log(message):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def _attributions(store, cfg, d):
    """就地重算持仓归因，只为拿到「哪几只异动」。纯本地计算，不联网。"""
    try:
        # PF.build 返回 Portfolio 数据类，holdings 是 Holding 对象列表（不是 dict），
        # 且库里没有持仓快照时会抛 ValueError —— 一并由外层 except 兜住。
        portfolio = PF.build(store, cfg)
        sectors = cfg.get("benchmarks.sectors") or {}
        market = cfg.get("benchmarks.market", "SPY")
        out = []
        for h in portfolio.holdings:
            etf = sectors.get(h.sector)
            if not etf:
                continue
            r = AT.attribute(store, h.ticker, etf, market, 60)
            if r:
                out.append(r)
        return out
    except Exception as exc:
        log(f"  ⚠️ 归因重算失败，本次只用热度与 Form 4 入池：{type(exc).__name__}: {exc}")
        return []


def one_item(store, cfg, d, entry):
    """跑完一只票的完整深读。任何一步失败由调用方捕获。"""
    ticker = entry["ticker"]
    email = cfg.get("identity.sec_email")

    fr = FT.fetch(email, ticker, store)
    store.log_health(fr.source, fr.ok, fr.latency_ms, f"{ticker} {fr.detail}")
    sections = dict((fr.data or {}).get("sections") or {})
    if fr.ok and fr.data:
        prev = FT.previous(store, ticker, fr.data.get("filed_at") or d)
        sections["prev_risk_factors"] = (prev or {}).get("risk_factors", "")

    nr = NF.fetch(ticker, max_items=4)
    store.log_health(nr.source, nr.ok, nr.latency_ms, f"{ticker} {nr.detail}")

    try:
        held = [h.ticker for h in PF.build(store, cfg).holdings]
    except Exception:
        held = []          # 没有持仓快照时相关性那条判 None，不影响其余五条
    facts = FA.collect(store, cfg, ticker, d, held_tickers=held)
    py = CR.evaluate(facts)

    material = DR.build_material(ticker, sections, nr.data or [], facts)
    llm = DR.stage1(cfg, ticker, material, store=store)
    item = DR.stage2(cfg, ticker, py, llm, facts, store=store)
    item.update({"source": entry["source"], "reason": entry["reason"],
                 "facts": facts, "py": py, "d": d})
    return item


def one_item_dry_run(store, cfg, d, entry):
    """干跑单只票：只读库内已有数据，绝不联网、绝不调 LLM（裁定 1）。

    与 ``one_item`` 的差别：
      - 不调 ``FT.fetch``/``NF.fetch``（会分别打 EDGAR 与新闻站的网络请求），
        财报正文和新闻正文对干跑没有意义 —— 反正也不会喂给 LLM。
      - ``FA.collect`` 的 ``fin`` 参数注入一个空实现，跳过 yfinance 网络
        请求；相关性、Form 4 申报数、热度排名这三条本来就是纯本地 SQL，
        不受影响，仍然能判定。
      - 不调 ``DR.stage1``/``DR.stage2``（两次真实 LLM 调用），直接用 py
        六条的命中/总数套 ``DR.label_for`` 得到标签，叙述用固定占位文案。
    """
    ticker = entry["ticker"]
    try:
        held = [h.ticker for h in PF.build(store, cfg).holdings]
    except Exception:
        held = []
    facts = FA.collect(store, cfg, ticker, d, held_tickers=held,
                       fin=lambda _ticker: {})
    py = CR.evaluate(facts)
    tag = DR.label_for(py["hit"], py["total"], 0)
    return {"ticker": ticker, "label": tag["label"], "note": tag["note"],
            "hit": py["hit"], "total": py["total"],
            "narrative": "[dry-run] 未调用 LLM，本节只展示 py 六条硬指标的判定结果。",
            "disagreements": [], "text_hits": {}, "text_quotes": {},
            "model": "dry-run（未调用 LLM）", "disclaimer": CR.DISCLAIMER,
            "source": entry["source"], "reason": entry["reason"],
            "facts": facts, "py": py, "d": d}


def build_items(store, cfg, d, entries, one_item=one_item):
    """逐只跑深读。单只失败只跳过那一只。"""
    items = []
    for entry in entries:
        try:
            items.append(one_item(store, cfg, d, entry))
        except Exception as exc:
            log(f"  ⚠️ {entry['ticker']} 深读失败：{type(exc).__name__}: {exc}")
            try:
                store.log_health("deepread", False, 0,
                                 f"{entry['ticker']} {type(exc).__name__}")
            except Exception:
                pass
    return items


def emit(store, cfg, d, items, dry_run=False, report_dir=None):
    """写报告、落结果、入队。一只票一条推送。"""
    markdown = RD.render_report(d, items)
    if dry_run:
        print(markdown)
        for item in items:
            title, body = RD.render_push(item)
            log(f"[dry-run] 推送标题：{title}")
            print(body)
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    with store.tx() as c:
        for item in items:
            c.execute(
                "INSERT OR REPLACE INTO deepread_results(d,ticker,source,py_hits,"
                "llm_hits,disagreements,score_hit,score_total,label,narrative,"
                "model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (d, item["ticker"], item.get("source", ""),
                 json.dumps(item["py"]["hits"], ensure_ascii=False),
                 json.dumps(item.get("text_hits") or {}, ensure_ascii=False),
                 len(item.get("disagreements") or []), item["hit"], item["total"],
                 item["label"], item["narrative"], item["model"], now))
        c.execute(
            "INSERT INTO reports(d,kind,body_md,created_at,logical_date) VALUES (?,?,?,?,?) "
            "ON CONFLICT(d,kind) DO UPDATE SET body_md=excluded.body_md,"
            "created_at=excluded.created_at", (d, "pool", markdown, now, d))
    def _queued():
        return store.q("SELECT COUNT(*) c FROM outbox WHERE kind='pool' "
                       "AND logical_date=?", (d,))[0]["c"]

    for item in items:
        title, body = RD.render_push(item)
        before = _queued()
        # event_key 让重跑变成幂等：同一天同一只票只会有一条。
        OB.enqueue(store, "pool", title, body, priority="default",
                   logical_date=d, event_key=f"pool:{item['ticker']}")
        inserted += int(_queued() > before)

    out_dir = Path(report_dir) if report_dir else REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"pool_{d}.md").write_text(markdown, encoding="utf-8")
    return inserted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", help="指定逻辑日期，默认今天")
    ap.add_argument("--limit", type=int, default=3, help="新票名额，默认 3")
    a = ap.parse_args()

    d = a.date or local_logical_date()
    st = Store.open_read_only(CFG.db_path) if a.dry_run else Store(CFG.db_path)
    try:
        entries = PL.build(st, CFG, d, attributions=_attributions(st, CFG, d),
                           new_slots=a.limit)
        log(f"入池 {len(entries)} 只：" + ", ".join(e["ticker"] for e in entries))
        # 裁定 1：干跑换一条完全离线的单只票处理路径，不联网、不调 LLM。
        item_fn = one_item_dry_run if a.dry_run else one_item
        items = build_items(st, CFG, d, entries, one_item=item_fn)
        log(f"深读成功 {len(items)} 只")
        n = emit(st, CFG, d, items, dry_run=a.dry_run)
        log(f"入队 {n} 条")
        return 0
    except Exception:
        log("❌ 深读主流程异常：\n" + traceback.format_exc())
        if not a.dry_run:
            try:
                st.log_health("run_pool", False, 0, "pipeline_exception")
            except Exception:
                pass
        return 1
    finally:
        st.close()


if __name__ == "__main__":
    sys.exit(main())
