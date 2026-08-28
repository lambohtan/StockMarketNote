#!/usr/bin/env python3
"""outbox 队列测试。运行：python3 tests/test_outbox.py

重点测幂等 —— Mac 睡过头后 launchd 会补跑任务且不保证顺序，
drain 被调用两次是常态，不能因此把同一条推送发两遍。
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw import outbox as OB

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Store(tmp.name)


def test_enqueue_and_unsent():
    print("\n入队与读取")
    st = fresh_store()
    check("空队列", OB.unsent(st), [])
    i1 = OB.enqueue(st, "daily", "标题A", "正文A")
    i2 = OB.enqueue(st, "l1", "标题B", "正文B", priority="urgent")
    rows = OB.unsent(st)
    check("两条未发送", len(rows), 2)
    check("按入队顺序", [r["id"] for r in rows], [i1, i2])
    check("priority 存对了", rows[1]["priority"], "urgent")
    st.close()


def test_idempotent_send():
    print("\n幂等：标记已发后不再出现在未发送队列")
    st = fresh_store()
    i1 = OB.enqueue(st, "daily", "T", "B")
    OB.mark_sent(st, i1)
    check("标记后队列为空", OB.unsent(st), [])
    # 记下第一次 mark_sent 后的时间戳
    first_sent_at = st.q("SELECT sent_at FROM outbox WHERE id=?", (i1,))[0]["sent_at"]
    OB.mark_sent(st, i1)          # 重复标记不能炸
    check("重复标记仍为空", OB.unsent(st), [])
    # 检查时间戳未被改写（真正的幂等性）
    second_sent_at = st.q("SELECT sent_at FROM outbox WHERE id=?", (i1,))[0]["sent_at"]
    check("sent_at 时间戳幂等", first_sent_at, second_sent_at)
    st.close()


def test_mark_failed_keeps_in_queue():
    print("\n失败的条目留在队列里等重试")
    st = fresh_store()
    i1 = OB.enqueue(st, "daily", "T", "B")
    OB.mark_failed(st, i1, "网络超时")
    rows = OB.unsent(st)
    check("仍在未发送队列", len(rows), 1)
    check("attempts 加到 1", rows[0]["attempts"], 1)
    check("记下错误原因", rows[0]["last_error"], "网络超时")
    OB.mark_failed(st, i1, "又超时")
    check("attempts 加到 2", OB.unsent(st)[0]["attempts"], 2)
    st.close()


def test_has_kind_on():
    print("\n某天是否已有某类条目（失败检测要用）")
    st = fresh_store()
    check("还没有 daily", OB.has_kind_on(st, "daily", "2026-08-27"), False)
    OB.enqueue(st, "daily", "T", "B", created_at="2026-08-27T06:05:00")
    check("有了 daily", OB.has_kind_on(st, "daily", "2026-08-27"), True)
    check("换一天没有", OB.has_kind_on(st, "daily", "2026-08-28"), False)
    check("换个 kind 没有", OB.has_kind_on(st, "weekly", "2026-08-27"), False)
    # 已发送的也算「今天有过」—— 否则重复入队失败通知
    i = OB.unsent(st)[0]["id"]
    OB.mark_sent(st, i)
    check("已发送的仍算今天有过", OB.has_kind_on(st, "daily", "2026-08-27"), True)
    st.close()


def test_claim_is_exclusive():
    print("\n并发安全：claim 是原子的，第二次 claim 返回 False")
    st = fresh_store()
    i1 = OB.enqueue(st, "daily", "T", "B")
    check("第一次 claim 成功", OB.claim(st, i1), True)
    check("claim 后队列为空", OB.unsent(st), [])
    check("第二次 claim 失败", OB.claim(st, i1), False)
    st.close()


def test_release_puts_it_back():
    print("\n失败恢复：release 后条目重新进入队列")
    st = fresh_store()
    i1 = OB.enqueue(st, "daily", "T", "B")
    OB.claim(st, i1)
    check("claim 后队列为空", OB.unsent(st), [])
    OB.release(st, i1)
    rows = OB.unsent(st)
    check("release 后条目回队", len(rows), 1)
    check("可以再次 claim", OB.claim(st, i1), True)
    st.close()


def test_claim_does_not_touch_others():
    print("\n claim 只影响目标条目，不影响其他条目")
    st = fresh_store()
    i1 = OB.enqueue(st, "daily", "T1", "B1")
    i2 = OB.enqueue(st, "daily", "T2", "B2")
    OB.claim(st, i1)
    rows = OB.unsent(st)
    check("claim i1 后队列剩 i2", len(rows), 1)
    check("剩下的确是 i2", rows[0]["id"], i2)
    st.close()


if __name__ == "__main__":
    test_enqueue_and_unsent()
    test_idempotent_send()
    test_mark_failed_keeps_in_queue()
    test_has_kind_on()
    test_claim_is_exclusive()
    test_release_puts_it_back()
    test_claim_does_not_touch_others()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
