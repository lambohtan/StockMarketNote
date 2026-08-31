"""Reddit search fetcher for ticker-specific discussion posts.

Default path is Reddit's public Atom/RSS search feed
(``reddit.com/r/{sub}/search.rss``). The richer JSON search endpoint
(``/search.json``) is reliably WAF-blocked (``HTTP 403``) for public clients
(issue #862), and probing it on every call only doubled our request volume
against Reddit's per-IP rate limit — tripping ``429`` on the RSS fallback — so
it is kept (``_fetch_subreddit_json``) but not used by default. On a 429 we back
off once (honouring ``Retry-After``). RSS lacks score / comment counts, so those
posts are marked and the formatter omits the metrics rather than printing fake
zeros.

No API key required. Returns formatted plaintext blocks ready for prompt
injection and degrades gracefully — returns a placeholder string rather than
raising, so callers never special-case missing data.
"""

from __future__ import annotations

import html
import http.client
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .symbol_utils import crypto_base

logger = logging.getLogger(__name__)

_SEARCH = "https://www.reddit.com/search.rss?{qs}"
_API = "https://www.reddit.com/r/{sub}/search.json?{qs}"
_RSS = "https://www.reddit.com/r/{sub}/search.rss?{qs}"
# A descriptive, identified User-Agent (per Reddit's API etiquette). Reddit
# blocks generic/anonymous tokens like bare "Mozilla/5.0" or "curl/…" but
# serves this one on both endpoints; the RSS feed accepts it even when the
# JSON search endpoint 403s, so no browser-spoofing is needed.
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# Default subreddits ordered roughly by signal density for ticker-specific
# discussion. wallstreetbets has the most volume but most noise; stocks /
# investing trend more measured. Caller can override.
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")

# STOCKWATCH 修改：Reddit 的公开 RSS 按 IP 做突发限流，实测窗口内**只有第一发**
# 能成功——1 秒、8 秒间隔都会 429，25 秒可以恢复。原实现每个 ticker 对三个板块
# 各发一次（间隔 1 秒），必然有两次失败，而失败又被渲染成"没有帖子"。
#
# 改成一次站内搜索用 `subreddit:` 过滤覆盖全部板块：请求量降到 1/3，且实测召回
# 更好（合并查询抓到了 "The under/over valuation of Nvidia/AMD"，分板块查询没抓到）。
# 加上进程内最小间隔作为兜底；单进程串行跑批时每只票间隔远超它，不会真正等待。
_MIN_REQUEST_INTERVAL = 20.0
_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    wait = _MIN_REQUEST_INTERVAL - (time.time() - _last_request_at)
    if wait > 0:
        logger.debug("Reddit throttle: sleeping %.1fs", wait)
        time.sleep(wait)
    _last_request_at = time.time()


def _entry_subreddit(entry) -> str:
    """从 Atom entry 的链接里取出板块名。站内搜索的结果混合了多个板块。"""
    link = entry.find("atom:link", _ATOM_NS)
    href = link.get("href") if link is not None else ""
    if href and "/r/" in href:
        return href.split("/r/", 1)[1].split("/", 1)[0].lower()
    return ""


def _parse_entries(root, limit: int) -> list[dict]:
    posts = []
    for entry in root.findall("atom:entry", _ATOM_NS)[:limit]:
        title_el = entry.find("atom:title", _ATOM_NS)
        published_el = entry.find("atom:published", _ATOM_NS)
        content_el = entry.find("atom:content", _ATOM_NS)
        posts.append({
            "title": (title_el.text if title_el is not None else "") or "",
            "score": None,
            "num_comments": None,
            "created_utc": _iso_to_timestamp(
                published_el.text if published_el is not None else None
            ),
            "selftext": _strip_html(content_el.text if content_el is not None else ""),
            "source": "rss",
            "subreddit": _entry_subreddit(entry),
        })
    return posts


def _fetch_combined(
    ticker: str,
    subreddits: Iterable[str],
    limit_per_sub: int,
    timeout: float,
    _retry: bool = True,
) -> dict[str, list[dict]] | None:
    """一次站内搜索覆盖全部板块，返回 {板块: 帖子列表}；失败返回 ``None``。"""
    subs = list(subreddits)
    query = f"{ticker} (" + " OR ".join(f"subreddit:{s}" for s in subs) + ")"
    qs = urlencode({"q": query, "sort": "new", "t": "week",
                    "limit": max(25, limit_per_sub * len(subs))})
    _throttle()
    try:
        with urlopen(Request(_SEARCH.format(qs=qs), headers={"User-Agent": _UA}),
                     timeout=timeout) as resp:
            root = ET.fromstring(resp.read())
    except HTTPError as exc:
        if exc.code == 429 and _retry:
            wait = _retry_after_seconds(exc) or _MIN_REQUEST_INTERVAL
            logger.warning("Reddit combined search 429 for %s — backing off %.1fs",
                           ticker, wait)
            time.sleep(wait)
            return _fetch_combined(ticker, subs, limit_per_sub, timeout, _retry=False)
        logger.warning("Reddit combined search failed for %s: %s", ticker, exc)
        return None
    except (OSError, http.client.HTTPException, ET.ParseError) as exc:
        logger.warning("Reddit combined search failed for %s: %s", ticker, exc)
        return None

    grouped: dict[str, list[dict]] = {s: [] for s in subs}
    lowered = {s.lower(): s for s in subs}
    for post in _parse_entries(root, 100):
        target = lowered.get(post.get("subreddit", ""))
        if target and len(grouped[target]) < limit_per_sub:
            grouped[target].append(post)
    return grouped


def _search_qs(ticker: str, limit: int) -> str:
    return urlencode({
        "q": ticker,
        "restrict_sr": "on",
        "sort": "new",
        "t": "week",  # last 7 days
        "limit": limit,
    })


def _iso_to_timestamp(iso_str: str | None) -> float | None:
    """Parse an Atom ``published`` timestamp to a UTC epoch, or None."""
    if not iso_str:
        return None
    try:
        normalized = iso_str[:-1] + "+00:00" if iso_str.endswith("Z") else iso_str
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return None


def _strip_html(content: str) -> str:
    """Reduce the HTML body Reddit embeds in an Atom entry to plain text."""
    if not content:
        return ""
    # Reddit wraps the real selftext between SC_OFF / SC_ON markers.
    if "<!-- SC_OFF -->" in content and "<!-- SC_ON -->" in content:
        content = content.split("<!-- SC_OFF -->")[1].split("<!-- SC_ON -->")[0]
    text = re.sub(r"<[^>]+>", " ", content)
    return " ".join(html.unescape(text).split())


def _retry_after_seconds(exc: HTTPError) -> float | None:
    """Seconds to wait from a 429's ``Retry-After`` header, capped at 30s."""
    try:
        val = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
        return min(float(val), 30.0) if val else None
    except (ValueError, TypeError, AttributeError):
        return None


def _fetch_subreddit_rss(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
    _retry: bool = True,
) -> list[dict]:
    """Default path: parse the public Atom search feed for a subreddit.

    Carries no score / comment counts, so those fields are left None and the
    post is tagged ``source="rss"`` for honest display. On a 429 (Reddit's
    per-IP rate limit) we back off once — honouring ``Retry-After`` when
    present — before giving up, so a transient burst doesn't blank the feed.
    """
    url = _RSS.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(url, headers={"User-Agent": _UA})
    try:
        with urlopen(req, timeout=timeout) as resp:
            root = ET.fromstring(resp.read())
    except HTTPError as exc:
        if exc.code == 429 and _retry:
            wait = _retry_after_seconds(exc) or 5.0
            logger.warning(
                "Reddit RSS 429 for r/%s · %s — backing off %.1fs then retrying once",
                sub, ticker, wait,
            )
            time.sleep(wait)
            return _fetch_subreddit_rss(ticker, sub, limit, timeout, _retry=False)
        logger.warning("Reddit RSS fetch failed for r/%s · %s: %s", sub, ticker, exc)
        return None
    except (OSError, http.client.HTTPException, ET.ParseError) as exc:
        # OSError covers URLError/TimeoutError/connection resets; HTTPException
        # covers chunked-transfer errors (IncompleteRead/BadStatusLine, #1024).
        logger.warning("Reddit RSS fetch failed for r/%s · %s: %s", sub, ticker, exc)
        return None

    return _parse_entries(root, limit)


def _fetch_subreddit_json(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> list[dict]:
    """Richer JSON search path (carries score / comment counts).

    Reddit's WAF currently returns ``403 Blocked`` on this endpoint for
    non-OAuth clients (issue #862), so it is NOT used by default — calling it on
    every request only doubled our volume against the per-IP rate limit and
    triggered 429s on the RSS fallback. Kept for the day the WAF relaxes or an
    OAuth token is wired in; degrades to RSS on failure.
    """
    url = _API.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        children = (payload.get("data") or {}).get("children") or []
        return [c.get("data", {}) for c in children if isinstance(c, dict)]
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning(
            "Reddit JSON fetch failed for r/%s · %s: %s — falling back to RSS feed.",
            sub, ticker, exc,
        )
        return _fetch_subreddit_rss(ticker, sub, limit, timeout)


def _fetch_subreddit(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> list[dict] | None:
    """Fetch one subreddit, RSS-first. ``None`` means the fetch failed.

    STOCKWATCH 修改：失败与"确实没有帖子"必须区分。原实现两种情况都返回 ``[]``，
    于是被 429 限流的 subreddit 在 prompt 里被渲染成 "<no posts found>"，情绪分析
    师据此写出"社区无人讨论"——把抓取失败当成了市场事实。

    The JSON search endpoint is reliably WAF-blocked (403) for public clients,
    so we go straight to the RSS feed — which serves our identified User-Agent
    reliably — halving our request volume against Reddit's per-IP rate limit.
    """
    return _fetch_subreddit_rss(ticker, sub, limit, timeout)


def fetch_reddit_posts(
    ticker: str,
    subreddits: Iterable[str] = DEFAULT_SUBREDDITS,
    limit_per_sub: int = 5,
    timeout: float = 10.0,
    inter_request_delay: float = 1.0,
) -> str:
    """Fetch recent Reddit posts mentioning ``ticker`` across finance
    subreddits and return them as a formatted plaintext block.

    ``inter_request_delay`` paces the (now RSS-only) per-subreddit requests to
    stay under Reddit's public per-IP rate limit; combined with the RSS-first
    path it makes 429s rare even when several analyses run back-to-back.
    """
    # Crypto reaches us as a Yahoo pair (BTC-USD); search Reddit for the base
    # ("BTC") so the query actually matches discussion instead of near-nothing.
    ticker = crypto_base(ticker) or ticker
    subreddits = tuple(subreddits)
    blocks = []
    total_posts = 0
    failed_subs: list[str] = []

    # 先试一次合并搜索（1 个请求覆盖全部板块）。成功就不再逐板块请求——那会
    # 触发限流，让大部分板块变成假的"没有帖子"。
    combined = _fetch_combined(ticker, subreddits, limit_per_sub, timeout)

    for i, sub in enumerate(subreddits):
        if combined is not None:
            posts = combined.get(sub, [])
        else:
            if i > 0:
                time.sleep(inter_request_delay)
            posts = _fetch_subreddit(ticker, sub, limit_per_sub, timeout)
        if posts is None:
            failed_subs.append(sub)
            blocks.append(
                f"r/{sub}: <FETCH FAILED — rate-limited or unreachable. "
                f"This is NOT evidence that nobody is discussing {ticker.upper()}; "
                f"treat this source as unavailable and lower your confidence.>")
            continue
        total_posts += len(posts)
        if not posts:
            blocks.append(f"r/{sub}: <no posts found mentioning {ticker.upper()} in the past 7 days>")
            continue

        via_rss = any(p.get("source") == "rss" for p in posts)
        header = f"r/{sub} — {len(posts)} recent posts mentioning {ticker.upper()}"
        header += " (via RSS feed; scores/comments unavailable):" if via_rss else ":"
        lines = [header]
        for p in posts:
            title = (p.get("title") or "").replace("\n", " ").strip()
            score = p.get("score")
            comments = p.get("num_comments")
            created = p.get("created_utc")
            created_str = (
                time.strftime("%Y-%m-%d", time.gmtime(created)) if created else "?"
            )
            # Score / comment counts are absent on the RSS fallback path —
            # show them only when present rather than printing fake zeros.
            meta = created_str
            if score is not None and comments is not None:
                meta += f" · {score:>4}↑ · {comments:>3}c"
            selftext = (p.get("selftext") or "").replace("\n", " ").strip()
            if len(selftext) > 240:
                selftext = selftext[:240] + "…"
            lines.append(
                f"  [{meta}] {title}"
                + (f"\n    body excerpt: {selftext}" if selftext else "")
            )
        blocks.append("\n".join(lines))

    if total_posts == 0:
        # STOCKWATCH 修改：全空时也要说清是"抓取失败"还是"确实没人讨论"。原实现
        # 一律返回 "no posts found"，会让下游把限流当成社区静默的证据。
        if failed_subs:
            reached = [f"r/{s}" for s in subreddits if s not in failed_subs]
            return (
                f"<REDDIT UNAVAILABLE — fetch failed for "
                f"{', '.join(f'r/{s}' for s in failed_subs)} (rate-limited or unreachable)"
                + (f"; {', '.join(reached)} returned no posts" if reached else "")
                + f". This is NOT evidence about how much {ticker.upper()} is being "
                  f"discussed. Treat Reddit as an unavailable source and lower confidence.>"
            )
        return (
            f"<no Reddit posts found mentioning {ticker.upper()} across "
            f"{', '.join(f'r/{s}' for s in subreddits)} in the past 7 days>"
        )
    return "\n\n".join(blocks)
