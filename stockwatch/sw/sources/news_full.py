"""新闻正文抓取。

`news.py` 只取标题（日报够用），深读需要正文才能让 LLM 说出实质内容。
但相当一部分新闻站会挡爬虫或要 JS 渲染 —— 取不到就**如实降级成标题**并
标 ``full=False``，绝不能让下游以为自己读了全文。
"""
import re

from .base import SourceResult, timed
from .news import fetch_news

# 少于这个长度基本是订阅墙提示语或空壳页，不当成正文。
MIN_BODY_CHARS = 200
MAX_BODY_CHARS = 8000
UA = "StockWatch/1.0 (personal research tool)"

_SCRIPT = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t ]+")


def html_to_text(html):
    """粗暴但够用的正文提取：剥脚本样式与标签，压空白。"""
    s = _SCRIPT.sub(" ", html or "")
    s = _TAG.sub(" ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
    s = _WS.sub(" ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _default_opener(url, timeout=10):
    import requests
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r.text


@timed("news.full")
def fetch(ticker, max_items=4, opener=None, headlines=None, timeout=10):
    """取一只票的新闻正文。单条失败只影响那一条，整体仍然 ok。"""
    get_headlines = headlines or (
        lambda tk, n: (fetch_news(tk, n).data or []))
    items = get_headlines(ticker, max_items) or []
    open_url = opener or _default_opener

    rows, degraded = [], 0
    for item in items[:max(0, int(max_items))]:
        title = item.get("summary") or ""
        url = item.get("url") or ""
        text, full = title, False
        if url:
            try:
                body = html_to_text(open_url(url, timeout=timeout))
                if len(body) >= MIN_BODY_CHARS:
                    text, full = body[:MAX_BODY_CHARS], True
            except Exception:
                pass                      # 单条失败就降级，不影响其他条
        if not full:
            degraded += 1
        rows.append({"title": title, "url": url, "text": text, "full": full})

    detail = f"{degraded}/{len(rows)} 条只拿到标题" if degraded else ""
    return SourceResult(source="news.full", ok=True, rows=len(rows),
                        data=rows, detail=detail)
