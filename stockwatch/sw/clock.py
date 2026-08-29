"""与 launchd 墙钟一致的逻辑日期辅助函数。"""
from datetime import datetime


def logical_date(now=None):
    """返回本机墙钟日期；测试可注入任意本地/ET 边界时间。"""
    value = now or datetime.now()
    # 生产调用的 datetime.now() 是本机 naive 墙钟时间；注入 aware 时间时，
    # 先换算到本机时区，避免 ET 午夜被错误地当成本机新的一天。
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.date().isoformat()
