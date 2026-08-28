"""
推送队列。

「算」和「发」之间的唯一接口：compute 任务只入队，notify 任务只出队。
这样即使计算任务硬崩溃（OOM / 被 kill / 段错误），
独立的 notify 任务照样能发现「今天该有的条目没有」并上报 ——
try/except 抓不到这类失败。

并发安全（claim/release 原语，Task 3 发送逻辑应该这样用）：
  run_daily（跑完发现已过推送点会自 drain）和 run_notify（08:00 定时 drain）
  是两个不同的 launchd Label。Mac 从 06:00 睡到 08:30 唤醒后，launchd 会同时
  补跑两者 —— 并发窗口真实存在，不是理论风险。

  正确用法：
      token = claim(store, entry_id)
      if token is None:
          continue  # 没领到，别人已经在处理或已发送，直接跳过
      try:
          真正发送(entry)
      except Exception as e:
          release(store, entry_id, token)   # 发送失败，退回队列等下次重试
          mark_failed(store, entry_id, e)

  claim() 返回一个「令牌」（写入的时间戳字符串）而不是简单的 True/False，
  是为了让 release() 能校验归属：release 必须带着 claim() 给的那个令牌，
  SQL 层面用 `WHERE sent_at=?` 校验，确保只能归还「自己刚领到、还没发成功」
  的那一条。如果没有这层校验，一次误用（比如发送成功后误调 release，或者对
  自己没 claim 到的 id 调 release）就会把一条已经真实推送成功的条目打回队列，
  下次 drain 再推一遍 —— 这正是 claim/release 本来要根治的重复推送问题，
  只是换了个入口。
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


def mark_sent(store, entry_id, ts=None):
    """标记已发送。重复调用是安全的 —— 补跑竞态下这会真实发生。

    ts：仅供测试注入固定时间戳，用来在不依赖时钟分辨率的前提下断言
    「第二次调用不会覆盖第一次写入的 sent_at」。生产代码不传，走默认的
    当前时间。
    """
    ts = ts or datetime.now().isoformat(timespec="seconds")
    with store.tx() as c:
        c.execute("UPDATE outbox SET sent_at=? WHERE id=? AND sent_at IS NULL",
                  (ts, entry_id))


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
    原子领取一条待发条目。领到返回令牌（写入的时间戳字符串），没领到返回 None。

    并发场景：run_daily（跑完发现已过推送点会自 drain）和 run_notify（08:00 定时）
    是两个不同的 launchd Label，Mac 睡过头唤醒后 launchd 会同时补跑两者。
    靠 UPDATE ... WHERE sent_at IS NULL 的 rowcount 判断谁赢，输的一方拿到 None 直接跳过。

    返回令牌而不是 bool，是为了让 release() 能校验归属 —— 否则 release 可能把
    一条已经真实发送成功的条目打回队列，从另一个入口重新制造重复推送。
    """
    ts = datetime.now().isoformat(timespec="seconds")
    with store.tx() as c:
        cur = c.execute(
            "UPDATE outbox SET sent_at=? WHERE id=? AND sent_at IS NULL",
            (ts, entry_id))
        return ts if cur.rowcount == 1 else None


def release(store, entry_id, token):
    """
    发送失败时归还领取，让下次 drain 能重试。

    token 必须是本次 claim() 返回的那个值。带 token 校验（WHERE sent_at=?）
    确保只能归还自己领到的那一条 —— 对未 claim 的、或已被别人处理的条目调用本函数
    不会有任何效果。返回是否真的归还了。
    """
    with store.tx() as c:
        cur = c.execute(
            "UPDATE outbox SET sent_at=NULL WHERE id=? AND sent_at=?",
            (entry_id, token))
        return cur.rowcount == 1
