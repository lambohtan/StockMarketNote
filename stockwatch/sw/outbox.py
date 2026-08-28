"""
推送队列。

「算」和「发」之间的唯一接口：compute 任务只入队，notify 任务只出队。
这样即使计算任务硬崩溃（OOM / 被 kill / 段错误），
独立的 notify 任务照样能发现「今天该有的条目没有」并上报 ——
try/except 抓不到这类失败。

并发安全（claim/release 原语）：
  run_daily 和 run_notify 是两个不同的 launchd Label，Mac 睡过头时会并发补跑。
  claim() 是原子领取（返回 True/False），确保同一条不会被多个进程推送。
  如果失败可以 release() 退回。
"""
from datetime import datetime

KINDS = ("daily", "weekly", "l1", "failure")
PRIORITIES = ("low", "default", "high", "urgent")


def enqueue(store, kind, title, body, priority="default", created_at=None):
    """入队一条待发送的推送，返回新条目 id。"""
    if kind not in KINDS:
        raise ValueError(f"未知 kind: {kind}，合法值 {KINDS}")
    if priority not in PRIORITIES:
        raise ValueError(f"未知 priority: {priority}，合法值 {PRIORITIES}")
    ts = created_at or datetime.now().isoformat(timespec="seconds")
    with store.tx() as c:
        cur = c.execute(
            "INSERT INTO outbox (created_at,kind,priority,title,body,attempts) "
            "VALUES (?,?,?,?,?,0)", (ts, kind, priority, title, body))
        return cur.lastrowid


def unsent(store):
    """所有未发送的条目，按入队时间升序。"""
    return store.q("SELECT * FROM outbox WHERE sent_at IS NULL "
                   "ORDER BY created_at, id")


def mark_sent(store, entry_id):
    """标记已发送。重复调用是安全的 —— 补跑竞态下这会真实发生。"""
    with store.tx() as c:
        c.execute("UPDATE outbox SET sent_at=? WHERE id=? AND sent_at IS NULL",
                  (datetime.now().isoformat(timespec="seconds"), entry_id))


def mark_failed(store, entry_id, err):
    """发送失败：attempts +1，记下原因，条目留在队列里等下次重试。"""
    with store.tx() as c:
        c.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                  (str(err)[:500], entry_id))


def has_kind_on(store, kind, d):
    """某天是否入队过某类条目（发没发都算）。失败检测靠这个判断。"""
    r = store.conn.execute(
        "SELECT 1 FROM outbox WHERE kind=? AND substr(created_at,1,10)=? LIMIT 1",
        (kind, d)).fetchone()
    return r is not None


def claim(store, entry_id):
    """
    原子领取一条待发条目。返回 True 表示本次调用领到了，False 表示已被别人领走。

    并发场景：run_daily（过推送点自 drain）和 run_notify（定时 drain）是两个
    不同的 launchd Label，Mac 睡过头唤醒后会同时补跑。靠 UPDATE ... WHERE
    sent_at IS NULL 的 rowcount 判断谁赢，输的一方直接跳过，不会重复推送。
    """
    ts = datetime.now().isoformat(timespec="seconds")
    with store.tx() as c:
        cur = c.execute(
            "UPDATE outbox SET sent_at=? WHERE id=? AND sent_at IS NULL",
            (ts, entry_id))
        return cur.rowcount == 1


def release(store, entry_id):
    """发送失败时归还领取，让下次 drain 能重试。"""
    with store.tx() as c:
        c.execute("UPDATE outbox SET sent_at=NULL WHERE id=?", (entry_id,))
