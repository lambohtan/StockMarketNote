"""
ntfy 推送 + outbox drain。

⚠️ ntfy.sh 是公共服务器，topic 名是随机串（隐蔽性，不是加密）。
任何知道 topic 的人都能读到推送内容，所以正文里**绝不出现账户金额**。
知道用户在看 NVDA 是一回事，知道账户里有多少钱是另一回事。

⚠️ 并发领取：run_daily（跑完发现已过推送点会自 drain）和 run_notify
（08:00 定时 drain）是两个不同的 launchd Label。Mac 从 06:00 睡到 08:30
唤醒后，launchd 会同时补跑两者 —— 两边各自的 outbox.unsent() 都可能拿到
同一条「看起来还没发」的记录。所以 drain 不能走「unsent → 发送 → mark_sent」
这种非原子流程，必须对每一条先 outbox.claim()，抢到令牌的一方才真的发送；
没抢到的直接跳过，交由抢到的一方负责。详见 sw/outbox.py 顶部的说明。
"""
from datetime import datetime
import requests

from . import outbox as OB
from .clock import logical_date as local_logical_date
from .policy import MONEY_RE, assert_no_directives, assert_no_money

TAGS = {
    "daily": "chart_with_upwards_trend",
    "weekly": "calendar",
    "l1": "rotating_light",
    "failure": "x",
}


def send(cfg, title, body, priority="default", tags=None, dry_run=False, kind=None):
    """发一条 ntfy。返回 (ok, detail)。"""
    assert_no_money(body)
    assert_no_money(title)
    assert_no_directives(body)
    assert_no_directives(title)
    url = cfg.ntfy_url
    if not url:
        return False, "notify endpoint 未配置"
    if dry_run:
        # 干跑只展示内容，不把配置里的 topic（等同于密码）打印到日志。
        print(f"[dry-run] → ntfy endpoint\n  [{priority}] {title}\n  {body[:300]}")
        return True, "dry-run"
    try:
        headers = {
            "Title": (title or "StockWatch").encode("utf-8"),
            "Priority": priority,
        }
        if tags:
            headers["Tags"] = tags
        r = requests.post(url, data=(body or "").encode("utf-8"),
                          headers=headers, timeout=20)
        if r.ok:
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code} {r.text[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _failure_grace_active(store, logical_day, cfg, explicit_day, now=None,
                          grace_seconds=None, allow_persist=True):
    """判断 watchdog 是否仍在持久化 grace 窗口内。"""
    info = store.run_info(logical_day) if hasattr(store, "run_info") else None
    if info and info.get("status") == "completed":
        # completed 是 emit 事务最后写入的标记；有它就不能产生 failure。
        return False, True
    supplied_grace = grace_seconds is not None
    if grace_seconds is None:
        # 老的直接调用传入 today 是兼容测试/脚本语义；正式 CLI 不传，走
        # 配置的持久化 grace。生产 run_daily 会先写 runs running 记录。
        grace_seconds = 0 if explicit_day else int(
            cfg.get("schedule.failure_grace_seconds", 900) or 900)
    if not info and grace_seconds > 0 and (not explicit_day or supplied_grace):
        # notify 可能先于 compute 被 launchd 补跑；把这次预期持久化为
        # running/grace，下一次唤醒再判断，避免一次偶发顺序颠倒就误报。
        if allow_persist and hasattr(store, "begin_run"):
            store.begin_run(logical_day, grace_seconds=grace_seconds, now=now)
            return True, False
        if not allow_persist:
            return True, False
    if info and info.get("grace_until"):
        current = (now or datetime.now()).isoformat(
            timespec="seconds")
        if current < info["grace_until"]:
            return True, False
    return False, False


def drain(store, cfg, dry_run=False, today=None, logical_date=None,
          now=None, grace_seconds=None):
    """
    把队列里所有未发送的条目发出去。

    dry_run=True 是严格只读的预览：会读取队列并调用 send(..., dry_run=True)
    展示内容，但绝不入队、claim、release、记失败或写健康记录。

    发送前先做失败检测：今天如果**从来没有**入队过 daily 条目，
    说明计算任务没跑成（可能是硬崩溃，try/except 抓不到），
    入队一条 failure 通知。用户不会每天开 app，
    所以「没有消息」绝不能等于「没事」。

    发送本身走 claim/release 原语而不是「unsent → 发送 → mark_sent」：
    unsent() 只是一次独立的 SELECT，和「真的发出这条推送」之间没有原子性。
    两个 launchd 任务（run_daily 自 drain、run_notify 定时 drain）睡过头后
    可能同时跑到这里，各自的 unsent() 都会看到同一条待发记录。claim() 用
    `UPDATE ... WHERE sent_at IS NULL` 的原子性保证只有一方能抢到令牌；
    抢不到的一方直接跳过，绝不重复发送。
    """
    explicit_day = today is not None or logical_date is not None
    today = logical_date or today or local_logical_date(now)
    failure_reported = False
    failure_preview = None

    effective_grace = (0 if dry_run and grace_seconds is None else grace_seconds)
    grace_active, completed = _failure_grace_active(
        store, today, cfg, explicit_day, now=now, grace_seconds=effective_grace,
        allow_persist=not dry_run)
    if (not completed and not grace_active
            and not OB.has_kind_on(store, "daily", today)
            and not OB.has_kind_on(store, "failure", today)):
        # 第二个条件是同一天不重复上报的关键：只看「今天有没有 daily」的话，
        # 只要今天一直没跑成，drain 每次被调用（补跑、定时）都会再入队一条
        # failure，把手机刷屏。「今天已经报过一次失败」本身也该算数。
        last = store.q("SELECT logical_date d FROM outbox "
                       "WHERE kind='daily' ORDER BY logical_date DESC, id DESC LIMIT 1")
        last_ok = last[0]["d"] if last else "从未成功过"
        failure_preview = {
            "kind": "failure",
            "priority": "high",
            "title": f"StockWatch {today} 没跑成",
            "body": f"今天的计算任务未产出日报。\n"
                    f"最后一次成功：{last_ok}。",
        }
        if not dry_run:
            # logical_date/event_key 是失败事件的持久化身份；created_at 只保留
            # 发送审计时间，不承担跨时区/跨交易日幂等。
            created_at = (now or datetime.now()).isoformat(timespec="seconds")
            OB.enqueue(store, "failure", failure_preview["title"],
                       failure_preview["body"], priority="high",
                       created_at=created_at, logical_date=today,
                       event_key="failure")
            # 第一次 watchdog wake 仅建立 running/grace 标记，不立即宣称失败；
            # 失败事件入队后仍可在下一次 wake 重复读取而保持幂等。
        failure_reported = True

    if dry_run:
        # 预览 failure（正式模式下它会先入队）和现有未发送条目，但不使用
        # claim/release，也不写 attempts、last_error 或 source_health。
        rows = ([failure_preview] if failure_preview else []) + OB.unsent(store)
        sent = failed = 0
        for row in rows:
            try:
                ok, detail = send(cfg, row["title"], row["body"],
                                  priority=row["priority"] or "default",
                                  tags=TAGS.get(row["kind"]), dry_run=True)
            except Exception as e:
                # 金额守卫命中：干跑也要报告失败，但不能把错误写回数据库。
                ok, detail = False, str(e)
            if ok:
                sent += 1
            else:
                failed += 1
        return {"sent": sent, "failed": failed,
                "failure_reported": failure_reported}

    sent = failed = 0
    for row in OB.unsent(store):
        token = OB.claim(store, row["id"])
        if token is None:
            continue  # 已被另一个任务领走，直接跳过
        try:
            ok, detail = send(cfg, row["title"], row["body"],
                              priority=row["priority"] or "default",
                              tags=TAGS.get(row["kind"]),
                              dry_run=dry_run)
        except Exception as e:
            # 金额守卫命中：计为失败留在队列里，绝不静默发出去
            ok, detail = False, str(e)
        if ok:
            # 只有远端成功确认后才落 sent_at；claim 本身不改变发送状态。
            if OB.mark_sent(store, row["id"], token=token):
                sent += 1
            else:
                # lease 在极慢的发送期间可能已过期并被另一进程接管；
                # owner-safe mark_sent 失败时不伪造本进程的成功计数。
                continue
        else:
            # 先由当前 lease owner 记失败，再释放 claim；错误 token 无法改动
            # 他人的条目。
            OB.mark_failed(store, row["id"], detail, token=token)
            OB.release(store, row["id"], token)
            failed += 1
        store.log_health("ntfy", ok, 0, f"{row['kind']}: {detail}")

    return {"sent": sent, "failed": failed, "failure_reported": failure_reported}
