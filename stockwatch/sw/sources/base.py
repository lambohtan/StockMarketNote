import time, traceback
from dataclasses import dataclass, field

@dataclass
class SourceResult:
    """每个数据源统一的返回。ok=False 时上层继续跑其他源，不中断整个任务。"""
    source: str
    ok: bool = True
    rows: int = 0
    latency_ms: int = 0
    detail: str = ""
    data: object = None

def timed(source):
    """装饰器：统一计时、捕获异常、生成 SourceResult。"""
    def deco(fn):
        def wrap(*a, **kw):
            t0 = time.time()
            try:
                r = fn(*a, **kw)
                if not isinstance(r, SourceResult):
                    r = SourceResult(source=source, data=r)
                r.source = source
                r.latency_ms = int((time.time() - t0) * 1000)
                return r
            except Exception as e:
                return SourceResult(
                    source=source, ok=False,
                    latency_ms=int((time.time() - t0) * 1000),
                    detail=f"{type(e).__name__}: {e}")
        return wrap
    return deco
