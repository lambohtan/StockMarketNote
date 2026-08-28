"""
美股交易日历与时间判定。

存在的理由：计算任务跑在本机 06:00（美股本机时区 06:30 开盘），
理论上不会碰到当日 bar。但盘前交易在某些情况下会让 yfinance 返回当日的半根 K 线，
而且以后谁把计算时间改到盘中，这里就是唯一的防线。

把半天当一天喂进 60 日回归会污染 β 和残差 σ，**且是静默的**。
"""
from datetime import datetime, date, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKET_CLOSE = time(16, 0)          # 常规时段收盘，ET


def now_et():
    return datetime.now(ET)


def et_today():
    return now_et().date()


def is_trading_day(d):
    """周一到周五。⚠️ 不含美股假日表 —— 假日当天没有 bar，
    下游按「没有数据」处理即可，不需要在这里判。"""
    return d.weekday() < 5


def session_closed(d, now=None):
    """d 这个交易日是否已经收盘。过去的日子恒为 True。"""
    now = now or now_et()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    now = now.astimezone(ET)
    if d < now.date():
        return True
    if d > now.date():
        return False
    return now.time() >= MARKET_CLOSE


def drop_incomplete_bars(rows, now=None):
    """
    rows: [(d, ticker, close, volume), ...]，d 是 ISO 日期字符串。
    剔除所有「日期对应的交易日尚未收盘」的行。
    返回 (保留的行, 被剔除的日期升序列表)。
    """
    now = now or now_et()
    kept, dropped = [], set()
    for r in rows:
        d = date.fromisoformat(r[0])
        if session_closed(d, now):
            kept.append(r)
        else:
            dropped.add(r[0])
    return kept, sorted(dropped)
