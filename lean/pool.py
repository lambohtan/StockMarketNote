"""Candidate stock-pool builder: multi-source heat mining for the StockWatch pipeline.

This module is the first stage of the product line.  It queries several
public "what is being talked about / traded today" endpoints, normalizes every
hit against a US listing whitelist, screens out non-common-stock instruments,
and produces a deterministic ranking.  No LLM is involved: ranking is plain
Python arithmetic over source ranks, so the same inputs always give the same
pool.

Failure handling is the point of most of the code here.  A heat source that
returns HTTP 429 must never look like "nobody is talking about anything
today", and a missing listing whitelist must never look like "no US stock
matched".  Every source carries its own health, an unusable listing map raises
instead of returning an empty pool, and unknown numbers stay ``None`` rather
than collapsing to zero.
"""

from __future__ import annotations

import html
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POOL_DIR = REPO_ROOT / "local-data" / "pool"

#: Fetch units.  One unit is one HTTP call whose health is reported as a whole.
APEWISDOM_ALL = "apewisdom:all-stocks"
APEWISDOM_WSB = "apewisdom:wallstreetbets"
STOCKTWITS_TRENDING = "stocktwits:trending"
NASDAQ_MOVERS = "nasdaq:marketmovers"

#: Scoring sources.  ``NASDAQ_MOVERS`` splits into these two.
NASDAQ_DOLLAR_VOLUME = "nasdaq:dollar-volume"
NASDAQ_ADVANCED = "nasdaq:advanced"

HEALTH_OK = "ok"
HEALTH_EMPTY = "empty"
HEALTH_FAILED = "failed"

LISTING_URL = ("https://api.nasdaq.com/api/screener/stocks"
               "?tableonly=true&limit=25&download=true&exchange={exchange}")
APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/{filter}/page/1"
STOCKTWITS_URL = "https://api.stocktwits.com/api/2/trending/symbols.json?limit=30"
NASDAQ_MOVERS_URL = "https://api.nasdaq.com/api/marketmovers"

_EXCHANGES = {"nasdaq": "NASDAQ", "nyse": "NYSE", "amex": "AMEX"}

#: Weight of each scoring source in the final heat score.
DEFAULT_WEIGHTS = {
    APEWISDOM_ALL: 0.35,
    APEWISDOM_WSB: 0.20,
    STOCKTWITS_TRENDING: 0.25,
    NASDAQ_DOLLAR_VOLUME: 0.15,
    NASDAQ_ADVANCED: 0.05,
}

#: How deep each source's ranking is treated as meaningful.
_RANK_SCALE = {
    APEWISDOM_ALL: 100.0,
    APEWISDOM_WSB: 100.0,
    STOCKTWITS_TRENDING: 30.0,
    NASDAQ_DOLLAR_VOLUME: 10.0,
    NASDAQ_ADVANCED: 10.0,
}

#: Extra credit for showing up in more than one independent source.
_COVERAGE_BONUS = 0.03
#: Cap on the 24h mention-growth bonus, so a low-base spike cannot dominate.
_MOMENTUM_CAP = 0.15
_MOMENTUM_GAIN = 0.06

_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")
#: StockTwits appends these to non-equity symbols (``ZORA.X`` crypto, ``DX_F`` futures).
_NON_EQUITY_SUFFIXES = (".X", ".CX")
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

_INSTRUMENT_PATTERNS = (
    ("warrant", re.compile(r"\bwarrants?\b", re.I)),
    ("right", re.compile(r"\brights?\b", re.I)),
    ("unit", re.compile(r"\bunits?\b", re.I)),
    ("preferred", re.compile(r"\bpreferred\b|\bpfd\b", re.I)),
    ("note", re.compile(r"\bnotes?\b|\bdebenture", re.I)),
    ("fund", re.compile(r"\betf\b|\bexchange[- ]traded fund\b|closed end fund", re.I)),
)
_COMMON_MARKERS = re.compile(r"common stock|ordinary share|depositary|class [a-z]\b", re.I)
_SUFFIX_INSTRUMENT = {"W": "warrant", "R": "right", "U": "unit"}

_NAME_TAIL_RE = re.compile(
    r"\s*(,?\s*(inc|corp|corporation|incorporated|company|co|ltd|limited|plc|holdings?|"
    r"group|nv|sa|ag)\.?)+\s*$", re.I)
_NAME_INSTRUMENT_RE = re.compile(
    r"\s*(common stock|ordinary shares?|class [a-z]|american depositary shares?.*|"
    r"common shares?|depositary shares?.*)\s*$", re.I)


class PoolBuildError(RuntimeError):
    """Base error for visible, non-silent pool build failures."""


class SourceFetchError(PoolBuildError):
    """A source request failed; this is never a legitimate empty result."""


class ListingUnavailableError(PoolBuildError):
    """The US listing whitelist could not be built, so no pool may be emitted."""


@dataclass(frozen=True)
class Listing:
    """One US-listed security from the Nasdaq screener whitelist."""

    ticker: str
    name: str
    exchange: str
    country: str
    instrument: str
    price: float | None
    market_cap: float | None


@dataclass(frozen=True)
class Candidate:
    """One ticker as observed by one source, with its auditable reason."""

    ticker: str
    source: str
    rank: int
    observed_at: str
    reason: str
    freshness: str
    metric: float | None = None
    momentum: float | None = None


@dataclass(frozen=True)
class SourceOutcome:
    """Health of one fetch unit; ``failed`` must never be read as ``empty``."""

    source: str
    health: str
    count: int = 0
    note: str = ""


@dataclass(frozen=True)
class ScreenedOut:
    ticker: str
    source: str
    reason: str


@dataclass(frozen=True)
class RankedTicker:
    ticker: str
    name: str
    market: str
    exchange: str
    country: str
    score: float
    sources: tuple[str, ...]
    reasons: tuple[str, ...]
    observed_at: str
    freshness: tuple[str, ...]
    mentions: int | None = None
    momentum: float | None = None
    price: float | None = None
    market_cap: float | None = None


@dataclass(frozen=True)
class PoolReport:
    as_of: str
    analysis_date: str
    top_n: int
    ranked: tuple[RankedTicker, ...]
    considered: tuple[RankedTicker, ...]
    outcomes: tuple[SourceOutcome, ...]
    excluded: tuple[ScreenedOut, ...]
    degraded_sources: tuple[str, ...]
    thresholds: Mapping[str, float]

    @property
    def complete(self) -> bool:
        """True only when every fetch unit answered; empty answers still count."""

        return not self.degraded_sources

    def to_pool_payload(self) -> dict:
        """Shape consumed directly by ``prefetch_reddit.py --pool-file``."""

        return {
            "as_of": self.as_of,
            "analysis_date": self.analysis_date,
            "complete": self.complete,
            "tickers": [
                {"ticker": entry.ticker, "aliases": _aliases_for(entry)}
                for entry in self.ranked
            ],
        }

    def to_provenance(self) -> dict:
        """Full auditable record: every contract field, health and exclusion."""

        health = {outcome.source: outcome.health for outcome in self.outcomes}
        return {
            "as_of": self.as_of,
            "analysis_date": self.analysis_date,
            "complete": self.complete,
            "degraded_sources": list(self.degraded_sources),
            "thresholds": dict(self.thresholds),
            "sources": [
                {"source": o.source, "health": o.health, "count": o.count, "note": o.note}
                for o in self.outcomes
            ],
            "pool": [
                {
                    "rank": index,
                    "ticker": entry.ticker,
                    "name": entry.name,
                    "market": entry.market,
                    "exchange": entry.exchange,
                    "country": entry.country,
                    "score": round(entry.score, 6),
                    "sources": list(entry.sources),
                    "observed_at": entry.observed_at,
                    "reasons": list(entry.reasons),
                    "freshness": list(entry.freshness),
                    "source_health": {s: health.get(_fetch_unit(s), "unknown")
                                      for s in entry.sources},
                    "mentions": entry.mentions,
                    "momentum": entry.momentum,
                    "price": entry.price,
                    "market_cap": entry.market_cap,
                }
                for index, entry in enumerate(self.ranked, start=1)
            ],
            "excluded": [
                {"ticker": e.ticker, "source": e.source, "reason": e.reason}
                for e in self.excluded
            ],
        }


def _fetch_unit(source: str) -> str:
    return NASDAQ_MOVERS if source in (NASDAQ_DOLLAR_VOLUME, NASDAQ_ADVANCED) else source


def _aliases_for(entry: RankedTicker) -> list[str]:
    alias = _NAME_INSTRUMENT_RE.sub("", entry.name or "")
    alias = _NAME_TAIL_RE.sub("", alias).strip(" ,.")
    if not alias or alias.upper() == entry.ticker:
        return []
    return [alias]


def normalize_ticker(raw: object) -> str | None:
    """Return the canonical symbol, or ``None`` when it is not a US equity symbol."""

    if not isinstance(raw, str):
        return None
    symbol = raw.strip().upper().lstrip("$")
    symbol = symbol.replace("/", ".")
    if not symbol or any(symbol.endswith(s) for s in _NON_EQUITY_SUFFIXES):
        return None
    return symbol if _TICKER_RE.match(symbol) else None


def classify_instrument(ticker: str, name: str) -> str:
    """Classify a listing as common stock or a derivative/non-equity wrapper."""

    text = html.unescape(name or "")
    for kind, pattern in _INSTRUMENT_PATTERNS:
        if pattern.search(text):
            return kind
    if len(ticker) == 5 and ticker[-1] in _SUFFIX_INSTRUMENT and not _COMMON_MARKERS.search(text):
        return _SUFFIX_INSTRUMENT[ticker[-1]]
    return "common"


def _number(raw: object) -> float | None:
    """Parse a screener number, leaving unparsable values unknown, not zero."""

    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().replace("$", "").replace(",", "").replace("%", "")
    if not cleaned or cleaned in {"--", "N/A", "NA"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _rows(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("rows"), list):
            return [r for r in data["rows"] if isinstance(r, dict)]
        table = data.get("table")
        if isinstance(table, dict) and isinstance(table.get("rows"), list):
            return [r for r in table["rows"] if isinstance(r, dict)]
    return []


def parse_nasdaq_listings(payload_by_exchange: Mapping[str, object]) -> dict[str, Listing]:
    """Build the US listing whitelist keyed by canonical ticker."""

    listings: dict[str, Listing] = {}
    for exchange_key, payload in payload_by_exchange.items():
        exchange = _EXCHANGES.get(exchange_key.lower(), exchange_key.upper())
        for row in _rows(payload):
            ticker = normalize_ticker(row.get("symbol"))
            if not ticker:
                continue
            name = html.unescape(str(row.get("name") or "")).strip()
            listings.setdefault(ticker, Listing(
                ticker=ticker,
                name=name,
                exchange=exchange,
                country=str(row.get("country") or "").strip(),
                instrument=classify_instrument(ticker, name),
                price=_number(row.get("lastsale")),
                market_cap=_number(row.get("marketCap")),
            ))
    return listings


def parse_apewisdom(payload: object, source: str, observed_at: str,
                    limit: int | None = None) -> list[Candidate]:
    """Parse ApeWisdom's Reddit mention aggregation into ranked candidates."""

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    candidates: list[Candidate] = []
    for index, row in enumerate(results, start=1):
        if not isinstance(row, dict):
            continue
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker:
            continue
        rank = int(row.get("rank") or index)
        mentions = _number(row.get("mentions"))
        baseline = _number(row.get("mentions_24h_ago"))
        # A missing baseline is unknown momentum, not flat momentum.
        momentum = mentions / baseline if mentions is not None and baseline else None
        baseline_text = f"{int(baseline)}" if baseline is not None else "unknown"
        candidates.append(Candidate(
            ticker=ticker,
            source=source,
            rank=rank,
            observed_at=observed_at,
            reason=(f"{source} rank {rank}, {int(mentions) if mentions is not None else '?'} "
                    f"mentions (24h prior {baseline_text})"),
            freshness="fetch_time_only",
            metric=mentions,
            momentum=momentum,
        ))
        if limit and len(candidates) >= limit:
            break
    return candidates


def parse_stocktwits(payload: object, observed_at: str) -> list[Candidate]:
    """Parse StockTwits trending symbols, keeping US common-stock symbols only."""

    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, list):
        return []
    candidates: list[Candidate] = []
    for row in symbols:
        if not isinstance(row, dict):
            continue
        if str(row.get("region") or "US").upper() != "US":
            continue
        instrument = str(row.get("instrument_class") or "Stock")
        if instrument.replace(" ", "").lower() not in {"stock", "commonstock"}:
            continue
        ticker = normalize_ticker(row.get("symbol"))
        if not ticker:
            continue
        rank = len(candidates) + 1
        score = _number(row.get("trending_score"))
        watchlist = _number(row.get("watchlist_count"))
        candidates.append(Candidate(
            ticker=ticker,
            source=STOCKTWITS_TRENDING,
            rank=rank,
            observed_at=observed_at,
            reason=(f"stocktwits trending rank {rank}, score "
                    f"{'unknown' if score is None else round(score, 2)}, watchlist "
                    f"{'unknown' if watchlist is None else int(watchlist)}"),
            freshness="fetch_time_only",
            metric=score,
        ))
    return candidates


def _parse_declared_time(raw: object) -> datetime | None:
    """Parse Nasdaq's ``Data as of Aug 31, 2026 3:58 AM ET`` stamp as US Eastern."""

    if not isinstance(raw, str):
        return None
    match = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})(?:\s+(\d{1,2}:\d{2}\s*[AP]M))?", raw)
    if not match:
        return None
    stamp = match.group(1) + (f" {match.group(2)}" if match.group(2) else " 12:00 AM")
    try:
        naive = datetime.strptime(stamp.replace("  ", " "), "%b %d, %Y %I:%M %p")
    except ValueError:
        return None
    # Nasdaq labels these ET without an offset; -04:00 is used and the residual
    # one-hour standard-time error stays far inside the 24h freshness window.
    return naive.replace(tzinfo=timezone(timedelta(hours=-4)))


def _freshness(declared: datetime | None, as_of: datetime) -> str:
    if declared is None:
        return "unknown"
    age = as_of - declared
    return "declared_intraday" if timedelta(hours=-2) <= age <= timedelta(hours=24) \
        else "declared_stale"


def parse_nasdaq_movers(payload: object, observed_at: str,
                        as_of: datetime) -> dict[str, list[Candidate]]:
    """Split Nasdaq market movers into dollar-volume and advancing sources."""

    stocks = {}
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        stocks = payload["data"].get("STOCKS") or {}
    grouped: dict[str, list[Candidate]] = {NASDAQ_DOLLAR_VOLUME: [], NASDAQ_ADVANCED: []}
    tables = ((NASDAQ_DOLLAR_VOLUME, "MostActiveByDollarVolume", "most active by dollar volume"),
              (NASDAQ_ADVANCED, "MostAdvanced", "top gainer"))
    for source, key, label in tables:
        section = stocks.get(key) if isinstance(stocks, dict) else None
        if not isinstance(section, dict):
            continue
        freshness = _freshness(_parse_declared_time(section.get("dataAsOf")), as_of)
        for index, row in enumerate(_rows({"data": section}), start=1):
            ticker = normalize_ticker(row.get("symbol"))
            if not ticker:
                continue
            change = str(row.get("change") or "").strip()
            grouped[source].append(Candidate(
                ticker=ticker,
                source=source,
                rank=index,
                observed_at=observed_at,
                reason=f"nasdaq {label} rank {index} ({change or 'change unknown'})",
                freshness=freshness,
                metric=_number(change),
            ))
    return grouped


def screen_candidates(listings: Mapping[str, Listing], candidates: Iterable[Candidate],
                      min_price: float, min_market_cap: float
                      ) -> tuple[list[Candidate], list[ScreenedOut]]:
    """Keep US-listed common stock that clears the liquidity floor.

    Unknown price or market cap is kept rather than rejected: the contract
    forbids turning an unknown number into a disqualifying zero.
    """

    kept: list[Candidate] = []
    excluded: list[ScreenedOut] = []
    for candidate in candidates:
        listing = listings.get(candidate.ticker)
        if listing is None:
            excluded.append(ScreenedOut(candidate.ticker, candidate.source, "not_us_listed"))
            continue
        if listing.instrument != "common":
            excluded.append(ScreenedOut(candidate.ticker, candidate.source,
                                        f"instrument_{listing.instrument}"))
            continue
        if listing.price is not None and listing.price < min_price:
            excluded.append(ScreenedOut(candidate.ticker, candidate.source, "below_price_floor"))
            continue
        if listing.market_cap is not None and listing.market_cap < min_market_cap:
            excluded.append(ScreenedOut(candidate.ticker, candidate.source,
                                        "below_market_cap_floor"))
            continue
        kept.append(candidate)
    return kept, excluded


def rank_candidates(listings: Mapping[str, Listing], candidates: Iterable[Candidate],
                    weights: Mapping[str, float] | None = None) -> list[RankedTicker]:
    """Score and order candidates deterministically; no model is consulted."""

    weights = dict(weights or DEFAULT_WEIGHTS)
    best: dict[str, dict[str, Candidate]] = {}
    for candidate in candidates:
        per_source = best.setdefault(candidate.ticker, {})
        previous = per_source.get(candidate.source)
        if previous is None or candidate.rank < previous.rank:
            per_source[candidate.source] = candidate

    ranked: list[RankedTicker] = []
    for ticker, per_source in best.items():
        listing = listings.get(ticker)
        score = 0.0
        for source, candidate in per_source.items():
            scale = _RANK_SCALE.get(source, 50.0)
            score += weights.get(source, 0.0) * max(0.0, 1.0 - (candidate.rank - 1) / scale)
        score += _COVERAGE_BONUS * (len(per_source) - 1)

        momentum_values = [c.momentum for c in per_source.values() if c.momentum]
        momentum = max(momentum_values) if momentum_values else None
        if momentum and momentum > 1.0:
            score += min(_MOMENTUM_CAP, _MOMENTUM_GAIN * math.log2(momentum))

        mention_values = [c.metric for c in per_source.values()
                          if c.source in (APEWISDOM_ALL, APEWISDOM_WSB) and c.metric is not None]
        ordered = sorted(per_source.values(), key=lambda c: (-weights.get(c.source, 0.0), c.source))
        ranked.append(RankedTicker(
            ticker=ticker,
            name=listing.name if listing else "",
            market="US",
            exchange=listing.exchange if listing else "UNKNOWN",
            country=listing.country if listing else "",
            score=score,
            sources=tuple(c.source for c in ordered),
            reasons=tuple(c.reason for c in ordered),
            observed_at=min(c.observed_at for c in per_source.values()),
            freshness=tuple(dict.fromkeys(c.freshness for c in ordered)),
            mentions=int(max(mention_values)) if mention_values else None,
            momentum=momentum,
            price=listing.price if listing else None,
            market_cap=listing.market_cap if listing else None,
        ))

    ranked.sort(key=lambda e: (-round(e.score, 9), -(e.mentions or 0), e.ticker))
    return ranked


class PoolHttpClient:
    """Minimal JSON fetcher with bounded retries; failures stay loud."""

    def __init__(self, opener: Callable = urlopen, timeout: float = 25.0,
                 retries: int = 2, pause: float = 1.5,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self._opener = opener
        self._timeout = timeout
        self._retries = retries
        self._pause = pause
        self._sleep = sleeper

    def fetch_json(self, url: str) -> object:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            request = Request(url, headers={"User-Agent": _USER_AGENT,
                                            "Accept": "application/json"})
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    return json.loads(response.read().decode("utf-8", "replace"))
            except HTTPError as exc:
                last = SourceFetchError(f"HTTP {exc.code} for {url}")
            except (URLError, TimeoutError, OSError) as exc:
                last = SourceFetchError(f"{type(exc).__name__}: {exc} for {url}")
            except json.JSONDecodeError as exc:
                last = SourceFetchError(f"invalid JSON from {url}: {exc}")
            if attempt < self._retries:
                self._sleep(self._pause * (attempt + 1))
        raise last if last else SourceFetchError(f"unknown failure for {url}")


def _fetch_listings(client, exchanges: Sequence[str]) -> dict[str, Listing]:
    payloads: dict[str, object] = {}
    for exchange in exchanges:
        try:
            payloads[exchange] = client.fetch_json(LISTING_URL.format(exchange=exchange))
        except SourceFetchError as exc:
            raise ListingUnavailableError(
                f"US listing whitelist incomplete ({exchange}): {exc}; "
                "refusing to emit a pool that cannot be verified as US-listed") from exc
    listings = parse_nasdaq_listings(payloads)
    if not listings:
        raise ListingUnavailableError("US listing whitelist parsed to zero rows")
    return listings


def build_pool(client, *, as_of: datetime, analysis_date: str, top_n: int = 20,
               min_price: float = 3.0, min_market_cap: float = 3e8,
               weights: Mapping[str, float] | None = None) -> PoolReport:
    """Fetch every source, screen, rank and report with per-source health."""

    observed_at = as_of.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    listings = _fetch_listings(client, tuple(_EXCHANGES))

    outcomes: list[SourceOutcome] = []
    candidates: list[Candidate] = []
    degraded: list[str] = []

    def run(unit: str, url: str, parse) -> None:
        try:
            payload = client.fetch_json(url)
        except SourceFetchError as exc:
            outcomes.append(SourceOutcome(unit, HEALTH_FAILED, 0, str(exc)))
            degraded.append(unit)
            return
        records = parse(payload)
        candidates.extend(records)
        outcomes.append(SourceOutcome(unit, HEALTH_OK if records else HEALTH_EMPTY, len(records),
                                      "" if records else "source responded with no usable rows"))

    run(APEWISDOM_ALL, APEWISDOM_URL.format(filter="all-stocks"),
        lambda p: parse_apewisdom(p, APEWISDOM_ALL, observed_at))
    run(APEWISDOM_WSB, APEWISDOM_URL.format(filter="wallstreetbets"),
        lambda p: parse_apewisdom(p, APEWISDOM_WSB, observed_at))
    run(STOCKTWITS_TRENDING, STOCKTWITS_URL,
        lambda p: parse_stocktwits(p, observed_at))
    run(NASDAQ_MOVERS, NASDAQ_MOVERS_URL,
        lambda p: [c for group in parse_nasdaq_movers(p, observed_at, as_of).values()
                   for c in group])

    kept, excluded = screen_candidates(listings, candidates, min_price, min_market_cap)
    considered = rank_candidates(listings, kept, weights)
    return PoolReport(
        as_of=observed_at,
        analysis_date=analysis_date,
        top_n=top_n,
        ranked=tuple(considered[:top_n]),
        considered=tuple(considered),
        outcomes=tuple(outcomes),
        excluded=tuple(excluded),
        degraded_sources=tuple(degraded),
        thresholds={"min_price": min_price, "min_market_cap": min_market_cap},
    )
