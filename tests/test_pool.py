from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import build_pool as pool_cli
from lean.pool import (
    APEWISDOM_ALL,
    APEWISDOM_WSB,
    HEALTH_EMPTY,
    HEALTH_FAILED,
    HEALTH_OK,
    ListingUnavailableError,
    NASDAQ_ADVANCED,
    NASDAQ_DOLLAR_VOLUME,
    SourceFetchError,
    STOCKTWITS_TRENDING,
    build_pool,
    classify_instrument,
    normalize_ticker,
    parse_apewisdom,
    parse_nasdaq_listings,
    parse_nasdaq_movers,
    parse_stocktwits,
    rank_candidates,
    screen_candidates,
)
from lean.reddit_prefetch import load_pool

AS_OF = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)
OBSERVED = AS_OF.isoformat()


def listing_row(symbol, name, price="100.00", cap="900000000.00", country="United States"):
    return {
        "symbol": symbol,
        "name": name,
        "lastsale": f"${price}",
        "netchange": "1.00",
        "pctchange": "1.00%",
        "volume": "1000000",
        "marketCap": cap,
        "country": country,
        "ipoyear": "2000",
        "industry": "Semiconductors",
        "sector": "Technology",
    }


def listings_payload():
    return {
        "nasdaq": {"data": {"rows": [
            listing_row("NVDA", "NVIDIA Corporation Common Stock", "217.55", "5242955000000.00"),
            listing_row("MU", "Micron Technology, Inc. Common Stock", "932.86", "1000000000000.00"),
            listing_row("AMD", "Advanced Micro Devices, Inc. Common Stock", "150.00", "240000000000.00"),
            listing_row("MIACW", "Meridian3 Industrials Acquisition Corp Warrant", "1.02", "50000000.00"),
            listing_row("PENNY", "Penny Holdings, Inc. Common Stock", "0.39", "40000000.00"),
            listing_row("BABA", "Alibaba Group Holding Limited American Depositary Shares",
                        "120.00", "288033587540.00", country="China"),
        ]}},
        "nyse": {"data": {"rows": [
            listing_row("GAP", "The Gap, Inc. Common Stock", "25.00", "9000000000.00"),
            listing_row("PFDX", "Sample Bancorp Inc. 7% Series A Preferred Stock", "25.00", "800000000.00"),
        ]}},
        "amex": {"data": {"rows": [
            listing_row("HL", "Hecla Mining Company Common Stock", "12.00", "7000000000.00"),
        ]}},
    }


def apewisdom_payload(results):
    return {"count": len(results), "pages": 1, "current_page": 1, "results": results}


def ape_row(rank, ticker, name, mentions, mentions_24h, upvotes=100):
    return {
        "rank": rank, "ticker": ticker, "name": name, "mentions": mentions,
        "upvotes": upvotes, "rank_24h_ago": rank, "mentions_24h_ago": mentions_24h,
    }


def stocktwits_payload(symbols):
    return {"symbols": symbols, "response": {"status": 200}}


def st_row(symbol, instrument_class="Stock", region="US", score=1.0, watchlist=1000):
    return {
        "symbol": symbol, "symbol_display": symbol, "exchange": "NASDAQ", "region": region,
        "title": symbol, "watchlist_count": watchlist, "instrument_class": instrument_class,
        "trending": True, "trending_score": score,
    }


def movers_payload(dollar_rows, advanced_rows, as_of="Data as of Aug 31, 2026 3:58 AM ET"):
    def table(rows):
        return {"dataAsOf": as_of, "table": {"headers": {}, "rows": rows}}
    return {"data": {"STOCKS": {
        "MostActiveByDollarVolume": table(dollar_rows),
        "MostAdvanced": table(advanced_rows),
    }}, "status": {"rCode": 200}}


def mover_row(symbol, name, price="100.00", change="+1%"):
    return {"symbol": symbol, "name": name, "lastSalePrice": f"${price}",
            "lastSaleChange": "+1.00", "change": change, "deltaIndicator": "up"}


DEFAULT_APE_ALL = apewisdom_payload([
    ape_row(1, "SPY", "SPDR S&amp;P 500 ETF Trust", 101, 66),
    ape_row(2, "NVDA", "NVIDIA", 56, 52),
    ape_row(3, "MU", "Micron Technology", 51, 30),
    ape_row(4, "GAP", "Gap Inc", 20, 19),
])
DEFAULT_APE_WSB = apewisdom_payload([
    ape_row(1, "NVDA", "NVIDIA", 30, 25),
    ape_row(2, "MU", "Micron Technology", 20, 10),
])
DEFAULT_ST = stocktwits_payload([
    st_row("QQQ", instrument_class="ExchangeTradedFund"),
    st_row("ZORA.X", instrument_class="CRYPTO", region="X"),
    st_row("GAP"),
    st_row("HL"),
    st_row("NVDA"),
])
DEFAULT_MOVERS = movers_payload(
    [mover_row("NVDA", "NVIDIA Corporation"), mover_row("MU", "Micron Technology, Inc.")],
    [mover_row("MIACW", "Meridian3 Industrials Acquisition Corp Warrant", "1.02", "+240%"),
     mover_row("AMD", "Advanced Micro Devices, Inc.", "150.00", "+9%")],
)


class FakeClient:
    """Offline stand-in for the HTTP client; maps URL fragments to payloads."""

    def __init__(self, listings=None, ape_all=None, ape_wsb=None, stocktwits=None, movers=None):
        self.listings = listings_payload() if listings is None else listings
        self.ape_all = DEFAULT_APE_ALL if ape_all is None else ape_all
        self.ape_wsb = DEFAULT_APE_WSB if ape_wsb is None else ape_wsb
        self.stocktwits = DEFAULT_ST if stocktwits is None else stocktwits
        self.movers = DEFAULT_MOVERS if movers is None else movers
        self.calls: list[str] = []

    def fetch_json(self, url: str):
        self.calls.append(url)
        if "screener/stocks" in url:
            for exchange, payload in self.listings.items():
                if f"exchange={exchange}" in url:
                    return self._resolve(payload)
            raise AssertionError(f"unexpected listing url {url}")
        if "apewisdom" in url and "all-stocks" in url:
            return self._resolve(self.ape_all)
        if "apewisdom" in url and "wallstreetbets" in url:
            return self._resolve(self.ape_wsb)
        if "stocktwits" in url:
            return self._resolve(self.stocktwits)
        if "marketmovers" in url:
            return self._resolve(self.movers)
        raise AssertionError(f"unexpected url {url}")

    @staticmethod
    def _resolve(payload):
        if isinstance(payload, Exception):
            raise payload
        return payload


class NormalizationTest(unittest.TestCase):
    def test_normalize_strips_decoration(self):
        self.assertEqual(normalize_ticker(" $nvda "), "NVDA")
        self.assertEqual(normalize_ticker("BRK.B"), "BRK.B")

    def test_normalize_rejects_non_equity_symbols(self):
        for raw in ("", "   ", None, "ZORA.X", "DX_F", "TOO-LONG-SYMBOL-HERE"):
            self.assertIsNone(normalize_ticker(raw), raw)

    def test_classify_instrument(self):
        self.assertEqual(classify_instrument("NVDA", "NVIDIA Corporation Common Stock"), "common")
        self.assertEqual(classify_instrument("MIACW", "Meridian3 Acquisition Corp Warrant"), "warrant")
        self.assertEqual(classify_instrument("PFDX", "Sample 7% Series A Preferred Stock"), "preferred")
        self.assertEqual(classify_instrument("ABCU", "Sample Acquisition Corp Units"), "unit")
        self.assertEqual(
            classify_instrument("BABA", "Alibaba Group Holding Limited American Depositary Shares"),
            "common",
        )


class ListingTest(unittest.TestCase):
    def test_listings_carry_exchange_and_numbers(self):
        listings = parse_nasdaq_listings(listings_payload())
        self.assertEqual(listings["NVDA"].exchange, "NASDAQ")
        self.assertEqual(listings["GAP"].exchange, "NYSE")
        self.assertEqual(listings["HL"].exchange, "AMEX")
        self.assertAlmostEqual(listings["NVDA"].price, 217.55)
        self.assertAlmostEqual(listings["AMD"].market_cap, 240000000000.0)
        self.assertEqual(listings["BABA"].country, "China")

    def test_unparsable_numbers_stay_unknown_not_zero(self):
        payload = {"nasdaq": {"data": {"rows": [
            {"symbol": "XYZ", "name": "Sample Common Stock", "lastsale": "",
             "marketCap": "", "country": "United States"},
        ]}}}
        listings = parse_nasdaq_listings(payload)
        self.assertIsNone(listings["XYZ"].price)
        self.assertIsNone(listings["XYZ"].market_cap)


class SourceParserTest(unittest.TestCase):
    def test_apewisdom_records_rank_mentions_and_momentum(self):
        records = parse_apewisdom(DEFAULT_APE_ALL, APEWISDOM_ALL, OBSERVED)
        nvda = next(r for r in records if r.ticker == "NVDA")
        self.assertEqual(nvda.rank, 2)
        self.assertEqual(nvda.metric, 56.0)
        self.assertAlmostEqual(nvda.momentum, 56 / 52)
        self.assertEqual(nvda.freshness, "fetch_time_only")
        self.assertIn("mentions", nvda.reason)

    def test_apewisdom_missing_baseline_is_unknown_momentum_not_zero(self):
        payload = apewisdom_payload([{"rank": 1, "ticker": "NVDA", "name": "NVIDIA", "mentions": 10}])
        record = parse_apewisdom(payload, APEWISDOM_ALL, OBSERVED)[0]
        self.assertIsNone(record.momentum)

    def test_stocktwits_drops_etf_crypto_and_non_us(self):
        records = parse_stocktwits(DEFAULT_ST, OBSERVED)
        self.assertEqual([r.ticker for r in records], ["GAP", "HL", "NVDA"])
        self.assertEqual(records[0].rank, 1)

    def test_nasdaq_movers_split_into_named_sources_with_declared_time(self):
        grouped = parse_nasdaq_movers(DEFAULT_MOVERS, OBSERVED, AS_OF)
        self.assertEqual([r.ticker for r in grouped[NASDAQ_DOLLAR_VOLUME]], ["NVDA", "MU"])
        self.assertEqual([r.ticker for r in grouped[NASDAQ_ADVANCED]], ["MIACW", "AMD"])
        self.assertEqual(grouped[NASDAQ_DOLLAR_VOLUME][0].freshness, "declared_intraday")

    def test_nasdaq_movers_stale_declared_time_is_marked(self):
        payload = movers_payload([mover_row("NVDA", "NVIDIA Corporation")], [],
                                 as_of="Data as of Aug 20, 2026 3:58 AM ET")
        grouped = parse_nasdaq_movers(payload, OBSERVED, AS_OF)
        self.assertEqual(grouped[NASDAQ_DOLLAR_VOLUME][0].freshness, "declared_stale")


class ScreenTest(unittest.TestCase):
    def setUp(self):
        self.listings = parse_nasdaq_listings(listings_payload())

    def screen(self, tickers, source=APEWISDOM_ALL):
        records = parse_apewisdom(
            apewisdom_payload([ape_row(i + 1, t, t, 10, 5) for i, t in enumerate(tickers)]),
            source, OBSERVED)
        return screen_candidates(self.listings, records, min_price=3.0, min_market_cap=3e8)

    def test_unlisted_ticker_is_excluded_as_not_us_listed(self):
        kept, excluded = self.screen(["SPY", "NVDA"])
        self.assertEqual([c.ticker for c in kept], ["NVDA"])
        self.assertEqual(excluded[0].ticker, "SPY")
        self.assertEqual(excluded[0].reason, "not_us_listed")

    def test_warrant_and_preferred_are_excluded(self):
        _, excluded = self.screen(["MIACW", "PFDX"])
        self.assertEqual({e.reason for e in excluded}, {"instrument_warrant", "instrument_preferred"})

    def test_liquidity_floor_excludes_penny_names(self):
        _, excluded = self.screen(["PENNY"])
        self.assertEqual(excluded[0].reason, "below_price_floor")

    def test_us_listed_adr_is_kept(self):
        kept, _ = self.screen(["BABA"])
        self.assertEqual([c.ticker for c in kept], ["BABA"])


class RankingTest(unittest.TestCase):
    def setUp(self):
        self.listings = parse_nasdaq_listings(listings_payload())

    def rank(self, records):
        kept, _ = screen_candidates(self.listings, records, min_price=3.0, min_market_cap=3e8)
        return rank_candidates(self.listings, kept)

    def test_cross_source_confirmation_beats_single_source_rank(self):
        records = [
            *parse_apewisdom(apewisdom_payload([ape_row(1, "GAP", "Gap", 40, 40),
                                                ape_row(9, "NVDA", "NVIDIA", 10, 10)]),
                             APEWISDOM_ALL, OBSERVED),
            *parse_apewisdom(apewisdom_payload([ape_row(1, "NVDA", "NVIDIA", 10, 10)]),
                             APEWISDOM_WSB, OBSERVED),
            *parse_stocktwits(stocktwits_payload([st_row("NVDA")]), OBSERVED),
        ]
        ranked = self.rank(records)
        self.assertEqual(ranked[0].ticker, "NVDA")
        self.assertEqual(len(ranked[0].sources), 3)

    def test_momentum_breaks_a_tie_between_equal_ranks(self):
        records = parse_apewisdom(
            apewisdom_payload([ape_row(1, "NVDA", "NVIDIA", 40, 10),
                               ape_row(1, "MU", "Micron", 40, 40)]),
            APEWISDOM_ALL, OBSERVED)
        ranked = self.rank(records)
        self.assertEqual(ranked[0].ticker, "NVDA")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_identical_evidence_ties_break_alphabetically_and_are_stable(self):
        records = parse_apewisdom(
            apewisdom_payload([ape_row(1, "MU", "Micron", 10, 10),
                               ape_row(1, "AMD", "AMD", 10, 10)]),
            APEWISDOM_ALL, OBSERVED)
        first = [r.ticker for r in self.rank(records)]
        second = [r.ticker for r in self.rank(list(reversed(records)))]
        self.assertEqual(first, ["AMD", "MU"])
        self.assertEqual(first, second)

    def test_ranked_entry_keeps_provenance(self):
        ranked = self.rank(parse_apewisdom(DEFAULT_APE_ALL, APEWISDOM_ALL, OBSERVED))
        entry = next(r for r in ranked if r.ticker == "NVDA")
        self.assertEqual(entry.market, "US")
        self.assertEqual(entry.exchange, "NASDAQ")
        self.assertTrue(entry.reasons)
        self.assertEqual(entry.sources, (APEWISDOM_ALL,))


class BuildPoolTest(unittest.TestCase):
    def build(self, client, **kwargs):
        return build_pool(client, as_of=AS_OF, analysis_date="2026-08-31", **kwargs)

    def test_healthy_run_is_complete_and_excludes_etfs(self):
        report = self.build(FakeClient())
        tickers = [entry.ticker for entry in report.ranked]
        self.assertTrue(report.complete)
        self.assertNotIn("SPY", tickers)
        self.assertNotIn("QQQ", tickers)
        self.assertNotIn("MIACW", tickers)
        self.assertEqual(report.ranked[0].ticker, "NVDA")
        self.assertEqual({o.health for o in report.outcomes}, {HEALTH_OK})

    def test_failed_source_marks_report_degraded_not_empty_success(self):
        client = FakeClient(stocktwits=SourceFetchError("stocktwits: HTTP 429"))
        report = self.build(client)
        self.assertFalse(report.complete)
        self.assertIn(STOCKTWITS_TRENDING, report.degraded_sources)
        outcome = next(o for o in report.outcomes if o.source == STOCKTWITS_TRENDING)
        self.assertEqual(outcome.health, HEALTH_FAILED)
        self.assertIn("429", outcome.note)
        self.assertTrue(report.ranked, "healthy sources still produce candidates")

    def test_empty_source_is_success_not_failure(self):
        report = self.build(FakeClient(stocktwits=stocktwits_payload([])))
        outcome = next(o for o in report.outcomes if o.source == STOCKTWITS_TRENDING)
        self.assertEqual(outcome.health, HEALTH_EMPTY)
        self.assertTrue(report.complete)

    def test_listing_failure_fails_closed_with_no_pool(self):
        client = FakeClient(listings={"nasdaq": SourceFetchError("HTTP 403"),
                                      "nyse": listings_payload()["nyse"],
                                      "amex": listings_payload()["amex"]})
        with self.assertRaises(ListingUnavailableError):
            self.build(client)

    def test_all_heat_sources_failing_never_returns_a_clean_empty_pool(self):
        err = SourceFetchError("HTTP 500")
        client = FakeClient(ape_all=err, ape_wsb=err, stocktwits=err, movers=err)
        report = self.build(client)
        self.assertFalse(report.complete)
        self.assertEqual(report.ranked, ())
        self.assertEqual(len(report.degraded_sources), 4)

    def test_top_n_limits_output_but_keeps_full_provenance(self):
        report = self.build(FakeClient(), top_n=2)
        self.assertEqual(len(report.ranked), 2)
        self.assertGreater(len(report.considered), 2)

    def test_pool_payload_is_consumable_by_reddit_prefetch(self):
        report = self.build(FakeClient(), top_n=3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pool.json"
            path.write_text(json.dumps(report.to_pool_payload()), encoding="utf-8")
            items = load_pool(path)
        self.assertEqual([item.ticker for item in items],
                         [entry.ticker for entry in report.ranked])

    def test_provenance_records_contract_fields(self):
        report = self.build(FakeClient(), top_n=1)
        entry = report.to_provenance()["pool"][0]
        for field in ("ticker", "market", "exchange", "sources", "observed_at",
                      "reasons", "freshness", "source_health", "score"):
            self.assertIn(field, entry)


class CliTest(unittest.TestCase):
    def run_cli(self, argv, client):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = pool_cli.main([*argv, "--no-store"], client=client)
        return code, buffer.getvalue()

    def test_json_output_and_exit_zero_when_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pool.json"
            code, printed = self.run_cli(
                ["--as-of", "2026-08-31T14:00:00Z", "--analysis-date", "2026-08-31",
                 "--top", "3", "--out", str(out), "--json"], FakeClient())
            payload = json.loads(printed)
            self.assertEqual(code, 0)
            self.assertTrue(payload["complete"])
            self.assertEqual(len(payload["pool"]), 3)
            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(written["tickers"]), 3)

    def test_degraded_run_exits_one_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, printed = self.run_cli(
                ["--as-of", "2026-08-31T14:00:00Z", "--top", "3",
                 "--out", str(Path(tmp) / "pool.json"), "--json"],
                FakeClient(movers=SourceFetchError("HTTP 500")))
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(printed)["complete"])

    def test_listing_failure_exits_two_without_writing_a_pool_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pool.json"
            code, _ = self.run_cli(
                ["--as-of", "2026-08-31T14:00:00Z", "--out", str(out), "--json"],
                FakeClient(listings={"nasdaq": SourceFetchError("HTTP 403")}))
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
