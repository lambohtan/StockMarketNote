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

@timed("edgar.filings")
def fetch_filings(email, forms=("8-K", "4"), days_back=3, limit=400):
    """拉最近几天的申报。不按 ticker 逐个查（那样几百次请求会被限流），
    而是拉全市场最近的，入库后再和你的持仓/股票池做 join。"""
    _init(email)
    from edgar import get_filings
    now = datetime.now().isoformat(timespec="seconds")
    rows, errs = [], []
    for form in forms:
        try:
            fl = get_filings(form=form).head(limit)
            for f in fl:
                try:
                    acc = getattr(f, "accession_no", None) or getattr(f, "accession_number", "")
                    items = getattr(f, "items", None)
                    items_s = ",".join(items) if isinstance(items, (list, tuple)) else (items or "")
                    rows.append((
                        str(acc),
                        str(getattr(f, "filing_date", "")),
                        (getattr(f, "ticker", None) or ""),
                        str(getattr(f, "cik", "")),
                        form, items_s,
                        str(getattr(f, "filing_url", "") or ""),
                        json.dumps({"company": str(getattr(f, "company", ""))}, ensure_ascii=False),
                        now))
                except Exception:
                    continue
        except Exception as e:
            errs.append(f"{form}:{type(e).__name__}: {e}")
    return SourceResult(source="edgar.filings", rows=len(rows), data=rows,
                        ok=len(rows) > 0, detail="; ".join(errs[:3]))
