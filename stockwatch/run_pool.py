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


def _held_tickers(store, cfg):
    """当前持仓代码列表。没有持仓快照时返回空列表（相关性那条判 None，
    不影响其余五条）——`one_item`/`one_item_dry_run` 在没被显式传入
    `held` 时各自回退到这里，两处共享同一份异常处理，不再逐字重复。
    """
    try:
        return [h.ticker for h in PF.build(store, cfg).holdings]
    except Exception:
        return []


def _attributions(store, cfg, d, holdings):
    """就地重算持仓归因，只为拿到「哪几只异动」。纯本地计算，不联网。

    ``holdings`` 由调用方（``main()``）传入，是已经 ``PF.build()`` 过一次的
    持仓列表 —— 不在这里重新查库，避免一天里对同一份持仓快照重复建
    Portfolio。

    与 ``run_daily.build_context`` 的写法对齐（此前这里两处都反了，是 I4
    修复的内容）：
      1. 行业未映射到 ETF 时 ``etf`` 取 ``None`` 而不是 ``continue`` 跳过
         这只持仓 —— ``AT.attribute`` 的 docstring 明说 ``sector_etf=None``
         会退化为单因子，不是不能算。原来的 ``continue`` 会让「行业未知」
         的持仓即使被判极端异动也进不了深读池，与 spec「持仓异动全读」矛盾。
      2. try/except 是**逐票**的，不是包住整个循环 —— 原来任意一只票的
         ``AT.attribute`` 抛异常会让整个函数返回 ``[]``，当天
         ``holding_anomaly`` 这个来源整个消失；改成单票失败只跳过那一只，
         其余持仓照常参与归因。
    """
    sectors = cfg.get("benchmarks.sectors") or {}
    market = cfg.get("benchmarks.market", "SPY")
    out = []
    for h in holdings:
        etf = sectors.get(h.sector) if h.sector else None
        try:
            r = AT.attribute(store, h.ticker, etf, market, 60)
        except Exception as exc:
            log(f"  ⚠️ {h.ticker} 归因失败：{type(exc).__name__}: {exc}")
            r = None
        if r:
            out.append(r)
    return out


def one_item(store, cfg, d, entry, held=None):
    """跑完一只票的完整深读。任何一步失败由调用方捕获。

    ``held``：当前持仓代码列表，`main()` 在一天里只算一次后传入；
    未传入（比如测试直接调用）时回退到 `_held_tickers()` 自己查一次。
    """
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

    held = _held_tickers(store, cfg) if held is None else held
    facts = FA.collect(store, cfg, ticker, d, held_tickers=held)
    py = CR.evaluate(facts)

    material = DR.build_material(ticker, sections, nr.data or [], facts)
    llm = DR.stage1(cfg, ticker, material, store=store)
    item = DR.stage2(cfg, ticker, py, llm, facts, store=store)
    item.update({"source": entry["source"], "reason": entry["reason"],
                 "facts": facts, "py": py, "d": d})
    return item


def one_item_dry_run(store, cfg, d, entry, held=None):
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
    held = _held_tickers(store, cfg) if held is None else held
    facts = FA.collect(store, cfg, ticker, d, held_tickers=held,
                       fin=lambda _ticker: {})
    py = CR.evaluate(facts)
    # 干跑跳过 yfinance，所以 is_fund 恒为 False —— ETF 在干跑里认不出来，
    # 报告里如实说明，别让人以为基金判定失效了。
    tag = DR.label_for(py["hit"], py["total"], 0,
                       is_fund=bool(facts.get("is_fund")))
    return {"ticker": ticker, "label": tag["label"], "note": tag["note"],
            "hit": py["hit"], "total": py["total"],
            "narrative": "[dry-run] 未调用 LLM，本节只展示 py 六条硬指标的判定结果。\n"
                         "干跑不调 yfinance，因此 ETF/基金在这里认不出来（真跑才判）。",
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
    """写报告、落结果、入队。一只票一条推送。

    I5 修复：``deepread_results.llm_hits`` 按 spec §4 应该是「8 条 LLM 判定
    + 引用」，此前只写了 ``text_hits``（2 条文本判定），LLM 对 py 那 6 条
    的独立对照判定（``llm_hits``/``llm_quotes``）从未落库。这张表存在的
    唯一理由是「一年后回看当时是怎么判的」和 P7 信号有效性追踪——交叉
    验证的证据恰恰是最该留的那部分，所以这里把 stage2 带出来的
    ``llm_hits``/``llm_quotes``/``text_hits``/``text_quotes`` 四件套整个
    存成一个 JSON 对象；``disagreements`` 列仍是计数（不改 spec 的列
    类型），分歧的具体内容存进新增的 ``disagreements_detail`` 列。
    """
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
            llm_hits_full = {
                "hits": item.get("llm_hits") or {},
                "quotes": item.get("llm_quotes") or {},
                "text_hits": item.get("text_hits") or {},
                "text_quotes": item.get("text_quotes") or {},
            }
            c.execute(
                "INSERT OR REPLACE INTO deepread_results(d,ticker,source,py_hits,"
                "llm_hits,disagreements,disagreements_detail,score_hit,score_total,"
                "label,narrative,model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d, item["ticker"], item.get("source", ""),
                 json.dumps(item["py"]["hits"], ensure_ascii=False),
                 json.dumps(llm_hits_full, ensure_ascii=False),
                 len(item.get("disagreements") or []),
                 json.dumps(item.get("disagreements") or [], ensure_ascii=False),
                 item["hit"], item["total"],
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


def _alert_all_failed(store, d, n_entries):
    """入池 N 只但全部深读失败：必须推送，沉默不等于没事（spec §8：I6）。

    与「今天没有票入池」区分开——那种情况 ``entries`` 本身就是空的，是
    正常的「没有票触发入池条件」，``emit()`` 会照常写一份「0 只」的报告，
    不需要额外推送。这里是相反的情形：明明入池了，却一只都没读出结果，
    看门狗必须能看到。``notify.drain`` 的看门狗只认 ``kind='daily'``，
    不会替 ``run_pool`` 兜底，所以这条警报只能在这里显式入队。
    """
    title = f"股票池深读 {d} 未产出"
    body = (f"今日入池 {n_entries} 只，但全部深读失败，本次没有任何一只票产出结果。\n"
            "详情见 source_health 里的 deepread 记录。")
    try:
        OB.enqueue(store, "pool", title, body, priority="high",
                   logical_date=d, event_key=f"pool-all-failed:{d}")
    except Exception:
        pass
    try:
        store.log_health("run_pool", False, 0,
                         f"入池 {n_entries} 只但全部深读失败")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", help="指定逻辑日期，默认今天")
    ap.add_argument("--limit", type=int, default=None,
                    help="新票名额，默认读 config.yaml 的 pool.new_slots（8）")
    a = ap.parse_args()

    d = a.date or local_logical_date()
    st = Store.open_read_only(CFG.db_path) if a.dry_run else Store(CFG.db_path)
    try:
        # 一天只建一次 Portfolio：_attributions 要完整 Holding 对象算行业
        # 归因，one_item/one_item_dry_run 只要代码列表算相关性——此前两处
        # 各自独立调 PF.build，一天里对同一份持仓快照重复查库 N+1 次。
        try:
            holdings = PF.build(st, CFG).holdings
        except Exception as exc:
            log(f"  ⚠️ 持仓读取失败，本次只用热度与 Form 4 入池：{type(exc).__name__}: {exc}")
            holdings = []
        held = [h.ticker for h in holdings]

        slots = a.limit if a.limit is not None else int(
            CFG.get("pool.new_slots", PL.DEFAULT_NEW_SLOTS) or PL.DEFAULT_NEW_SLOTS)
        # held 全量传进去：持仓的入池不再只看价格异动，还看重大 8-K、
        # 财报刚发布、Form 4 集中 —— 平静的持仓一条都不会冒出来。
        entries = PL.build(st, CFG, d, attributions=_attributions(st, CFG, d, holdings),
                           new_slots=slots, held_tickers=held)
        log(f"入池 {len(entries)} 只：" + ", ".join(e["ticker"] for e in entries))
        # 裁定 1：干跑换一条完全离线的单只票处理路径，不联网、不调 LLM。
        if a.dry_run:
            item_fn = lambda s, c, dd, e: one_item_dry_run(s, c, dd, e, held=held)
        else:
            item_fn = lambda s, c, dd, e: one_item(s, c, dd, e, held=held)
        items = build_items(st, CFG, d, entries, one_item=item_fn)
        log(f"深读成功 {len(items)} 只")
        # I6：入池了但一只都没读出来，必须响亮，不能悄悄写一份空报告了事。
        if entries and not items and not a.dry_run:
            _alert_all_failed(st, d, len(entries))
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
