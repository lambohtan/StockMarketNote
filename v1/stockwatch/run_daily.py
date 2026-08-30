#!/usr/bin/env python3
"""每日计算主流程：计算与发送分离，计算只写入 outbox。"""
import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sw.config import CFG
from sw.store import Store
from sw import outbox as OB, notify as NT, alerts as AL, llm as LM
from sw import daily_report as DR
from sw.analysis import portfolio as PF, attribution as AT, causes as CS, watchlist as WL
from sw.clock import logical_date as local_logical_date

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "stockwatch.db"


def log(message):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def _watchlist(store, d, held):
    try:
        return WL.collect(store, d, held)
    except Exception as exc:
        log(f"  ⚠️ 观察池汇总失败：{type(exc).__name__}: {exc}")
        return []


def _record_cause_health(store, causes):
    """把原因适配器状态写入 source_health；只写类型/覆盖摘要，不写材料。"""
    if getattr(store, "read_only", False):
        return
    for item in getattr(causes, "health", []) or []:
        try:
            detail = str(item.get("detail") or "")[:500]
            store.log_health(item.get("source", "causes"), item.get("ok", False),
                             0, detail)
        except Exception:
            # 健康记录是旁路观测，不能让它击穿日报主链路。
            continue


def build_context(store, cfg, snapshot_date=None, window=60, use_llm=True,
                  logical_date=None, persist_health=True):
    """跑确定性计算并返回渲染 ctx。

    ``d``/``market_date`` 是最后完整交易日；``logical_date`` 是本机调度日，
    两者即使跨周末或跨时区也不混用。直接库调用未显式传 logical_date 时，
    为兼容旧 API 回退到 market_date；生产 main 总是显式传入本机墙钟日。
    """
    portfolio = PF.build(store, cfg, snapshot_date)
    sector_map = cfg.get("benchmarks.sectors") or {}
    market_etf = cfg.get("benchmarks.market", "SPY")

    attributions = []
    for holding in portfolio.holdings:
        etf = sector_map.get(holding.sector) if holding.sector else None
        try:
            result = AT.attribute(store, holding.ticker, etf, market_etf, window)
        except Exception as exc:
            log(f"  ⚠️ {holding.ticker} 归因失败：{type(exc).__name__}: {exc}")
            result = None
        if result:
            result["sector"] = holding.sector
            attributions.append(result)

    market_date = (attributions[0]["d"] if attributions
                   else portfolio.snapshot_date or local_logical_date())
    logical_day = logical_date or market_date

    # 异动票走完整原因链；正常票只读本地 8-K L1，避免无意义的新闻/LLM 调用。
    causes_by_ticker = {}
    for attribution in attributions:
        ticker = attribution["ticker"]
        if attribution["level"] == "normal":
            try:
                l1 = CS.find_l1_causes(store, ticker, market_date)
            except Exception as exc:
                log(f"  ⚠️ {ticker} L1 查询失败：{type(exc).__name__}: {exc}")
                l1 = []
            if persist_health:
                _record_cause_health(store, l1)
            if l1:
                causes_by_ticker[ticker] = l1
            continue
        peers = CS.peer_readthrough(attributions, ticker, attribution.get("sector"))
        try:
            causes = CS.find_causes(store, ticker, market_date, peers=peers)
        except Exception as exc:
            log(f"  ⚠️ {ticker} 找原因失败：{type(exc).__name__}: {exc}")
            causes = []
        causes_by_ticker[ticker] = causes
        if persist_health:
            _record_cause_health(store, causes)

    alerts = AL.scan(store, portfolio, attributions, causes_by_ticker)

    if use_llm and causes_by_ticker:
        anomalous = {a["ticker"] for a in attributions
                     if a.get("level") != "normal"}
        for ticker, causes in causes_by_ticker.items():
            # 正常票的 L1 只走本地文件，不因为正常而调用 LLM。
            if ticker not in anomalous or not causes:
                continue
            material = "\n".join(f"[{c['source']}] {c['summary']}" for c in causes)
            summary = LM.summarize(
                cfg, material, f"下面是 {ticker} 今日异动的相关材料，压成一句话说明发生了什么。",
                store=store)
            if summary:
                causes.insert(0, {"source": "摘要", "summary": summary, "url": "",
                                  "item": None, "is_l1": False})

    return {
        "d": market_date,                 # 旧渲染 API 的 market_date 别名
        "market_date": market_date,
        "logical_date": logical_day,
        "attributions": attributions,
        "causes_by_ticker": causes_by_ticker,
        "alerts": alerts,
        "watchlist_events": _watchlist(store, market_date,
                                        {h.ticker for h in portfolio.holdings}),
        "market": {},
        "portfolio_weights": portfolio.weights(),
        "n_holdings": len(portfolio.holdings),
    }


def _insert_outbox(c, created_at, logical_day, event_key, kind, priority, title, body):
    cur = c.execute(
        "INSERT OR IGNORE INTO outbox(created_at,logical_date,event_key,kind,priority,"
        "title,body,attempts) VALUES (?,?,?,?,?,?,?,0)",
        (created_at, logical_day, event_key, kind, priority, title, body))
    return cur.rowcount == 1


def emit(store, cfg, ctx, dry_run=False, force=False, report_dir=None):
    """渲染并在一个 DB 事务中写报告、alerts、outbox，最后完成 run。"""
    market_day = ctx.get("market_date") or ctx["d"]
    logical_day = ctx.get("logical_date") or market_day
    existing = store.run_info(logical_day)
    if not force and existing and existing.get("status") == "completed":
        log(f"  {logical_day} 已完成，跳过（用 --force 强制重跑）")
        return 0
    # 旧库迁移前没有 runs 时，保留历史 daily 的幂等语义。
    if not force and not existing and OB.has_kind_on(store, "daily", logical_day):
        log(f"  {logical_day} 已经入过队，跳过（用 --force 强制重跑）")
        return 0

    markdown = DR.render_markdown(ctx)
    title, body = DR.render_push(ctx)
    if dry_run:
        log("─" * 60)
        print(markdown)
        log("─" * 60)
        log(f"[dry-run] 推送标题：{title}")
        print(body)
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    daily_key = "daily" if not force else f"daily:force:{uuid4().hex}"
    inserted = 0
    with store.tx() as c:
        # 计算开始/恢复标记。completed 只有本事务最后才会写。
        c.execute(
            "INSERT INTO runs(logical_date,market_date,status,started_at,grace_until,attempt_count) "
            "VALUES (?,?, 'running', ?, ?, 1) ON CONFLICT(logical_date) DO UPDATE SET "
            "market_date=excluded.market_date,status='running',started_at=excluded.started_at,"
            "grace_until=excluded.grace_until,attempt_count=runs.attempt_count+1",
            (logical_day, market_day, now, now))
        c.execute(
            "INSERT INTO reports(d,kind,body_md,created_at,logical_date) VALUES (?,?,?,?,?) "
            "ON CONFLICT(d,kind) DO UPDATE SET body_md=excluded.body_md,"
            "created_at=excluded.created_at,logical_date=excluded.logical_date",
            (market_day, "daily", markdown, now, logical_day))
        inserted += int(_insert_outbox(c, now, logical_day, daily_key, "daily",
                                        "default", title, body))
        for alert in ctx.get("alerts", []):
            if alert["level"] != "L1":
                continue
            alert_title, alert_body = AL.render_alert_push(alert)
            key = f"l1:{alert['ticker']}:{alert['category']}"
            inserted += int(_insert_outbox(c, now, logical_day, key, "l1",
                                            "urgent", alert_title, alert_body))
            c.execute(
                "INSERT OR IGNORE INTO alerts(d,ticker,level,category,body_md,pushed,"
                "logical_date,event_key) VALUES (?,?,?,?,?,?,?,?)",
                (market_day, alert["ticker"], alert["level"], alert["category"],
                 AL.render_alert(alert), 0, logical_day, key))
        # completed 是事务的最后一个业务写入；任意前面异常都会整体 rollback。
        c.execute(
            "UPDATE runs SET status='completed',market_date=?,completed_at=?,last_error=NULL "
            "WHERE logical_date=?", (market_day, now, logical_day))

    if report_dir is None:
        # 测试/临时 DB 的报告也必须落在临时目录，避免验收过程覆盖用户报告；
        # 只有正式配置 DB 才使用项目的 reports/ 目录。
        # 不读取可变的 STOCKWATCH_DB 环境变量判断正式目录：测试 CLI 会临时
        # 改它，而那时仍必须把报告留在临时 DB 旁边。
        configured_db = DEFAULT_DB_PATH.resolve()
        store_db = Path(store.path).resolve()
        report_root = REPORTS_DIR if store_db == configured_db else store_db.parent / "reports"
    else:
        report_root = Path(report_dir)
    report_path = report_root / f"daily_{market_day}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(markdown, encoding="utf-8")
    log(f"  ✅ 入队 {inserted} 条，报告写入 {report_path.name}")
    return inserted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--window", type=int, default=60)
    args = parser.parse_args()

    logical_day = local_logical_date()
    st = Store.open_read_only(CFG.db_path) if args.dry_run else Store(CFG.db_path)
    try:
        if not args.dry_run:
            LM.check_env(CFG)
            if not args.force and (st.run_info(logical_day) or {}).get("status") == "completed":
                log(f"{logical_day} 已完成，重试任务退出")
                return 0
            st.begin_run(logical_day, grace_seconds=int(
                CFG.get("schedule.failure_grace_seconds", 900) or 900), force=args.force)

        if args.dry_run:
            log("[dry-run] 跳过 ingest（严格只读预览）")
        elif not args.skip_ingest:
            log("1/3 抓数")
            import run_ingest
            sys.argv = ["run_ingest.py"]
            run_ingest.main()

        log("2/3 计算")
        ctx = build_context(st, CFG, window=args.window, use_llm=not args.dry_run,
                            logical_date=logical_day, persist_health=not args.dry_run)
        log(f"  {len(ctx['attributions'])} 只归因成功，"
            f"{sum(1 for x in ctx['attributions'] if x['level'] != 'normal')} 只异动，"
            f"{len(ctx['alerts'])} 条提醒")

        log("3/3 渲染与入队")
        emit(st, CFG, ctx, dry_run=args.dry_run, force=args.force)

        push_at = str(CFG.get("schedule.push_time", "08:00"))
        if (not args.dry_run
                and datetime.now().strftime("%H:%M") >= push_at):
            log(f"  当前已过推送点 {push_at}，立即 drain 一次")
            result = NT.drain(st, CFG, logical_date=logical_day)
            log(f"  drain: 发出 {result['sent']} 条，失败 {result['failed']} 条")
        return 0
    except LM.LLMConfigError as exc:
        log(f"❌ LLM 配置有问题：{exc}")
        if not args.dry_run:
            try:
                st.mark_run_failed(logical_day, type(exc).__name__)
            except Exception:
                pass
        return 1
    except Exception:
        log("❌ 主流程异常：\n" + traceback.format_exc())
        if not args.dry_run:
            try:
                st.log_health("run_daily", False, 0, "pipeline_exception")
                st.mark_run_failed(logical_day, "pipeline_exception")
            except Exception:
                pass
        return 1
    finally:
        st.close()


if __name__ == "__main__":
    sys.exit(main())
