"""Reddit RSS prefetch pipeline for scheduled StockWatch runs.

This module deliberately stops at the evidence-cache boundary.  A scheduler
can call :mod:`prefetch_reddit` with the day's stock pool, let this process
pace Reddit requests over time, and later let the Trading Agent read the
result with :func:`load_cached_reddit`.  Loading the cache never performs a
network request.

Only public Atom/RSS feeds are used.  Reddit does not provide scores or total
comment counts in those feeds, so the cache records sampled comments and must
not present them as engagement-weighted community consensus.
"""

from __future__ import annotations

import fcntl
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_PATH = REPO_ROOT / "local-data" / "tradingagents" / "reddit-prefetch.sqlite3"
DEFAULT_RATE_LOCK = REPO_ROOT / "local-data" / "tradingagents" / ".reddit-rate-limit.lock"
DEFAULT_RATE_STATE = REPO_ROOT / "local-data" / "tradingagents" / "reddit-rate-limit.json"
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")

_SEARCH_URL = "https://www.reddit.com/r/{subreddit}/search.rss?{query}"
_USER_AGENT = "stockwatch-reddit-prefetch/1.0 (+https://github.com/TauricResearch/TradingAgents)"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_TICKER_RE = re.compile(r"^[A-Za-z0-9._\-^=+]{1,32}$")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9$%._-]+")
_POST_ID_RE = re.compile(r"/comments/([a-z0-9]+)/", re.IGNORECASE)

_FINANCE_TERMS = {
    "revenue", "earnings", "profit", "margin", "guidance", "valuation",
    "multiple", "cashflow", "cash", "debt", "demand", "supply", "growth",
    "decline", "competition", "market share", "forecast", "estimate", "eps",
    "capex", "opex", "dividend", "buyback", "dilution", "orders", "backlog",
    "营收", "利润", "毛利", "指引", "估值", "现金流", "债务", "需求", "竞争",
}
_CAUSAL_TERMS = {
    "because", "therefore", "due to", "which means", "resulting", "driven by",
    "原因", "因为", "所以", "意味着", "导致",
}
_BOT_NAMES = {"automoderator", "visualmod", "wsbmod"}
_EMPTY_BODIES = {"[deleted]", "[removed]", "deleted", "removed"}


class RedditPrefetchError(RuntimeError):
    """Base error for explicit, visible prefetch failures."""


class PoolInputError(RedditPrefetchError):
    """The scheduler supplied an invalid or empty stock pool."""


class RedditFetchError(RedditPrefetchError):
    """A Reddit RSS request failed after the bounded retry policy."""


@dataclass(frozen=True)
class PoolItem:
    ticker: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class RedditPost:
    post_id: str
    title: str
    subreddit: str
    permalink: str
    body: str
    published_at: str | None
    source_rank: int = 0


@dataclass(frozen=True)
class RedditComment:
    comment_id: str
    post_id: str
    post_title: str
    subreddit: str
    author: str
    body: str
    permalink: str
    published_at: str | None
    source_rank: int = 0
    quality_score: float = 0.0


@dataclass
class TickerResult:
    ticker: str
    status: str
    posts_seen: int = 0
    posts_fetched: int = 0
    comments_seen: int = 0
    comments_selected: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class PrefetchReport:
    run_id: str
    analysis_date: str
    as_of: str
    started_at: str
    finished_at: str
    results: list[TickerResult]

    @property
    def has_degradation(self) -> bool:
        return any(result.status in {"failed", "partial", "limited"} for result in self.results)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "analysis_date": self.analysis_date,
            "as_of": self.as_of,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "has_degradation": self.has_degradation,
            "results": [asdict(result) for result in self.results],
        }


@dataclass(frozen=True)
class CachedRedditEvidence:
    ticker: str
    as_of_date: str
    status: str
    fresh: bool
    fetched_at: str | None
    text: str
    payload: dict | None


@dataclass(frozen=True)
class SearchOutcome:
    posts: tuple[RedditPost, ...]
    errors: tuple[str, ...] = ()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_pool_item(raw: str | dict) -> PoolItem:
    if isinstance(raw, str):
        ticker = raw
        aliases: Sequence[str] = ()
    elif isinstance(raw, dict):
        ticker = str(raw.get("ticker") or raw.get("symbol") or "")
        aliases_raw = raw.get("aliases") or raw.get("names") or ()
        if isinstance(aliases_raw, str):
            aliases = (aliases_raw,)
        elif isinstance(aliases_raw, Sequence):
            aliases = tuple(str(alias) for alias in aliases_raw)
        else:
            raise PoolInputError(f"invalid aliases for {ticker or '<missing ticker>'}")
    else:
        raise PoolInputError(f"pool entry must be a ticker string or object, got {type(raw).__name__}")

    ticker = ticker.strip().upper()
    if not _TICKER_RE.fullmatch(ticker) or ticker in {".", ".."}:
        raise PoolInputError(f"invalid ticker: {ticker!r}")
    cleaned_aliases = tuple(
        dict.fromkeys(alias.strip() for alias in aliases if alias and alias.strip())
    )
    if any(len(alias) > 80 for alias in cleaned_aliases):
        raise PoolInputError(f"alias exceeds 80 characters for {ticker}")
    return PoolItem(ticker=ticker, aliases=cleaned_aliases)


def parse_pool_payload(payload: object) -> list[PoolItem]:
    """Parse the scheduler's JSON-compatible stock-pool payload.

    Accepted shapes are ``["AMD", ...]`` and
    ``{"tickers": [{"ticker": "NVDA", "aliases": ["Nvidia"]}, ...]}``.
    Duplicate tickers are merged in stable input order.
    """

    if isinstance(payload, dict):
        entries = payload.get("tickers", payload.get("stocks"))
    else:
        entries = payload
    if not isinstance(entries, list):
        raise PoolInputError("pool JSON must be a list or an object containing 'tickers'")

    merged: dict[str, PoolItem] = {}
    for raw in entries:
        item = _normalize_pool_item(raw)
        previous = merged.get(item.ticker)
        if previous:
            aliases = tuple(dict.fromkeys((*previous.aliases, *item.aliases)))
            merged[item.ticker] = PoolItem(item.ticker, aliases)
        else:
            merged[item.ticker] = item
    if not merged:
        raise PoolInputError("stock pool is empty")
    return list(merged.values())


def load_pool(pool_file: Path | None, tickers: Sequence[str] = ()) -> list[PoolItem]:
    entries: list[str | dict] = list(tickers)
    if pool_file:
        try:
            raw_text = pool_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise PoolInputError(f"cannot read pool file {pool_file}: {exc}") from exc
        if pool_file.suffix.lower() == ".json" or raw_text.lstrip().startswith(("[", "{")):
            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise PoolInputError(f"invalid JSON in {pool_file}: {exc}") from exc
            file_items = parse_pool_payload(payload)
            entries.extend({"ticker": item.ticker, "aliases": list(item.aliases)} for item in file_items)
        else:
            for line in raw_text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    entries.extend(part for part in re.split(r"[\s,]+", line) if part)
    return parse_pool_payload(entries)


def _strip_html(content: str | None) -> str:
    if not content:
        return ""
    text = content
    if "<!-- SC_OFF -->" in text and "<!-- SC_ON -->" in text:
        text = text.split("<!-- SC_OFF -->", 1)[1].split("<!-- SC_ON -->", 1)[0]
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def _entry_text(entry: ET.Element, tag: str) -> str:
    element = entry.find(f"atom:{tag}", _ATOM_NS)
    return ((element.text if element is not None else "") or "").strip()


def _entry_link(entry: ET.Element) -> str:
    for element in entry.findall("atom:link", _ATOM_NS):
        href = element.get("href")
        if href:
            return href
    return ""


def _entry_subreddit(entry: ET.Element, permalink: str) -> str:
    category = entry.find("atom:category", _ATOM_NS)
    if category is not None and category.get("term") not in {None, "multi"}:
        return str(category.get("term")).lower()
    match = re.search(r"/r/([^/]+)/", permalink, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _is_reddit_permalink(permalink: str) -> bool:
    parsed = urlparse(permalink)
    return parsed.scheme == "https" and parsed.hostname in {
        "reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com",
    }


def parse_search_atom(xml_bytes: bytes) -> list[RedditPost]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise RedditFetchError(f"malformed Reddit search RSS: {exc}") from exc

    posts: list[RedditPost] = []
    for rank, entry in enumerate(root.findall("atom:entry", _ATOM_NS)):
        entry_id = _entry_text(entry, "id")
        permalink = _entry_link(entry)
        match = _POST_ID_RE.search(permalink)
        post_id = entry_id.removeprefix("t3_") if entry_id.startswith("t3_") else ""
        post_id = post_id or (match.group(1).lower() if match else "")
        if not post_id or not _is_reddit_permalink(permalink):
            continue
        posts.append(RedditPost(
            post_id=post_id,
            title=_entry_text(entry, "title"),
            subreddit=_entry_subreddit(entry, permalink),
            permalink=permalink,
            body=_strip_html(_entry_text(entry, "content")),
            published_at=_entry_text(entry, "published") or _entry_text(entry, "updated") or None,
            source_rank=rank,
        ))
    return posts


def parse_comment_atom(xml_bytes: bytes, post: RedditPost) -> list[RedditComment]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise RedditFetchError(f"malformed Reddit comment RSS for {post.post_id}: {exc}") from exc

    comments: list[RedditComment] = []
    for rank, entry in enumerate(root.findall("atom:entry", _ATOM_NS)):
        entry_id = _entry_text(entry, "id")
        if not entry_id.startswith("t1_"):
            continue
        author_el = entry.find("atom:author/atom:name", _ATOM_NS)
        author = ((author_el.text if author_el is not None else "") or "").strip()
        comments.append(RedditComment(
            comment_id=entry_id.removeprefix("t1_"),
            post_id=post.post_id,
            post_title=post.title,
            subreddit=post.subreddit,
            author=author.removeprefix("/u/"),
            body=_strip_html(_entry_text(entry, "content")),
            permalink=_entry_link(entry),
            published_at=_entry_text(entry, "published") or _entry_text(entry, "updated") or None,
            source_rank=rank,
        ))
    return comments


def _term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)


def _mentions(text: str, item: PoolItem) -> int:
    score = 0
    if re.search(rf"(?<![A-Za-z0-9])\${re.escape(item.ticker)}(?![A-Za-z0-9])", text, re.I):
        score += 8
    if len(item.ticker) > 2 and _term_pattern(item.ticker).search(text):
        score += 5
    for alias in item.aliases:
        if _term_pattern(alias).search(text):
            score += 5
    return score


def select_posts(
    posts: Sequence[RedditPost],
    item: PoolItem,
    *,
    as_of: datetime,
    window_hours: int,
    max_posts: int,
) -> list[RedditPost]:
    cutoff = as_of - timedelta(hours=window_hours)
    unique: dict[str, RedditPost] = {}
    for post in posts:
        published = _parse_datetime(post.published_at)
        if published and not (cutoff <= published <= as_of + timedelta(minutes=5)):
            continue
        unique.setdefault(post.post_id, post)

    def sort_key(post: RedditPost) -> tuple[float, float, int]:
        relevance = _mentions(post.title, item) * 2 + _mentions(post.body, item)
        published = _parse_datetime(post.published_at)
        timestamp = published.timestamp() if published else 0.0
        return relevance, timestamp, -post.source_rank

    ranked = sorted(unique.values(), key=sort_key, reverse=True)
    if max_posts <= 0:
        return []

    per_sub_cap = max(1, math.ceil(max_posts / max(1, len(DEFAULT_SUBREDDITS))))
    chosen: list[RedditPost] = []
    sub_counts: dict[str, int] = defaultdict(int)
    for post in ranked:
        if sub_counts[post.subreddit] >= per_sub_cap:
            continue
        chosen.append(post)
        sub_counts[post.subreddit] += 1
        if len(chosen) == max_posts:
            return chosen
    for post in ranked:
        if post not in chosen:
            chosen.append(post)
        if len(chosen) == max_posts:
            break
    return chosen


def _normalized_body(body: str) -> str:
    body = _URL_RE.sub(" ", body.lower())
    return " ".join(_TOKEN_RE.findall(body))


def _tokens(body: str) -> set[str]:
    return set(_normalized_body(body).split())


def _near_duplicate(left: str, right: str, threshold: float = 0.85) -> bool:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens) >= threshold


def _comment_quality(comment: RedditComment, item: PoolItem) -> float:
    body = comment.body
    lowered = body.lower()
    score = float(_mentions(body, item))
    score += min(3.0, len(body) / 160.0)
    if re.search(r"(?:\$|\b)\d+(?:\.\d+)?%?", body):
        score += 2
    score += min(4, sum(1 for term in _FINANCE_TERMS if term in lowered))
    if any(term in lowered for term in _CAUSAL_TERMS):
        score += 2
    if len(body) < 40:
        score -= 2
    letters = [char for char in body if char.isalpha()]
    if letters and sum(char.isupper() for char in letters) / len(letters) > 0.8:
        score -= 2
    score += max(0.0, 1.0 - comment.source_rank / 100.0)
    return round(score, 3)


def _eligible_comment(comment: RedditComment) -> bool:
    body = comment.body.strip()
    normalized = _normalized_body(body)
    if not normalized or normalized in _EMPTY_BODIES or len(normalized) < 20:
        return False
    if comment.author.lower() in _BOT_NAMES or comment.author.lower().endswith("bot"):
        return False
    if not any(char.isalpha() or char.isdigit() for char in body):
        return False
    return True


def select_comments(
    comments_by_post: dict[str, Sequence[RedditComment]],
    item: PoolItem,
    *,
    max_total: int,
    max_per_author: int = 2,
) -> list[RedditComment]:
    """Deterministically select informative, diverse comments without an LLM."""

    queues: dict[str, list[RedditComment]] = {}
    seen_exact: set[str] = set()
    accepted_bodies: list[str] = []
    for post_id, comments in comments_by_post.items():
        candidates: list[RedditComment] = []
        for comment in comments:
            if not _eligible_comment(comment):
                continue
            normalized = _normalized_body(comment.body)
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if digest in seen_exact or any(_near_duplicate(comment.body, body) for body in accepted_bodies):
                continue
            seen_exact.add(digest)
            accepted_bodies.append(comment.body)
            candidates.append(RedditComment(
                **{**asdict(comment), "quality_score": _comment_quality(comment, item)}
            ))
        queues[post_id] = sorted(
            candidates,
            key=lambda comment: (comment.quality_score, -comment.source_rank, comment.comment_id),
            reverse=True,
        )

    selected: list[RedditComment] = []
    author_counts: dict[str, int] = defaultdict(int)
    post_ids = [post_id for post_id, queue in queues.items() if queue]
    while post_ids and len(selected) < max_total:
        made_progress = False
        for post_id in list(post_ids):
            queue = queues[post_id]
            while queue:
                candidate = queue.pop(0)
                author_key = candidate.author.lower() or f"anonymous:{candidate.comment_id}"
                if author_counts[author_key] >= max_per_author:
                    continue
                selected.append(candidate)
                author_counts[author_key] += 1
                made_progress = True
                break
            if not queue:
                post_ids.remove(post_id)
            if len(selected) >= max_total:
                break
        if not made_progress:
            break
    return selected


def _comments_in_window(
    comments: Sequence[RedditComment], *, as_of: datetime, window_hours: int
) -> list[RedditComment]:
    cutoff = as_of - timedelta(hours=window_hours)
    kept: list[RedditComment] = []
    for comment in comments:
        published = _parse_datetime(comment.published_at)
        if published is not None and cutoff <= published <= as_of:
            kept.append(comment)
    return kept


def _header_float(headers, name: str) -> float | None:
    try:
        value = headers.get(name) if headers is not None else None
        return float(value) if value not in {None, ""} else None
    except (TypeError, ValueError, AttributeError):
        return None


class RedditRSSClient:
    """RSS client with a persistent, cross-process request gate."""

    def __init__(
        self,
        *,
        min_interval_seconds: float = 30.0,
        timeout: float = 20.0,
        lock_path: Path = DEFAULT_RATE_LOCK,
        state_path: Path = DEFAULT_RATE_STATE,
        opener: Callable = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be non-negative")
        self.min_interval_seconds = min_interval_seconds
        self.timeout = timeout
        self.lock_path = Path(lock_path)
        self.state_path = Path(state_path)
        self.opener = opener
        self.sleeper = sleeper
        self.clock = clock

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.state_path.parent, delete=False
        ) as handle:
            json.dump(state, handle, sort_keys=True)
            temp_name = handle.name
        os.replace(temp_name, self.state_path)

    def _delay_from_headers(self, headers) -> float:
        retry_after = _header_float(headers, "Retry-After")
        if retry_after is None and headers is not None:
            try:
                raw_retry_after = headers.get("Retry-After")
                retry_at = parsedate_to_datetime(raw_retry_after) if raw_retry_after else None
                if retry_at is not None:
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    retry_after = max(0.0, retry_at.timestamp() - self.clock())
            except (TypeError, ValueError, OverflowError):
                retry_after = None
        retry_after = retry_after or 0.0
        reset = _header_float(headers, "X-Ratelimit-Reset") or 0.0
        remaining = _header_float(headers, "X-Ratelimit-Remaining")
        reported = max(retry_after, reset + 2.0 if remaining is not None and remaining <= 0 else 0.0)
        return max(self.min_interval_seconds, reported)

    def get(self, url: str, *, retry_429: bool = True) -> bytes:
        attempts = 2 if retry_429 else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                state = self._read_state()
                wait = max(0.0, float(state.get("next_request_at", 0.0)) - self.clock())
                if wait:
                    self.sleeper(wait)
                request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/atom+xml"})
                try:
                    with self.opener(request, timeout=self.timeout) as response:
                        body = response.read()
                        delay = self._delay_from_headers(getattr(response, "headers", None))
                    self._write_state({
                        "next_request_at": self.clock() + delay,
                        "last_status": 200,
                        "updated_at": self.clock(),
                    })
                    return body
                except HTTPError as exc:
                    delay = self._delay_from_headers(getattr(exc, "headers", None))
                    self._write_state({
                        "next_request_at": self.clock() + delay,
                        "last_status": exc.code,
                        "updated_at": self.clock(),
                    })
                    last_error = exc
                    if exc.code != 429 or attempt + 1 >= attempts:
                        break
                except (URLError, OSError) as exc:
                    self._write_state({
                        "next_request_at": self.clock() + self.min_interval_seconds,
                        "last_status": "network_error",
                        "updated_at": self.clock(),
                    })
                    last_error = exc
                    break
                finally:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        raise RedditFetchError(f"Reddit RSS request failed for {url}: {last_error}")

    def search_posts(
        self,
        item: PoolItem,
        *,
        subreddits: Sequence[str],
        window_hours: int,
        limit: int = 100,
    ) -> SearchOutcome:
        # Site-wide and multi-subreddit RSS searches currently return valid but
        # empty feeds for queries that work on the individual subreddit path.
        # Search each configured subreddit sequentially so an empty result is
        # meaningful; the persistent request gate keeps this overnight-safe.
        # Ask for a week, then enforce the exact ``window_hours`` cutoff locally
        # in ``select_posts``; Reddit's ``t=day`` RSS is intermittently empty.
        search_terms = [item.ticker, *item.aliases[:3]]
        search_query = " OR ".join(
            f'"{term}"' if any(char.isspace() for char in term) else term
            for term in search_terms
        )
        qs = urlencode({
            "q": search_query,
            "restrict_sr": "on",
            "sort": "new",
            "t": "week",
            "limit": min(limit, 100),
        })
        posts: list[RedditPost] = []
        errors: list[str] = []
        for subreddit in subreddits:
            url = _SEARCH_URL.format(subreddit=subreddit, query=qs)
            try:
                posts.extend(parse_search_atom(self.get(url)))
            except RedditFetchError as exc:
                errors.append(f"r/{subreddit}: {exc}")
        return SearchOutcome(tuple(posts), tuple(errors))

    def fetch_comments(self, post: RedditPost, *, limit: int = 100) -> list[RedditComment]:
        safe_limit = max(1, min(limit, 500))
        url = post.permalink.rstrip("/") + f"/.rss?limit={safe_limit}"
        return parse_comment_atom(self.get(url), post)


class RedditCache:
    """SQLite cache with atomic per-ticker snapshots and reusable thread data."""

    SCHEMA_VERSION = 2

    def __init__(self, path: Path = DEFAULT_CACHE_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def session(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.session() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > self.SCHEMA_VERSION:
                raise RedditPrefetchError(
                    f"Reddit cache schema {current_version} is newer than supported "
                    f"version {self.SCHEMA_VERSION}"
                )
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS prefetch_runs (
                    run_id TEXT PRIMARY KEY,
                    as_of TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS prefetch_items (
                    run_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    status TEXT NOT NULL,
                    posts_seen INTEGER NOT NULL,
                    posts_fetched INTEGER NOT NULL,
                    comments_seen INTEGER NOT NULL,
                    comments_selected INTEGER NOT NULL,
                    error TEXT,
                    PRIMARY KEY (run_id, ticker)
                );
                CREATE TABLE IF NOT EXISTS thread_cache (
                    post_id TEXT PRIMARY KEY,
                    permalink TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ticker_snapshots (
                    ticker TEXT NOT NULL,
                    as_of_date TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    posts_seen INTEGER NOT NULL,
                    comments_seen INTEGER NOT NULL,
                    comments_selected INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (ticker, as_of_date)
                );
                CREATE TABLE IF NOT EXISTS raw_snapshots (
                    ticker TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    posts_stored INTEGER NOT NULL,
                    comments_stored INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (ticker, analysis_date, query_hash)
                );
            """)
            connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    def begin_run(self, run_id: str, as_of: str, started_at: str) -> None:
        with self.session() as connection:
            connection.execute(
                "INSERT INTO prefetch_runs(run_id, as_of, started_at, status) VALUES (?, ?, ?, 'running')",
                (run_id, as_of, started_at),
            )

    def finish_run(self, run_id: str, finished_at: str, status: str, error: str | None = None) -> None:
        with self.session() as connection:
            connection.execute(
                "UPDATE prefetch_runs SET finished_at = ?, status = ?, error = ? WHERE run_id = ?",
                (finished_at, status, error, run_id),
            )

    def record_item(self, run_id: str, result: TickerResult) -> None:
        with self.session() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO prefetch_items(
                       run_id, ticker, status, posts_seen, posts_fetched,
                       comments_seen, comments_selected, error
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, result.ticker, result.status, result.posts_seen,
                    result.posts_fetched, result.comments_seen,
                    result.comments_selected, " | ".join(result.errors) or None,
                ),
            )

    def get_thread(
        self,
        post_id: str,
        *,
        max_age_hours: float,
        now: datetime,
        requested_limit: int,
    ) -> list[RedditComment] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT fetched_at, payload_json FROM thread_cache WHERE post_id = ?", (post_id,)
            ).fetchone()
        if not row:
            return None
        fetched_at = _parse_datetime(row["fetched_at"])
        if not fetched_at or now - fetched_at > timedelta(hours=max_age_hours):
            return None
        payload = json.loads(row["payload_json"])
        if int(payload.get("requested_limit") or 0) < requested_limit:
            return None
        return [RedditComment(**comment) for comment in payload.get("comments", [])]

    def put_thread(
        self,
        post: RedditPost,
        comments: Sequence[RedditComment],
        fetched_at: datetime,
        *,
        requested_limit: int,
    ) -> None:
        payload = {
            "post": asdict(post),
            "requested_limit": requested_limit,
            "comments": [asdict(comment) for comment in comments],
        }
        with self.session() as connection:
            connection.execute(
                """INSERT INTO thread_cache(post_id, permalink, fetched_at, payload_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(post_id) DO UPDATE SET
                       permalink = excluded.permalink,
                       fetched_at = excluded.fetched_at,
                       payload_json = excluded.payload_json""",
                (post.post_id, post.permalink, _iso(fetched_at), json.dumps(payload, ensure_ascii=False)),
            )

    def put_snapshot(
        self, item: PoolItem, analysis_date: str, status: str, payload: dict
    ) -> bool:
        """Store a daily snapshot without replacing good data with degradation."""

        fetched_at = str(payload["fetched_at"])
        stats = payload["stats"]
        with self.session() as connection:
            existing = connection.execute(
                "SELECT status FROM ticker_snapshots WHERE ticker = ? AND as_of_date = ?",
                (item.ticker, analysis_date),
            ).fetchone()
            if (
                existing
                and existing["status"] == "success"
                and status != "success"
            ):
                return False
            connection.execute(
                """INSERT INTO ticker_snapshots(
                       ticker, as_of_date, fetched_at, status, posts_seen,
                       comments_seen, comments_selected, payload_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ticker, as_of_date) DO UPDATE SET
                       fetched_at = excluded.fetched_at,
                       status = excluded.status,
                       posts_seen = excluded.posts_seen,
                       comments_seen = excluded.comments_seen,
                       comments_selected = excluded.comments_selected,
                       payload_json = excluded.payload_json""",
                (
                    item.ticker, analysis_date, fetched_at, status,
                    stats["posts_seen"], stats["comments_seen"], stats["comments_selected"],
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
        return True

    def get_snapshot(self, ticker: str, as_of_date: str) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM ticker_snapshots WHERE ticker = ? AND as_of_date = ?",
                (ticker.upper(), as_of_date),
            ).fetchone()

    def put_raw_snapshot(
        self,
        item: PoolItem,
        analysis_date: str,
        query_hash: str,
        status: str,
        payload: dict,
    ) -> bool:
        """Atomically store raw data without replacing success with degradation."""

        stats = payload["stats"]
        with self.session() as connection:
            cursor = connection.execute(
                """INSERT INTO raw_snapshots(
                       ticker, analysis_date, query_hash, as_of, fetched_at, status,
                       posts_stored, comments_stored, payload_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ticker, analysis_date, query_hash) DO UPDATE SET
                       as_of = excluded.as_of,
                       fetched_at = excluded.fetched_at,
                       status = excluded.status,
                       posts_stored = excluded.posts_stored,
                       comments_stored = excluded.comments_stored,
                       payload_json = excluded.payload_json
                   WHERE raw_snapshots.status != 'success' OR excluded.status = 'success'""",
                (
                    item.ticker,
                    analysis_date,
                    query_hash,
                    payload["as_of"],
                    payload["fetched_at"],
                    status,
                    stats["posts_stored"],
                    stats["comments_stored"],
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
        return cursor.rowcount > 0

    def get_raw_snapshot(
        self, ticker: str, analysis_date: str, query_hash: str | None = None
    ) -> sqlite3.Row | None:
        with self.session() as connection:
            if query_hash:
                return connection.execute(
                    """SELECT * FROM raw_snapshots
                       WHERE ticker = ? AND analysis_date = ? AND query_hash = ?""",
                    (ticker.upper(), analysis_date, query_hash),
                ).fetchone()
            return connection.execute(
                """SELECT * FROM raw_snapshots
                   WHERE ticker = ? AND analysis_date = ?
                   ORDER BY CASE status WHEN 'success' THEN 0 ELSE 1 END, fetched_at DESC
                   LIMIT 1""",
                (ticker.upper(), analysis_date),
            ).fetchone()

    def purge(self, *, before: datetime) -> None:
        cutoff = _iso(before)
        with self.session() as connection:
            connection.execute("DELETE FROM thread_cache WHERE fetched_at < ?", (cutoff,))
            connection.execute("DELETE FROM raw_snapshots WHERE fetched_at < ?", (cutoff,))
            connection.execute("DELETE FROM ticker_snapshots WHERE fetched_at < ?", (cutoff,))
            connection.execute("DELETE FROM prefetch_runs WHERE started_at < ?", (cutoff,))
            connection.execute(
                "DELETE FROM prefetch_items WHERE run_id NOT IN (SELECT run_id FROM prefetch_runs)"
            )


def _raw_query_hash(
    item: PoolItem,
    *,
    subreddits: Sequence[str],
    window_hours: int,
    comments_per_post: int,
) -> str:
    config = {
        "schema_version": RedditCache.SCHEMA_VERSION,
        "ticker": item.ticker,
        "aliases": list(item.aliases),
        "subreddits": list(subreddits),
        "window_hours": window_hours,
        "search_limit_per_subreddit": 100,
        "comments_per_post": comments_per_post,
    }
    canonical = json.dumps(config, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dedupe_posts_raw(posts: Sequence[RedditPost]) -> list[RedditPost]:
    unique: dict[str, RedditPost] = {}
    for post in posts:
        unique.setdefault(post.post_id, post)
    return list(unique.values())


def _dedupe_comments_raw(comments: Sequence[RedditComment]) -> list[RedditComment]:
    unique: dict[str, RedditComment] = {}
    for comment in comments:
        unique.setdefault(comment.comment_id, comment)
    return list(unique.values())


def _raw_snapshot_payload(
    item: PoolItem,
    *,
    as_of: datetime,
    analysis_date: str,
    window_hours: int,
    status: str,
    query_hash: str,
    subreddits: Sequence[str],
    comments_per_post: int,
    posts: Sequence[RedditPost],
    comments_by_post: dict[str, Sequence[RedditComment]],
    errors: Sequence[str],
) -> dict:
    subreddit_counts: dict[str, int] = defaultdict(int)
    for post in posts:
        subreddit_counts[post.subreddit] += 1
    comments_stored = sum(len(comments) for comments in comments_by_post.values())
    return {
        "schema_version": RedditCache.SCHEMA_VERSION,
        "kind": "reddit_raw",
        "ticker": item.ticker,
        "aliases": list(item.aliases),
        "analysis_date": analysis_date,
        "query_hash": query_hash,
        "query": {
            "subreddits": list(subreddits),
            "window_hours": window_hours,
            "search_limit_per_subreddit": 100,
            "comments_per_post": comments_per_post,
        },
        "as_of": _iso(as_of),
        "window_start": _iso(as_of - timedelta(hours=window_hours)),
        "fetched_at": _iso(utc_now()),
        "status": status,
        "errors": list(errors),
        "stats": {
            "posts_returned": len(posts),
            "posts_stored": len(posts),
            "posts_with_comment_response": len(comments_by_post),
            "comments_requested_per_post": comments_per_post,
            "comments_stored": comments_stored,
            "comments_selected": 0,
            "selection_deferred": True,
            "search_limit_reached": any(count >= 100 for count in subreddit_counts.values()),
        },
        "posts": [
            {
                **asdict(post),
                "comments_fetched": post.post_id in comments_by_post,
                "comments": [
                    asdict(comment) for comment in comments_by_post.get(post.post_id, ())
                ],
            }
            for post in posts
        ],
    }


def prefetch_reddit(
    pool: Sequence[PoolItem],
    *,
    client: RedditRSSClient,
    cache: RedditCache,
    as_of: datetime,
    analysis_date: str | None = None,
    subreddits: Sequence[str] = DEFAULT_SUBREDDITS,
    window_hours: int = 168,
    comments_per_post: int = 20,
    thread_cache_hours: float = 168.0,
    reuse_existing: bool = True,
    progress: Callable[[str], None] | None = None,
) -> PrefetchReport:
    """Prefetch one ticker at a time and persist usable snapshots atomically."""

    if not pool:
        raise PoolInputError("stock pool is empty")
    if not (1 <= comments_per_post <= 500):
        raise ValueError("comments_per_post must be between 1 and 500")
    analysis_date = analysis_date or as_of.date().isoformat()
    try:
        if datetime.strptime(analysis_date, "%Y-%m-%d").date().isoformat() != analysis_date:
            raise ValueError
    except ValueError as exc:
        raise ValueError("analysis_date must use YYYY-MM-DD") from exc

    started = utc_now()
    run_id = f"reddit-{as_of.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    cache.begin_run(run_id, _iso(as_of), _iso(started))
    results: list[TickerResult] = []

    for item in pool:
        query_hash = _raw_query_hash(
            item,
            subreddits=subreddits,
            window_hours=window_hours,
            comments_per_post=comments_per_post,
        )
        if reuse_existing:
            existing = cache.get_raw_snapshot(item.ticker, analysis_date, query_hash)
            if existing:
                payload = json.loads(existing["payload_json"])
                if (
                    payload.get("as_of") == _iso(as_of)
                    and payload.get("schema_version") == RedditCache.SCHEMA_VERSION
                    and existing["status"] in {"success", "empty", "no_comments"}
                ):
                    stats = payload.get("stats") or {}
                    result = TickerResult(
                        ticker=item.ticker,
                        status="cached",
                        posts_seen=int(stats.get("posts_stored", 0)),
                        posts_fetched=int(stats.get("posts_with_comment_response", 0)),
                        comments_seen=int(stats.get("comments_stored", 0)),
                        comments_selected=0,
                    )
                    cache.record_item(run_id, result)
                    results.append(result)
                    if progress:
                        progress(f"{item.ticker}: exact as-of snapshot already complete; skipped")
                    continue
        if progress:
            progress(f"{item.ticker}: searching Reddit posts")
        result = TickerResult(ticker=item.ticker, status="running")
        try:
            search_outcome = client.search_posts(
                item, subreddits=subreddits, window_hours=window_hours, limit=100
            )
        except RedditFetchError as exc:
            result.status = "failed"
            result.errors.append(str(exc))
            cache.record_item(run_id, result)
            results.append(result)
            if progress:
                progress(f"{item.ticker}: search failed; previous successful cache preserved")
            continue

        if isinstance(search_outcome, SearchOutcome):
            raw_posts = list(search_outcome.posts)
            result.errors.extend(search_outcome.errors)
            if not raw_posts and search_outcome.errors:
                result.status = "failed"
                cache.record_item(run_id, result)
                results.append(result)
                if progress:
                    progress(f"{item.ticker}: all subreddit searches failed; previous cache preserved")
                continue
        else:
            # A small compatibility seam for deterministic test doubles and
            # callers written against the first prefetch prototype.
            raw_posts = search_outcome

        posts = _dedupe_posts_raw(raw_posts)
        result.posts_seen = len(posts)
        comments_by_post: dict[str, Sequence[RedditComment]] = {}

        for post in posts:
            cached_comments = cache.get_thread(
                post.post_id,
                max_age_hours=thread_cache_hours,
                now=utc_now(),
                requested_limit=comments_per_post,
            )
            if cached_comments is None:
                if progress:
                    progress(f"{item.ticker}: fetching r/{post.subreddit} thread {post.post_id}")
                try:
                    comments = _dedupe_comments_raw(
                        client.fetch_comments(post, limit=comments_per_post)
                    )
                    cache.put_thread(
                        post, comments, utc_now(), requested_limit=comments_per_post
                    )
                except RedditFetchError as exc:
                    result.errors.append(f"{post.post_id}: {exc}")
                    continue
            else:
                comments = cached_comments
                if progress:
                    progress(f"{item.ticker}: reused cached thread {post.post_id}")
            comments_by_post[post.post_id] = _dedupe_comments_raw(comments)

        result.posts_fetched = len(comments_by_post)
        result.comments_seen = sum(len(comments) for comments in comments_by_post.values())
        result.comments_selected = 0
        if not posts:
            result.status = "empty"
        elif not comments_by_post:
            result.status = "partial" if result.errors else "no_comments"
        elif result.errors:
            result.status = "partial"
        elif result.comments_seen == 0:
            result.status = "no_comments"
        else:
            result.status = "success"

        payload = _raw_snapshot_payload(
            item,
            as_of=as_of,
            analysis_date=analysis_date,
            window_hours=window_hours,
            status=result.status,
            query_hash=query_hash,
            subreddits=subreddits,
            comments_per_post=comments_per_post,
            posts=posts,
            comments_by_post=comments_by_post,
            errors=result.errors,
        )
        cache.put_raw_snapshot(item, analysis_date, query_hash, result.status, payload)
        cache.record_item(run_id, result)
        results.append(result)
        if progress:
            progress(
                f"{item.ticker}: {result.status}; {result.posts_fetched} threads, "
                f"{result.comments_seen} raw comments stored; selection deferred"
            )

    finished = utc_now()
    report = PrefetchReport(
        run_id=run_id,
        analysis_date=analysis_date,
        as_of=_iso(as_of),
        started_at=_iso(started),
        finished_at=_iso(finished),
        results=results,
    )
    cache.finish_run(
        run_id,
        report.finished_at,
        "partial" if report.has_degradation else "success",
    )
    return report


def _post_from_payload(post: dict) -> RedditPost:
    return RedditPost(
        post_id=str(post.get("post_id") or ""),
        title=str(post.get("title") or ""),
        subreddit=str(post.get("subreddit") or ""),
        permalink=str(post.get("permalink") or ""),
        body=str(post.get("body") or ""),
        published_at=post.get("published_at"),
        source_rank=int(post.get("source_rank") or 0),
    )


def _comment_from_payload(comment: dict) -> RedditComment:
    return RedditComment(
        comment_id=str(comment.get("comment_id") or ""),
        post_id=str(comment.get("post_id") or ""),
        post_title=str(comment.get("post_title") or ""),
        subreddit=str(comment.get("subreddit") or ""),
        author=str(comment.get("author") or ""),
        body=str(comment.get("body") or ""),
        permalink=str(comment.get("permalink") or ""),
        published_at=comment.get("published_at"),
        source_rank=int(comment.get("source_rank") or 0),
        quality_score=float(comment.get("quality_score") or 0.0),
    )


def _select_from_cached_payload(
    payload: dict, *, max_comments: int, window_hours: int
) -> list[RedditComment]:
    item = PoolItem(
        str(payload.get("ticker") or "").upper(),
        tuple(str(alias) for alias in payload.get("aliases") or ()),
    )
    as_of = _parse_datetime(payload.get("as_of")) or utc_now()
    post_payloads = payload.get("posts") or []
    posts = [_post_from_payload(post) for post in post_payloads if post.get("post_id")]
    selected_posts = select_posts(
        posts,
        item,
        as_of=as_of,
        window_hours=window_hours,
        max_posts=max(1, len(posts)),
    )
    allowed_post_ids = {post.post_id for post in selected_posts}
    comments_by_post: dict[str, list[RedditComment]] = {}
    for post in post_payloads:
        post_id = str(post.get("post_id") or "")
        if post_id not in allowed_post_ids:
            continue
        # Schema v2 stores raw comments. Schema v1 fallback only has comments
        # that were selected by the old prefetch path.
        raw_comments = post.get("comments")
        if raw_comments is None:
            raw_comments = post.get("selected_comments") or []
        comments = [_comment_from_payload(comment) for comment in raw_comments]
        comments_by_post[post_id] = _comments_in_window(
            comments, as_of=as_of, window_hours=window_hours
        )
    return select_comments(comments_by_post, item, max_total=max_comments)


def _format_snapshot(
    payload: dict,
    selected: Sequence[RedditComment],
    *,
    fresh: bool,
    evidence_status: str,
    legacy: bool,
) -> str:
    stats = payload.get("stats") or {}
    raw_status = payload.get("status", "unknown")
    freshness = "fresh" if fresh else "STALE"
    posts_stored = stats.get("posts_stored", stats.get("posts_fetched", 0))
    comments_stored = stats.get("comments_stored", stats.get("comments_seen", 0))
    lines = [
        (
            f"Reddit cached evidence ({freshness}, raw_status={raw_status}, "
            f"selection_status={evidence_status}, as_of={payload.get('as_of', '?')}):"
        ),
        (
            f"{posts_stored} raw posts stored; {comments_stored} raw comments stored; "
            f"{len(selected)} comments selected at read time. "
            "RSS provides no upvote or total-comment counts."
        ),
    ]
    if legacy:
        lines.append("Legacy schema v1 fallback: only the old preselected sample is available.")
    selected_by_post: dict[str, list[RedditComment]] = defaultdict(list)
    for comment in selected:
        selected_by_post[comment.post_id].append(comment)
    for post in payload.get("posts") or []:
        comments = selected_by_post.get(str(post.get("post_id") or ""), [])
        if not comments:
            continue
        lines.append(f"r/{post.get('subreddit', '?')} — {post.get('title', '').strip()}")
        for comment in comments:
            body = " ".join(comment.body.split())
            if len(body) > 420:
                body = body[:420] + "…"
            lines.append(f"  - {body}")
    errors = payload.get("errors") or []
    if errors:
        lines.append("Fetch degradation: " + " | ".join(str(error) for error in errors))
    return "\n".join(lines)


def load_cached_reddit(
    ticker: str,
    as_of_date: str,
    *,
    cache_path: Path = DEFAULT_CACHE_PATH,
    max_age_hours: float = 168.0,
    window_hours: int = 168,
    max_comments: int = 24,
    now: datetime | None = None,
) -> CachedRedditEvidence:
    """Select evidence from raw local data at read time; never touches the network."""

    if max_comments < 1:
        raise ValueError("max_comments must be positive")
    if window_hours < 1:
        raise ValueError("window_hours must be positive")
    now = now or utc_now()
    cache_path = Path(cache_path)
    row = None
    legacy = False
    if cache_path.is_file():
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                cache_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            try:
                row = connection.execute(
                    """SELECT * FROM raw_snapshots
                       WHERE ticker = ? AND analysis_date = ?
                       ORDER BY CASE status WHEN 'success' THEN 0 ELSE 1 END, fetched_at DESC
                       LIMIT 1""",
                    (ticker.upper(), as_of_date),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is None:
                row = connection.execute(
                    "SELECT * FROM ticker_snapshots WHERE ticker = ? AND as_of_date = ?",
                    (ticker.upper(), as_of_date),
                ).fetchone()
                legacy = row is not None
        except sqlite3.Error:
            row = None
        finally:
            if connection is not None:
                connection.close()
    if not row:
        text = (
            f"<REDDIT CACHE MISS for {ticker.upper()} on {as_of_date}. "
            "No live fallback was attempted; lower sentiment confidence.>"
        )
        return CachedRedditEvidence(
            ticker=ticker.upper(), as_of_date=as_of_date, status="missing",
            fresh=False, fetched_at=None, text=text, payload=None,
        )
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        text = (
            f"<REDDIT CACHE UNREADABLE for {ticker.upper()} on {as_of_date}. "
            "No live fallback was attempted; lower sentiment confidence.>"
        )
        return CachedRedditEvidence(
            ticker=ticker.upper(), as_of_date=as_of_date, status="unreadable",
            fresh=False, fetched_at=None, text=text, payload=None,
        )
    fetched_at = _parse_datetime(row["fetched_at"])
    fresh = bool(fetched_at and now - fetched_at <= timedelta(hours=max_age_hours))
    selected = _select_from_cached_payload(
        payload, max_comments=max_comments, window_hours=window_hours
    )
    raw_status = str(row["status"])
    if legacy:
        status = "legacy"
    elif raw_status == "success" and len(selected) < max_comments:
        status = "limited"
    else:
        status = raw_status
    if not fresh:
        status = "stale"
    payload["selection"] = {
        "selected_at_read_time": True,
        "requested_comments": max_comments,
        "selected_count": len(selected),
        "window_hours": window_hours,
        "comments": [asdict(comment) for comment in selected],
    }
    return CachedRedditEvidence(
        ticker=ticker.upper(),
        as_of_date=as_of_date,
        status=status,
        fresh=fresh,
        fetched_at=row["fetched_at"],
        text=_format_snapshot(
            payload,
            selected,
            fresh=fresh,
            evidence_status=status,
            legacy=legacy,
        ),
        payload=payload,
    )
