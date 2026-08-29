#!/usr/bin/env python3
"""
每日计算主流程（launchd 06:00 调这个）。

⚠️ 这个脚本**只入队，不发送**。推送由 run_notify.py 在 08:00 统一负责。
拆开的理由见设计文档 §2.1：08:00 那个独立任务发现「今天没有 daily 条目」
就能上报失败 —— 哪怕本脚本是被 OOM killer 干掉的，try/except 抓不到那种情况。

用法：
  python3 run_daily.py                    # 正常跑
  python3 run_daily.py --dry-run          # 全流程但不写库不入队
  python3 run_daily.py --skip-ingest      # 跳过抓数，用库里现有数据
  python3 run_daily.py --force            # 当天已跑过也重新入队
"""
import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sw.config import CFG
from sw.store import Store
from sw import outbox as OB, notify as NT, alerts as AL, llm as LM
from sw import daily_report as DR
from sw.analysis import portfolio as PF, attribution as AT, causes as CS, watchlist as WL
from sw.market_time import et_today, now_et


def log(m):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {m}", flush=True)


def _watchlist(store, d, held):
    try:
        return WL.collect(store, d, held)
    except Exception as e:
        log(f"  ⚠️ 观察池汇总失败：{type(e).__name__}: {e}")
        return []


def build_context(store, cfg, snapshot_date=None, window=60, use_llm=True):
    """跑完全部确定性计算，返回渲染用的 ctx。这一层不写库、不入队，方便测试。"""
    p = PF.build(store, cfg, snapshot_date)
    sector_map = cfg.get("benchmarks.sectors") or {}
    market_etf = cfg.get("benchmarks.market", "SPY")

    # 1. 归因
    attributions = []
    for h in p.holdings:
        etf = sector_map.get(h.sector) if h.sector else None
        try:
            r = AT.attribute(store, h.ticker, etf, market_etf, window)
        except Exception as e:
            log(f"  ⚠️ {h.ticker} 归因失败：{type(e).__name__}: {e}")
            r = None
        if r:
            r["sector"] = h.sector
            attributions.append(r)

    d = attributions[0]["d"] if attributions else et_today().isoformat()

    # 2. 只给异动的找原因 —— 正常的不查，省时间也省额度
    causes_by_ticker = {}
    for a in attributions:
        if a["level"] == "normal":
            continue
        peers = CS.peer_readthrough(attributions, a["ticker"], a.get("sector"))
        try:
            causes_by_ticker[a["ticker"]] = CS.find_causes(
                store, a["ticker"], a["d"], peers=peers)
        except Exception as e:
            log(f"  ⚠️ {a['ticker']} 找原因失败：{type(e).__name__}: {e}")
            causes_by_ticker[a["ticker"]] = []

    # 3. 恶化扫描
    alerts = AL.scan(store, p, attributions, causes_by_ticker)

    # 4. LLM 摘要 —— 全流程唯一用 LLM 的地方
    if use_llm and causes_by_ticker:
        for tk, cs in causes_by_ticker.items():
            if not cs:
                continue
            material = "\n".join(f"[{c['source']}] {c['summary']}" for c in cs)
            s = LM.summarize(cfg, material,
                             f"下面是 {tk} 今日异动的相关材料，压成一句话说明发生了什么。",
                             store=store)
            if s:
                cs.insert(0, {"source": "摘要", "summary": s, "url": "",
                              "item": None, "is_l1": False})

    return {
        "d": d,
        "attributions": attributions,
        "causes_by_ticker": causes_by_ticker,
        "alerts": alerts,
        "watchlist_events": _watchlist(store, d, {h.ticker for h in p.holdings}),
        "market": {},                # 可选，缺失时报告自动省略该节
        "portfolio_weights": p.weights(),
        "n_holdings": len(p.holdings),
    }


def emit(store, cfg, ctx, dry_run=False, force=False):
    """渲染 + 写库 + 入队。返回入队条目数。"""
    if not force and OB.has_kind_on(store, "daily", ctx["d"]):
        log(f"  {ctx['d']} 已经入过队，跳过（用 --force 强制重跑）")
        return 0

    md = DR.render_markdown(ctx)
    title, body = DR.render_push(ctx)
    if dry_run:
        log("─" * 60); print(md); log("─" * 60)
        log(f"[dry-run] 推送标题：{title}"); print(body)
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    store.upsert_many("reports", ["d", "kind", "body_md", "created_at"],
                      [(ctx["d"], "daily", md, now)])
    out = Path(__file__).resolve().parent / "reports" / f"daily_{ctx['d']}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")

    n = 0
    # created_at 锚定到 ctx['d']（本次计算所依据的交易日），而不是入队那一刻
    # 的挂钟时间 —— has_kind_on() 的「当天是否入过队」判断按 created_at 的
    # 日期部分匹配，07:00 重试任务和 06:00 首跑算的是同一个交易日，两次
    # created_at 必须落在同一天才能被判定为「已经入过队」，见
    # tests/test_outbox.py::test_has_kind_on 里同样的用法。
    OB.enqueue(store, "daily", title, body, priority="default",
              created_at=ctx["d"])
    n += 1
    for al in ctx["alerts"]:
        if al["level"] != "L1":
            continue
        t, b = AL.render_alert_push(al)
        OB.enqueue(store, "l1", t, b, priority="urgent")
        n += 1

    # alerts 表也留一份，供面板和以后的 P7 回看
    store.upsert_many("alerts", ["d", "ticker", "level", "category", "body_md"],
                      [(ctx["d"], a["ticker"], a["level"], a["category"],
                        AL.render_alert(a)) for a in ctx["alerts"]])
    log(f"  ✅ 入队 {n} 条，报告写入 {out.name}")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--window", type=int, default=60)
    a = ap.parse_args()

    st = Store(CFG.db_path)
    try:
        LM.check_env(CFG)          # 配置错了立刻退出，不要跑完才发现
    except LM.LLMConfigError as e:
        log(f"❌ LLM 配置有问题：{e}")
        st.close()
        return 1

    try:
        if not a.skip_ingest:
            log("1/3 抓数")
            import run_ingest
            sys.argv = ["run_ingest.py"] + (["--dry-run"] if a.dry_run else [])
            run_ingest.main()

        log("2/3 计算")
        ctx = build_context(st, CFG, window=a.window)
        log(f"  {len(ctx['attributions'])} 只归因成功，"
            f"{sum(1 for x in ctx['attributions'] if x['level'] != 'normal')} 只异动，"
            f"{len(ctx['alerts'])} 条提醒")

        log("3/3 渲染与入队")
        emit(st, CFG, ctx, dry_run=a.dry_run, force=a.force)

        # 睡过头的堵法：已经过了推送点就自己 drain 一次。
        # 注意调的是 notify.drain 这**同一个函数**，不是复制一份逻辑。
        push_at = str(CFG.get("schedule.push_time", "08:00"))
        if not a.dry_run and datetime.now().strftime("%H:%M") >= push_at:
            log(f"  当前已过推送点 {push_at}，立即 drain 一次")
            r = NT.drain(st, CFG)
            log(f"  drain: 发出 {r['sent']} 条，失败 {r['failed']} 条")

        st.close()
        return 0
    except Exception:
        log("❌ 主流程异常：\n" + traceback.format_exc())
        st.log_health("run_daily", False, 0, traceback.format_exc()[-400:])
        st.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
