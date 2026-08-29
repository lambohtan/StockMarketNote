#!/usr/bin/env python3
"""推送与 drain 的测试。运行：python3 tests/test_notify.py

⚠️ 全部用 dry_run，绝不真发。

四个重点：
  1. 金额守卫 —— ntfy.sh 是公共服务器，正文里不能出现账户金额
  2. 失败检测 —— 今天没有 daily 条目要主动上报，这是硬崩溃的唯一防线
  3. 补跑竞态 —— notify 先于 compute 执行时不能误报失败
  4. 并发领取 —— run_daily 和 run_notify 两个 launchd Label 睡过头后
     可能同时 drain，drain 必须用 claim/release 原语，绝不能对同一条
     已被领走的条目重复发送
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw import outbox as OB
from sw import notify as NT

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)

def check_raises(name, fn, exc=ValueError):
    try:
        fn()
    except exc:
        print(f"  ✅ {name}: 正确抛出 {exc.__name__}")
        return
    except Exception as e:
        print(f"  ❌ {name}: 抛了 {type(e).__name__}，期望 {exc.__name__}")
    else:
        print(f"  ❌ {name}: 没抛异常")
    FAIL.append(name)


class FakeCfg:
    ntfy_url = "https://ntfy.sh/test-topic"
    ntfy_topic = "test-topic"
    def get(self, k, d=None):
        return d


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Store(tmp.name)


def test_money_guard():
    print("\n金额守卫（ntfy.sh 是公共服务器）")
    check_raises("市值被拦下", lambda: NT.assert_no_money("持仓市值 $9,130"))
    check_raises("小数金额被拦下", lambda: NT.assert_no_money("成本 $440.51"))
    check_raises("带空格的被拦下", lambda: NT.assert_no_money("浮盈 $ 916"))
    # 百分比和 σ 值必须放行 —— 这才是推送的主要内容
    NT.assert_no_money("NVDA -8.7%，个股独立 -9.1%（2.4σ）")
    NT.assert_no_money("科技敞口 65.8%，有效独立赌注 2.15")
    print("  ✅ 百分比与 σ 值正常放行")


def test_money_guard_wider_wordings():
    """
    Task 8 修复轮 1：复审实测「50万元人民币」「50万港元」「1234usd」
    未被原来四条覆盖 —— LLM 摘要的措辞空间比 brief 举的例子宽得多。
    这里把「必须拦」和「必须放行」两组都断言上，防止以后改正则时
    只顾一边。
    """
    print("\n金额守卫：更宽的中文币种 + 大小写不敏感的 ISO 代码")
    # 必须拦下：新增的三种写法 + brief 里原本就该拦的写法
    check_raises("人民币口语写法被拦下", lambda: NT.assert_no_money("成本约 50万元人民币"))
    check_raises("港元被拦下", lambda: NT.assert_no_money("浮盈 50万港元"))
    check_raises("贴数字的小写 usd 被拦下", lambda: NT.assert_no_money("市值 1234usd"))
    check_raises("日元被拦下", lambda: NT.assert_no_money("等值 300万日元"))
    check_raises("欧元被拦下", lambda: NT.assert_no_money("等值 2000欧元"))
    check_raises("英镑被拦下", lambda: NT.assert_no_money("等值 1500英镑"))
    # 「千万」量词必须跟在阿拉伯数字后面（"3千万美元"），跟在中文数字后面
    # （"两千万美元"）是另一个量级的问题——完整中文数字解析（一/二/三/…/
    # 十/百/千/万/亿及其组合）不在本轮修复范围内，brief 只要求
    # 「万/亿/千万 与上述币种的组合」，指的是数字 + 量词的搭配。
    check_raises("千万量词被拦下", lambda: NT.assert_no_money("市值 3千万美元"))
    check_raises("RMB 代码被拦下", lambda: NT.assert_no_money("成本 RMB 5000"))
    check_raises("CNY 后缀小写被拦下", lambda: NT.assert_no_money("金额 5000cny"))
    check_raises("HKD 前缀被拦下", lambda: NT.assert_no_money("市值 HKD 2000"))

    # 必须放行：推送正文的主要内容，不能被新正则误伤
    for text in [
        "NVDA -8.7%，个股独立 -9.1%（2.4σ）",
        "市盈率 24.5",
        "过去 60 日走势偏弱",
        "2026 年财报季",
        "讨论量排名第 3 名",
        "3 家基金本季新建仓",
        "2.4σ",
    ]:
        NT.assert_no_money(text)
    print("  ✅ 百分比 / σ 值 / 市盈率 / 天数 / 年份 / 排名 / 家数 正常放行")


def test_money_guard_compound_magnitude_words():
    """
    Task 9 修复轮 1 · Important 2：复审实测 _QTY 量词组（千万/万/亿）
    同时漏掉「万亿/百万/十亿/千亿」这四个复合量级词——LLM 摘要经常把英文
    原文的 "$13 trillion"、"$2.5 million" 直接译成这类写法，覆盖面比
    Task 8 修复轮 1 处理的那批单字量词宽得多。而且 sw/alerts.py 是
    `from .notify import MONEY_RE` 复用同一份正则，这里补全，L1 推送的
    金额清洗自动受益。

    必须拦下 / 必须放行两组都断言上，防止以后改正则时只顾一边——尤其是
    "一万亿市值""元宇宙概念股""美元指数走强""单元测试覆盖率"这四条，
    它们都含"万亿/元/美元"但不是"数字紧邻币种"的搭配，是最容易被改
    过头的误伤候选。
    """
    print("\n金额守卫：万亿 / 百万 / 十亿 / 千亿 等复合量级词")
    # 必须拦下
    check_raises("万亿美元被拦下", lambda: NT.assert_no_money("分析师称市值达 13 万亿美元"))
    check_raises("百万美元被拦下", lambda: NT.assert_no_money("回购规模 2.5 百万美元"))
    check_raises("十亿美元被拦下", lambda: NT.assert_no_money("投资总额 30 十亿美元"))
    check_raises("千亿美元被拦下", lambda: NT.assert_no_money("估值约 5 千亿美元"))
    check_raises("万亿元被拦下", lambda: NT.assert_no_money("总市值 1.2 万亿元"))

    # 必须放行：推送正文的主要内容，以及"含万亿/元/美元但数字不紧邻币种"
    # 的场景，都不能被新扩的量词组误伤
    for text in [
        "-8.7%", "+6.2%", "2.4σ", "市盈率 24.5", "过去 60 日走势偏弱",
        "2026 年财报季", "讨论量排名第 3 名", "3 家基金本季新建仓",
        "从 78 名升至 19 名", "R² 0.55", "beta 1.6",
        "一万亿市值",       # "一"是中文数字，不是 \d，不该被数字量词规则命中
        "元宇宙概念股",     # "元"前面没有数字
        "美元指数走强",     # "美元"前面没有数字
        "单元测试覆盖率",   # "元"是"单元"的一部分，前面没有数字
    ]:
        NT.assert_no_money(text)
    print("  ✅ 复合量级词正确拦下，推送主要内容与近形词均未被误伤")


def test_drain_sends_and_marks():
    print("\ndrain 发送并标记（claim/release 流程）")
    st, cfg = fresh_store(), FakeCfg()
    OB.enqueue(st, "daily", "标题", "正文 -8.7%", created_at="2026-08-27T06:05:00")
    r = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("发出 1 条", r["sent"], 1)
    check("队列清空", OB.unsent(st), [])
    # 再 drain 一次不能重发（claim 已经写了 sent_at）
    r2 = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("重复 drain 不重发", r2["sent"], 0)
    st.close()


def test_failure_reported_when_no_daily():
    print("\n失败检测：今天没有 daily 条目就要上报")
    st, cfg = fresh_store(), FakeCfg()
    r = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("上报了失败", r["failure_reported"], True)
    check("失败通知被发出", r["sent"], 1)
    kinds = [x["kind"] for x in st.q("SELECT kind FROM outbox")]
    check("入队的是 failure", kinds, ["failure"])
    # 同一天再 drain 不能重复上报
    r2 = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("同一天不重复上报", r2["failure_reported"], False)
    st.close()


def test_no_false_failure_when_daily_exists():
    print("\n补跑竞态：有 daily 条目时绝不误报失败")
    st, cfg = fresh_store(), FakeCfg()
    OB.enqueue(st, "daily", "T", "B -1.2%", created_at="2026-08-27T06:05:00")
    r = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("没有误报", r["failure_reported"], False)
    # 已发送过的 daily 也不该触发失败上报
    r2 = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("已发送后仍不误报", r2["failure_reported"], False)
    st.close()


def test_money_in_queue_is_rejected_not_sent():
    print("\n队列里混进金额时：拒发并记错，不能静默发出去")
    st, cfg = fresh_store(), FakeCfg()
    OB.enqueue(st, "daily", "T", "你的持仓市值 $9,130",
               created_at="2026-08-27T06:05:00")
    r = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("没有发出", r["sent"], 0)
    check("计为失败", r["failed"], 1)
    rows = OB.unsent(st)
    check("仍在队列里", len(rows), 1)
    check("记下了原因", "金额" in (rows[0]["last_error"] or ""), True)
    st.close()


def test_claimed_entry_is_skipped():
    print("\n并发竞态：drain 拿到的列表里某条已被另一个进程 claim 走，必须跳过不发")
    # 场景还原：run_daily 和 run_notify 是两个 launchd Label，Mac 睡过头唤醒后
    # 可能同时 drain。两边各自的 unsent() 都可能拿到同一条「看起来还没发」的记录，
    # 但只有先调用 claim() 的一方能真的拿到令牌，另一方必须直接跳过。
    st, cfg = fresh_store(), FakeCfg()
    i1 = OB.enqueue(st, "daily", "T", "B -1.0%", created_at="2026-08-27T06:05:00")
    # drain 内部会自己调 unsent()，为了在单线程测试里制造出「列表是过时快照」的
    # 竞态窗口，这里先拍下这一刻的 unsent() 快照（此时条目确实还没被任何人 claim），
    # 再模拟另一个进程抢先真正 claim 并发送成功，最后让 drain 用到这份过时快照。
    stale_snapshot = OB.unsent(st)
    token = OB.claim(st, i1)
    check("模拟的另一进程抢先 claim 成功", token is not None, True)

    real_unsent = OB.unsent
    NT.OB.unsent = lambda store: stale_snapshot
    try:
        r = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    finally:
        NT.OB.unsent = real_unsent

    check("没有重复发送", r["sent"], 0)
    check("也没有误计为失败", r["failed"], 0)
    st.close()


if __name__ == "__main__":
    test_money_guard()
    test_money_guard_wider_wordings()
    test_money_guard_compound_magnitude_words()
    test_drain_sends_and_marks()
    test_failure_reported_when_no_daily()
    test_no_false_failure_when_daily_exists()
    test_money_in_queue_is_rejected_not_sent()
    test_claimed_entry_is_skipped()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
