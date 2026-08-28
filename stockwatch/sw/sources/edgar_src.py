"""SEC EDGAR —— 8-K / Form 4 / 13F。官方源，最可靠。"""
import json
from datetime import datetime, date, timedelta
from .base import SourceResult, timed

# 触发 L1 提醒的 8-K item：会计问题、审计师变更、破产、减值、高管变动、网络安全事件
L1_ITEMS = {"4.02", "4.01", "1.03", "2.06", "5.02", "1.05"}

_ready = False
def _init(email):
    global _ready
    if not _ready:
        from edgar import set_identity
        set_identity(email)
        _ready = True


def _assemble_row(f, form, now, ticker=""):
    """
    把一个 filing 对象组装成入库行。纯函数，不发网络请求，便于测试。

    行结构固定为 (accession, filed_at, ticker, cik, form, items, url, raw_json, seen_at)，
    这样全市场扫描（fetch_filings）和按 ticker 查（fetch_filings_for_tickers）
    可以共用同一个 upsert_many 调用。

    f 只需鸭子类型具备 accession_no/accession_number、filing_date、items、cik、
    filing_url、company 这些属性即可（edgar 库两种查询路径返回的对象都满足）。

    ticker 参数：按 ticker 查询时我们本来就知道 ticker（就是拿它去查的），直接传入；
    全市场扫描时不知道，退回 filing 对象自带的 ticker 属性（通常也是空——
    这正是修复轮 1 发现的问题：全市场索引结构上就不带 ticker）。
    """
    acc = getattr(f, "accession_no", None) or getattr(f, "accession_number", "")
    items = getattr(f, "items", None)
    if isinstance(items, (list, tuple)):
        items_s = ",".join(str(i) for i in items)
    else:
        items_s = items or ""
    return (
        str(acc),
        str(getattr(f, "filing_date", "")),
        ticker or (getattr(f, "ticker", None) or ""),
        str(getattr(f, "cik", "")),
        form, items_s,
        str(getattr(f, "filing_url", "") or ""),
        json.dumps({"company": str(getattr(f, "company", ""))}, ensure_ascii=False),
        now,
    )


@timed("edgar.filings")
def fetch_filings(email, forms=("8-K", "4"), days_back=3, limit=400):
    """拉最近几天的申报。不按 ticker 逐个查（那样几千只全市场逐个查会被限流），
    而是拉全市场最近的，入库后再和你的持仓/股票池做 join。

    注意（修复轮 1 实测确认）：这条路径返回的索引行结构上不带 ticker 和 items，
    只有 cik —— 所以 causes.py 真正依赖的匹配字段要靠 fetch_filings_for_tickers
    补上。这个函数仍然保留，P4 观察池（盯着非持仓的股票池）要用全市场扫描。"""
    _init(email)
    from edgar import get_filings
    now = datetime.now().isoformat(timespec="seconds")
    rows, errs = [], []
    for form in forms:
        try:
            fl = get_filings(form=form).head(limit)
            for f in fl:
                try:
                    rows.append(_assemble_row(f, form, now))
                except Exception:
                    continue
        except Exception as e:
            errs.append(f"{form}:{type(e).__name__}: {e}")
    return SourceResult(source="edgar.filings", rows=len(rows), data=rows,
                        ok=len(rows) > 0, detail="; ".join(errs[:3]))


@timed("edgar.filings_by_ticker")
def fetch_filings_for_tickers(email, tickers, forms=("8-K", "4"), days_back=7, limit_per=10):
    """
    按持仓 ticker 逐个查申报。

    为什么需要这个：全市场的 get_filings() 索引行**不带 ticker 和 items**（只有 cik），
    而 causes.py 要靠 ticker 匹配、靠 items 判定 L1。Company(ticker).get_filings()
    能拿到 items，ticker 则是我们查询时就已知的。

    P1 的注释担心「按 ticker 逐个查会被限流」——那是针对全市场几千只的顾虑。
    持仓只有二三十只，SEC 限速 10 req/s，完全够用。

    单只 ticker 或单个 form 查询失败不能中断其余的（全局约束：数据源失败不中断
    整个任务），失败记入 errs、继续下一个。
    """
    _init(email)
    from edgar import Company
    now = datetime.now().isoformat(timespec="seconds")
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    rows, errs = [], []
    for tk in tickers:
        tk = str(tk).upper().strip()
        if not tk:
            continue
        try:
            c = Company(tk)
        except Exception as e:
            errs.append(f"{tk}:{type(e).__name__}: {e}")
            continue
        for form in forms:
            try:
                fl = c.get_filings(form=form)
                if fl is None:
                    continue
                for f in list(fl)[:limit_per]:
                    try:
                        filed = str(getattr(f, "filing_date", ""))
                        if filed and filed < cutoff:
                            continue    # 只保留窗口内的，避免每天重复入库几年的历史
                        rows.append(_assemble_row(f, form, now, ticker=tk))
                    except Exception:
                        continue
            except Exception as e:
                errs.append(f"{tk}/{form}:{type(e).__name__}: {e}")
    # ok 反映「查询过程本身是否顺利」，不是「今天有没有新申报」——
    # 持仓多数日子本来就没有新的 8-K，这不该被算作数据源故障。
    return SourceResult(source="edgar.filings_by_ticker", rows=len(rows), data=rows,
                        ok=len(errs) == 0, detail="; ".join(errs[:3]))
