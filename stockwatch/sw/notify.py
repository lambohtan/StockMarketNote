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
import re
from datetime import datetime
import requests

from . import outbox as OB
from .market_time import et_today

# 匹配 $ 后面跟数字（允许中间有空格、逗号、小数点）
MONEY_RE = re.compile(r"\$\s*\d[\d,]*(\.\d+)?")

TAGS = {
    "daily": "chart_with_upwards_trend",
    "weekly": "calendar",
    "l1": "rotating_light",
    "failure": "x",
}


def assert_no_money(text):
    """推送正文的金额守卫。命中就抛，绝不静默发出去。"""
    m = MONEY_RE.search(text or "")
    if m:
        raise ValueError(
            f"推送正文里出现金额 {m.group(0)!r} —— ntfy.sh 是公共服务器，"
            f"账户金额不能出现在推送里（详情放本地面板）")


def send(cfg, title, body, priority="default", tags=None, dry_run=False):
    """发一条 ntfy。返回 (ok, detail)。"""
    url = cfg.ntfy_url
    if not url:
        return False, "config.yaml 的 notify.ntfy_topic 是空的"
    assert_no_money(body)
    assert_no_money(title)
    if dry_run:
        print(f"[dry-run] → {url}\n  [{priority}] {title}\n  {body[:300]}")
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


def drain(store, cfg, dry_run=False, today=None):
    """
    把队列里所有未发送的条目发出去。

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
    today = today or et_today().isoformat()
    failure_reported = False

    if (not OB.has_kind_on(store, "daily", today)
            and not OB.has_kind_on(store, "failure", today)):
        # 第二个条件是同一天不重复上报的关键：只看「今天有没有 daily」的话，
        # 只要今天一直没跑成，drain 每次被调用（补跑、定时）都会再入队一条
        # failure，把手机刷屏。「今天已经报过一次失败」本身也该算数。
        last = store.q("SELECT substr(created_at,1,10) d FROM outbox "
                       "WHERE kind='daily' ORDER BY created_at DESC LIMIT 1")
        last_ok = last[0]["d"] if last else "从未成功过"
        # created_at 的日期部分锚定到 today（而不是 datetime.now()），
        # 否则 has_kind_on(store, "failure", today) 在「今天」是调用方注入的
        # 逻辑日期（测试、或补跑时的美股交易日）而非真实挂钟日期时会找不到
        # 刚入队的这条，导致「同一天不重复上报」失效。
        created_at = today + datetime.now().strftime("T%H:%M:%S")
        OB.enqueue(store, "failure",
                   f"StockWatch {today} 没跑成",
                   f"今天的计算任务没有产出日报。\n"
                   f"最后一次成功：{last_ok}\n"
                   f"排查：查看 logs/ 目录，或在菜单栏里点「立即运行一次」。",
                   priority="high", created_at=created_at)
        failure_reported = True

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
        except ValueError as e:
            # 金额守卫命中：计为失败留在队列里，绝不静默发出去
            ok, detail = False, str(e)
        if ok:
            sent += 1  # claim 已经写了 sent_at，不需要再调 mark_sent
        else:
            OB.release(store, row["id"], token)  # 归还领取，让下次 drain 重试
            OB.mark_failed(store, row["id"], detail)
            failed += 1
        store.log_health("ntfy", ok, 0, f"{row['kind']}: {detail}")

    return {"sent": sent, "failed": failed, "failure_reported": failure_reported}
