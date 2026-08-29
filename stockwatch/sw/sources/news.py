"""新闻适配器：把第三方新闻响应归一为 SourceResult。"""
import warnings

from .base import SourceResult, timed


@timed("yfinance.news")
def fetch_news(ticker, max_items=3):
    """读取一只票的新闻；部分结果与失败状态同时保留，供上层标注。"""
    warnings.filterwarnings("ignore")
    import yfinance as yf

    rows, errors = [], []
    try:
        items = yf.Ticker(ticker).news or []
    except Exception as exc:
        return SourceResult(source="yfinance.news", ok=False, rows=0, data=[],
                            detail=f"{type(exc).__name__}")
    for item in items[:max(0, int(max_items))]:
        try:
            content = item.get("content") or item
            title = content.get("title") or ""
            if not title:
                errors.append("missing_title")
                continue
            canonical = content.get("canonicalUrl")
            if isinstance(canonical, dict):
                link = canonical.get("url") or ""
            else:
                link = content.get("link") or ""
            rows.append({"summary": title, "url": link})
        except Exception as exc:
            errors.append(type(exc).__name__)
    detail = "部分新闻解析失败" if errors else ""
    return SourceResult(source="yfinance.news", ok=not errors,
                        rows=len(rows), data=rows, detail=detail)
