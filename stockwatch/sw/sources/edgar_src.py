"""SEC EDGAR 适配器：8-K / Form 4 / 13F-HR。"""
import json
from datetime import date, datetime, timedelta

from .base import SourceResult, timed

_ready = False

# 可由应用随数据更新、由测试替换的确定性 CIK→ticker 层。没有映射时仍持久化
# CIK；有映射时全市场 Form 4 可以进入观察池，无需逐公司再发网络请求。
CIK_TICKER_MAP = {}


def _init(email):
    global _ready
    if not _ready:
        from edgar import set_identity
        set_identity(email)
        _ready = True


def _norm_cik(cik):
    raw = str(cik or "").strip()
    return raw.zfill(10) if raw.isdigit() else raw


def resolve_ticker(cik, mapping=None):
    """确定性 CIK 映射；mapping 可注入假实现，不发网络。"""
    source = CIK_TICKER_MAP if mapping is None else mapping
    return str(source.get(_norm_cik(cik), source.get(str(cik or ""), "")) or "").upper().strip()


def _assemble_row(f, form, now, ticker="", ticker_map=None):
    """纯函数：把 filing 对象规整成 edgar_filings 行。"""
    accession = getattr(f, "accession_no", None) or getattr(f, "accession_number", "")
    items = getattr(f, "items", None)
    if isinstance(items, (list, tuple)):
        items = ",".join(str(item) for item in items)
    items = items or ""
    cik = str(getattr(f, "cik", ""))
    mapped = resolve_ticker(cik, ticker_map)
    filing_ticker = getattr(f, "ticker", None) or ""
    return (
        str(accession), str(getattr(f, "filing_date", "")),
        str(ticker or filing_ticker or mapped), cik, form, str(items),
        str(getattr(f, "filing_url", "") or ""),
        json.dumps({"company": str(getattr(f, "company", ""))}, ensure_ascii=False), now,
    )


def _window(as_of=None, days_back=3):
    end = date.fromisoformat(as_of) if as_of else date.today()
    span = max(1, int(days_back)) - 1
    return (end - timedelta(days=span)).isoformat(), end.isoformat()


def _iter_collection_with_status(collection, fallback_limit=400):
    """兼容 edgartools collection、分页迭代器和普通 list。"""
    if collection is None:
        return []
    pages = getattr(collection, "iter_pages", None)
    if callable(pages):
        out = []
        for page in pages():
            out.extend(list(page or []))
        return out, True
    try:
        return list(collection), True
    except TypeError:
        head = getattr(collection, "head", None)
        if callable(head):
            # 只有底层对象没有可迭代/分页接口时才被迫使用 cap，并显式
            # 返回 complete=False，让 source_health 暴露数据可能被截断。
            return list(head(fallback_limit or 400)), False
        raise


def _iter_collection(collection):
    """兼容旧内部调用，返回完整性由 _iter_collection_with_status 提供。"""
    return _iter_collection_with_status(collection)[0]


def _detail(form, fetched, kept, errors):
    bits = [f"{form}:扫描 {fetched} 条，窗口内 {kept} 条"]
    if errors:
        bits.append("失败=" + ",".join(errors[:3]))
    return "；".join(bits)


@timed("edgar.filings")
def fetch_filings(email, forms=("8-K", "4"), days_back=3, limit=400,
                  as_of=None, ticker_map=None):
    """全市场扫描指定日期窗口，完成可用分页后再组装数据。

    ``limit`` 是旧 API 参数，当前实现不会用它在首个 head() 上静默截断；
    collection 能提供完整迭代时全部保留。若底层迭代本身报错，ok=False，
    detail 会带扫描/窗口计数，不能把部分覆盖声称为完整成功。
    """
    _init(email)
    from edgar import get_filings
    now = datetime.now().isoformat(timespec="seconds")
    lo, hi = _window(as_of, days_back)
    rows, errors = [], []
    for form in forms:
        try:
            filings, complete = _iter_collection_with_status(get_filings(form=form), limit)
            kept = 0
            for filing in filings:
                filed = str(getattr(filing, "filing_date", ""))[:10]
                if filed and not (lo <= filed <= hi):
                    continue
                kept += 1
                try:
                    rows.append(_assemble_row(filing, form, now, ticker_map=ticker_map))
                except Exception:
                    errors.append(f"{form}:row")
            if not complete:
                errors.append(f"{form}:使用 cap {limit}，扫描 {len(filings)} 条、窗口内 {kept} 条，扫描不完整")
        except Exception as exc:
            errors.append(f"{form}:{type(exc).__name__}")
    return SourceResult(source="edgar.filings", rows=len(rows), data=rows,
                        ok=not errors,
                        detail=("窗口 " + lo + "~" + hi + "；" + "; ".join(errors[:3]))
                        if errors else f"窗口 {lo}~{hi}，完整扫描 {len(rows)} 条")


@timed("edgar.filings_by_ticker")
def fetch_filings_for_tickers(email, tickers, forms=("8-K", "4"), days_back=7,
                              limit_per=10, as_of=None, ticker_map=None):
    """按持仓查询并先过滤日期窗口，再应用每票上限。"""
    _init(email)
    from edgar import Company
    now = datetime.now().isoformat(timespec="seconds")
    lo, hi = _window(as_of, days_back)
    rows, errs = [], []
    for ticker in tickers:
        ticker = str(ticker).upper().strip()
        if not ticker:
            continue
        try:
            company = Company(ticker)
        except Exception as exc:
            errs.append(f"{ticker}:{type(exc).__name__}")
            continue
        for form in forms:
            try:
                filings, complete = _iter_collection_with_status(
                    company.get_filings(form=form), limit_per)
                in_window = [f for f in filings
                             if (not str(getattr(f, "filing_date", ""))[:10]
                                 or lo <= str(getattr(f, "filing_date", ""))[:10] <= hi)]
                if not complete:
                    errs.append(f"{ticker}/{form}:使用 cap {limit_per}，扫描 {len(filings)} 条、窗口内 {len(in_window)} 条，扫描不完整")
                if len(in_window) > int(limit_per or 0) > 0:
                    errs.append(f"{ticker}/{form}:窗口内 {len(in_window)} 条，截断 {limit_per} 条")
                for filing in in_window[:limit_per if limit_per else None]:
                    try:
                        rows.append(_assemble_row(filing, form, now, ticker=ticker,
                                                  ticker_map=ticker_map))
                    except Exception:
                        errs.append(f"{ticker}/{form}:row")
            except Exception as exc:
                errs.append(f"{ticker}/{form}:{type(exc).__name__}")
    return SourceResult(source="edgar.filings_by_ticker", rows=len(rows), data=rows,
                        ok=not errs, detail="; ".join(errs[:3]),
                        )
