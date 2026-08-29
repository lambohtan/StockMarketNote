"""
推送队列。

``sent_at`` 只表示远端已确认成功；领取发送权使用独立的短租约
``claim_token``/``claimed_at``。进程在 HTTP 前死亡时，租约到期后条目会
重新可领取，不会把未发送的通知永久标成已发送。
"""
from datetime import datetime, timedelta
from uuid import uuid4

KINDS = ("daily", "weekly", "l1", "failure")
PRIORITIES = ("low", "default", "high", "urgent")
CLAIM_LEASE_SECONDS = 300


def _now(now=None):
    return now or datetime.now()


def _stamp(now=None):
    return _now(now).isoformat(timespec="seconds")


def _logical_date(ts):
    return str(ts)[:10]


def enqueue(store, kind, title, body, priority="default", created_at=None,
            logical_date=None, event_key=None):
    """入队一条待发送推送，返回其 id。

    ``logical_date`` 与 ``event_key`` 是可选的，以兼容旧调用；业务事件应
    显式传入二者，唯一索引会把 daily/L1/failure 的重试变成幂等操作。
    """
    if kind not in KINDS:
        raise ValueError(f"未知 kind: {kind}，合法值 {KINDS}")
    if priority not in PRIORITIES:
        raise ValueError(f"未知 priority: {priority}，合法值 {PRIORITIES}")
    ts = created_at or _stamp()
    logical_date = logical_date or _logical_date(ts)
    with store.tx() as c:
        if event_key is None:
            cur = c.execute(
                "INSERT INTO outbox(created_at,logical_date,event_key,kind,priority,"
                "title,body,attempts) VALUES (?,?,?,?,?,?,?,0)",
                (ts, logical_date, None, kind, priority, title, body))
            return cur.lastrowid
        # INSERT OR IGNORE 后再读取 id，保证并发重复尝试得到同一个业务事件。
        c.execute(
            "INSERT OR IGNORE INTO outbox(created_at,logical_date,event_key,kind,"
            "priority,title,body,attempts) VALUES (?,?,?,?,?,?,?,0)",
            (ts, logical_date, event_key, kind, priority, title, body))
        row = c.execute(
            "SELECT id FROM outbox WHERE logical_date=? AND kind=? AND event_key=?",
            (logical_date, kind, event_key)).fetchone()
        return row[0]


def unsent(store, now=None, lease_seconds=CLAIM_LEASE_SECONDS):
    """返回未发送且没有有效 claim lease 的条目。"""
    if store.has_column("outbox", "claim_token"):
        cutoff = (_now(now) - timedelta(seconds=int(lease_seconds))).isoformat(
            timespec="seconds")
        return store.q(
            "SELECT * FROM outbox WHERE sent_at IS NULL AND "
            "(claim_token IS NULL OR claimed_at IS NULL OR claimed_at<=?) "
            "ORDER BY created_at,id", (cutoff,))
    # 只读打开的旧库可能尚未迁移，仍允许 dry-run 查看 pending rows。
    return store.q("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY created_at,id")


def mark_sent(store, entry_id, ts=None, token=None):
    """发送得到成功确认后标记 sent；无 token 只允许未领取的兼容调用。"""
    sent_at = _stamp() if ts is None else ts
    with store.tx() as c:
        if token is None:
            cur = c.execute(
                "UPDATE outbox SET sent_at=? WHERE id=? AND sent_at IS NULL "
                "AND (claim_token IS NULL OR claim_token='')",
                (sent_at, entry_id))
        else:
            cur = c.execute(
                "UPDATE outbox SET sent_at=?,claim_token=NULL,claimed_at=NULL "
                "WHERE id=? AND sent_at IS NULL AND claim_token=?",
                (sent_at, entry_id, token))
        return cur.rowcount == 1


def mark_failed(store, entry_id, err, token=None):
    """记录失败次数；带 token 时仅当前 lease owner 有权更新。"""
    with store.tx() as c:
        if token is None:
            c.execute(
                "UPDATE outbox SET attempts=attempts+1,last_error=? WHERE id=? "
                "AND sent_at IS NULL AND (claim_token IS NULL OR claim_token='')",
                (str(err)[:500], entry_id))
        else:
            c.execute(
                "UPDATE outbox SET attempts=attempts+1,last_error=? WHERE id=? "
                "AND sent_at IS NULL AND claim_token=?",
                (str(err)[:500], entry_id, token))


def has_kind_on(store, kind, logical_date):
    """按显式 logical_date 判断是否入队过某类事件。"""
    if store.has_column("outbox", "logical_date"):
        r = store.conn.execute(
            "SELECT 1 FROM outbox WHERE kind=? AND logical_date=? LIMIT 1",
            (kind, logical_date)).fetchone()
    else:
        r = store.conn.execute(
            "SELECT 1 FROM outbox WHERE kind=? AND substr(created_at,1,10)=? LIMIT 1",
            (kind, logical_date)).fetchone()
    return r is not None


def claim(store, entry_id, now=None, lease_seconds=CLAIM_LEASE_SECONDS):
    """原子领取条目，返回随机令牌；sent_at 保持 NULL。"""
    if not store.has_column("outbox", "claim_token"):
        return None
    now = _now(now)
    claimed_at = now.isoformat(timespec="seconds")
    cutoff = (now - timedelta(seconds=int(lease_seconds))).isoformat(
        timespec="seconds")
    token = uuid4().hex
    with store.tx() as c:
        cur = c.execute(
            "UPDATE outbox SET claim_token=?,claimed_at=? WHERE id=? "
            "AND sent_at IS NULL AND (claim_token IS NULL OR claimed_at IS NULL "
            "OR claimed_at<=?)",
            (token, claimed_at, entry_id, cutoff))
        return token if cur.rowcount == 1 else None


def release(store, entry_id, token):
    """当前 token 失败后释放 lease；错误 token 不影响任何状态。"""
    with store.tx() as c:
        cur = c.execute(
            "UPDATE outbox SET claim_token=NULL,claimed_at=NULL WHERE id=? "
            "AND sent_at IS NULL AND claim_token=?", (entry_id, token))
        return cur.rowcount == 1
