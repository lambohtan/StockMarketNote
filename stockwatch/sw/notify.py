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

# 金额守卫：ntfy 推送正文/标题绝不能出现金额。
# 覆盖四种写法（Task 7 接入 LLM 摘要后，模型很可能把 $ 金额翻译成中文写法，
# 光认 $ 前缀会被直接绕过）：
#   1. $ 前缀：$1,234.56 / $ 1234
#   2. 中文单位：1,234 美元 / 1234美元 / 50 万美元 / 3.2 亿美元 / 50万美金
#   3. USD 前缀：USD 1,234
#   4. USD 后缀：1234 USD
# 刻意不匹配百分比（-8.7%）和 σ 值（2.4σ）—— 这些是推送正文的主要内容，
# 误伤了系统就没法说人话了。
#
# Task 8 修复轮 1：复审实测「50万元人民币」「50万港元」「1234usd」这三种写法
# 未被上面四条覆盖 —— 原因分别是（a）币种单位表里只有 美元/美金，没有本币
# 及其他外币单位；（b）USD 前后缀匹配是大小写敏感的，貼着数字写的小写
# "usd" 会漏判。LLM 摘要来自新闻/8-K 原文，措辞空间比 brief 举的例子宽得多，
# 补齐两类缺口：
#   2'. 币种单位扩容：元 / 人民币 / 港元 / 港币 / 日元 / 欧元 / 英镑，
#       同样支持 万 / 亿 / 千万 量词前缀（如 50万元、3.2亿港元、
#       两千万人民币里的"千万"）。
#       "元"单独放行有误伤风险（元旦/元素/单元都含"元"字），但要求紧跟在
#       数字（可选量词）后面就把风险锁死到"digit + 元"这一种搭配，中文里
#       这个搭配基本只用来记金额（"20元/股"也是金额，不是误伤）。
#   3'/4'. USD/RMB/CNY/HKD 四个 ISO 代码前后缀均改成大小写不敏感
#       （re.IGNORECASE），覆盖 usd/Usd/USD 以及贴着数字写、中间没有空格的
#       "1234usd"。
#
# Task 9 修复轮 1 · Important 2：复审实测「13 万亿美元」「2.5 百万美元」
# 「30 十亿美元」「5 千亿美元」「1.2 万亿元」全部漏判——量词表 _QTY 只有
# 千万/万/亿三个词，不覆盖"十/百/千"与"万/亿"的组合。这不是小概率写法：
# LLM 摘要经常把英文原文的 "$13 trillion"、"$2.5 million" 直接译成中文
# 数量级词，覆盖面比 brief 举的例子宽得多。而且 sw/alerts.py 是
# `from .notify import MONEY_RE as _MONEY` 复用同一份正则——这里补全，
# L1 推送的金额清洗自动受益，不用改 alerts.py。
# 补全为「十/百/千/万/亿」及其两两组合（十万/百万/千万/十亿/百亿/千亿/
# 万亿）——覆盖常见的中文数量级写法。长的组合词排在前面，避免被短的
# 单字（如"千"）先匹配、导致后面"亿美元"这类残余因为不含币种单位而整体
# 匹配失败——不过 Python re 的 `(?:a|b)?` 本身会在整体匹配失败时回溯
# 到下一个候选（乃至跳过量词组匹配空），排前只是让常见情况少走一次
# 回溯，不是正确性的必要条件。
# 必须确认不误伤的场景（都不含"数字紧邻币种"）：裸的百分比/σ/回归指标、
# "60 日""2026 年""第 3 名"这类数字+量词但量词不是币种、"一万亿市值"
# "元宇宙概念股""美元指数走强""单元测试覆盖率"这类含"万亿/元/美元"但前面
# 不是 ASCII 数字的词——`\d` 只匹配阿拉伯数字，"一万亿"的"一"不触发。
_CN_CURRENCY_UNITS = r"(?:人民币|港元|港币|日元|欧元|英镑|美元|美金|元)"
_ISO_CODES = r"(?:USD|RMB|CNY|HKD)"
_QTY = r"(?:十万|百万|千万|十亿|百亿|千亿|万亿|十|百|千|万|亿)?"

MONEY_RE = re.compile(
    r"\$\s*\d[\d,]*(?:\.\d+)?"
    rf"|\d[\d,]*(?:\.\d+)?\s*{_QTY}\s*{_CN_CURRENCY_UNITS}"
    rf"|{_ISO_CODES}\s*\d[\d,]*(?:\.\d+)?"
    rf"|\d[\d,]*(?:\.\d+)?\s*{_ISO_CODES}",
    re.IGNORECASE,
)

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


def drain(store, cfg, dry_run=False, today=None):
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
    today = today or et_today().isoformat()
    failure_reported = False
    failure_preview = None

    if (not OB.has_kind_on(store, "daily", today)
            and not OB.has_kind_on(store, "failure", today)):
        # 第二个条件是同一天不重复上报的关键：只看「今天有没有 daily」的话，
        # 只要今天一直没跑成，drain 每次被调用（补跑、定时）都会再入队一条
        # failure，把手机刷屏。「今天已经报过一次失败」本身也该算数。
        last = store.q("SELECT substr(created_at,1,10) d FROM outbox "
                       "WHERE kind='daily' ORDER BY created_at DESC LIMIT 1")
        last_ok = last[0]["d"] if last else "从未成功过"
        failure_preview = {
            "kind": "failure",
            "priority": "high",
            "title": f"StockWatch {today} 没跑成",
            "body": f"今天的计算任务没有产出日报。\n"
                    f"最后一次成功：{last_ok}\n"
                    f"排查：查看 logs/ 目录，或在菜单栏里点「立即运行一次」。",
        }
        if not dry_run:
            # created_at 的日期部分锚定到 today（而不是 datetime.now()），
            # 否则 has_kind_on(store, "failure", today) 在「今天」是调用方注入的
            # 逻辑日期（测试、或补跑时的美股交易日）而非真实挂钟日期时会找不到
            # 刚入队的这条，导致「同一天不重复上报」失效。
            created_at = today + datetime.now().strftime("T%H:%M:%S")
            OB.enqueue(store, "failure", failure_preview["title"],
                       failure_preview["body"], priority="high",
                       created_at=created_at)
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
            except ValueError as e:
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
