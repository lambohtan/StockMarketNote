from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import prefetch_reddit as reddit_cli
import lean.reddit_prefetch as reddit_core
from lean.reddit_prefetch import (
    PrefetchReport,
    PoolInputError,
    PoolItem,
    RedditCache,
    RedditComment,
    RedditFetchError,
    RedditPost,
    RedditRSSClient,
    SearchOutcome,
    TickerResult,
    load_cached_reddit,
    load_pool,
    parse_comment_atom,
    parse_pool_payload,
    parse_search_atom,
    prefetch_reddit,
    select_comments,
    select_posts,
)


AS_OF = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)

SEARCH_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><category term="stocks"/><content type="html">&lt;p&gt;NVDA revenue and demand&lt;/p&gt;</content><id>t3_post1</id><link href="https://www.reddit.com/r/stocks/comments/post1/nvda_revenue/"/><updated>2026-08-31T12:00:00+00:00</updated><title>NVDA revenue outlook</title></entry>
  <entry><category term="investing"/><content type="html">&lt;p&gt;Nvidia valuation&lt;/p&gt;</content><id>t3_post2</id><link href="https://www.reddit.com/r/investing/comments/post2/nvidia_valuation/"/><updated>2026-08-31T11:00:00+00:00</updated><title>Is Nvidia fairly valued?</title></entry>
  <entry><category term="wallstreetbets"/><content type="html">&lt;p&gt;$NVDA earnings&lt;/p&gt;</content><id>t3_post3</id><link href="https://www.reddit.com/r/wallstreetbets/comments/post3/nvda_earnings/"/><updated>2026-08-31T10:00:00+00:00</updated><title>$NVDA earnings thread</title></entry>
</feed>"""

COMMENT_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><author><name>/u/original_poster</name></author><content type="html">&lt;p&gt;Original post body&lt;/p&gt;</content><id>t3_post1</id><link href="https://www.reddit.com/r/stocks/comments/post1/nvda_revenue/"/><updated>2026-08-31T12:00:00+00:00</updated><title>NVDA revenue outlook</title></entry>
  <entry><author><name>/u/analyst_one</name></author><content type="html">&lt;p&gt;Revenue growth was 22% because datacenter demand stayed strong.&lt;/p&gt;</content><id>t1_comment1</id><link href="https://www.reddit.com/r/stocks/comments/post1/nvda_revenue/comment1/"/><updated>2026-08-31T12:10:00+00:00</updated><title>/u/analyst_one on NVDA revenue outlook</title></entry>
  <entry><author><name>/u/AutoModerator</name></author><content type="html">&lt;p&gt;Please read the subreddit rules before commenting.&lt;/p&gt;</content><id>t1_comment2</id><link href="https://www.reddit.com/r/stocks/comments/post1/nvda_revenue/comment2/"/><updated>2026-08-31T12:11:00+00:00</updated><title>/u/AutoModerator on NVDA revenue outlook</title></entry>
</feed>"""


def make_post(post_id: str, subreddit: str = "stocks") -> RedditPost:
    return RedditPost(
        post_id, f"NVDA discussion {post_id}", subreddit,
        f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/topic/",
        "Nvidia revenue and valuation", "2026-08-31T12:00:00+00:00",
    )


def make_comment(post_id: str, number: int, body: str, author: str | None = None) -> RedditComment:
    return RedditComment(
        f"{post_id}-{number}", post_id, f"NVDA discussion {post_id}", "stocks",
        author or f"author{number}", body, f"https://reddit.test/{post_id}/{number}",
        "2026-08-31T12:10:00+00:00", number,
    )


class FakeClient:
    def __init__(self, posts, comments):
        self.posts, self.comments = posts, comments
        self.search_calls, self.comment_calls = [], []

    def search_posts(self, item, **_kwargs):
        self.search_calls.append(item.ticker)
        return self.posts

    def fetch_comments(self, post, **_kwargs):
        self.comment_calls.append(post.post_id)
        return self.comments[post.post_id]


class FakeHeaders(dict):
    def get(self, key, default=None):
        for existing, value in self.items():
            if existing.lower() == key.lower():
                return value
        return default


class FakeResponse:
    def __init__(self, body: bytes, headers=None):
        self.body, self.headers = body, FakeHeaders(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class RedditPrefetchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pool_input_aliases_dedup_and_validation(self):
        pool = parse_pool_payload({"tickers": [
            "amd", {"ticker": "NVDA", "aliases": ["Nvidia"]},
            {"ticker": "amd", "aliases": ["Advanced Micro Devices"]},
        ]})
        self.assertEqual([item.ticker for item in pool], ["AMD", "NVDA"])
        self.assertEqual(pool[0].aliases, ("Advanced Micro Devices",))
        path = self.temp / "pool.txt"
        path.write_text("AMD, NVDA\n# ignored\nMSFT\n", encoding="utf-8")
        self.assertEqual([item.ticker for item in load_pool(path)], ["AMD", "NVDA", "MSFT"])
        with self.assertRaises(PoolInputError):
            parse_pool_payload([])
        with self.assertRaises(PoolInputError):
            parse_pool_payload(["../../secret"])

    def test_atom_parsers_keep_identity_and_exclude_root(self):
        posts = parse_search_atom(SEARCH_ATOM)
        self.assertEqual([post.post_id for post in posts], ["post1", "post2", "post3"])
        self.assertEqual(posts[0].subreddit, "stocks")
        comments = parse_comment_atom(COMMENT_ATOM, posts[0])
        self.assertEqual([comment.comment_id for comment in comments], ["comment1", "comment2"])
        self.assertEqual(comments[0].author, "analyst_one")

    def test_post_selection_balances_subreddits_and_window(self):
        posts = parse_search_atom(SEARCH_ATOM)
        old = RedditPost(
            posts[0].post_id + "old", posts[0].title, posts[0].subreddit,
            posts[0].permalink, posts[0].body, "2026-08-29T12:00:00+00:00",
        )
        selected = select_posts(
            [*posts, old], PoolItem("NVDA", ("Nvidia",)),
            as_of=AS_OF, window_hours=24, max_posts=3,
        )
        self.assertEqual(
            {post.subreddit for post in selected}, {"stocks", "investing", "wallstreetbets"}
        )
        self.assertNotIn("post1old", {post.post_id for post in selected})

    def test_comment_selector_filters_and_balances(self):
        comments = {
            "p1": [
                make_comment("p1", 1, "Revenue rose 22% because datacenter demand stayed strong."),
                make_comment("p1", 2, "Revenue rose 22% because datacenter demand stayed strong."),
                make_comment("p1", 3, "[deleted]"),
                make_comment("p1", 4, "Read all community rules before posting here.", "AutoModerator"),
            ],
            "p2": [
                make_comment("p2", 1, "$NVDA valuation looks stretched after margin guidance declined."),
                make_comment("p2", 2, "Cash flow remains strong and debt is manageable for Nvidia."),
            ],
        }
        selected = select_comments(comments, PoolItem("NVDA", ("Nvidia",)), max_total=4)
        self.assertEqual(len(selected), 3)
        self.assertEqual({comment.post_id for comment in selected}, {"p1", "p2"})
        self.assertNotIn("AutoModerator", {comment.author for comment in selected})

    def test_prefetch_snapshot_and_read_only_loader(self):
        posts = [make_post("p1", "stocks"), make_post("p2", "investing"), make_post("p3", "wallstreetbets")]
        comments = {
            post.post_id: [
                make_comment(
                    post.post_id, index,
                    f"Nvidia {post.post_id} revenue growth was {20 + index}% because demand and margins improved {index}.",
                )
                for index in range(1, 6)
            ]
            for post in posts
        }
        cache_path = self.temp / "reddit.sqlite3"
        report = prefetch_reddit(
            [PoolItem("NVDA", ("Nvidia",))], client=FakeClient(posts, comments),
            cache=RedditCache(cache_path), as_of=AS_OF,
        )
        result = report.results[0]
        self.assertEqual((result.status, result.posts_fetched, result.comments_selected),
                         ("success", 3, 0))
        cache = RedditCache(cache_path)
        raw_row = cache.get_raw_snapshot("NVDA", "2026-08-31")
        raw_payload = json.loads(raw_row["payload_json"])
        self.assertEqual(raw_payload["schema_version"], 2)
        self.assertEqual(raw_payload["kind"], "reddit_raw")
        self.assertEqual(raw_payload["stats"]["comments_stored"], 15)
        self.assertEqual(raw_payload["stats"]["comments_selected"], 0)
        self.assertTrue(all("selected_comments" not in post for post in raw_payload["posts"]))
        self.assertEqual(sum(len(post["comments"]) for post in raw_payload["posts"]), 15)
        evidence = load_cached_reddit(
            "NVDA", "2026-08-31", cache_path=cache_path,
            max_age_hours=168, max_comments=9, now=AS_OF + timedelta(hours=1),
        )
        self.assertTrue(evidence.fresh)
        self.assertEqual(evidence.payload["selection"]["selected_count"], 9)
        self.assertIn("3 raw posts stored", evidence.text)
        self.assertIn("9 comments selected at read time", evidence.text)

    def test_failed_search_preserves_successful_snapshot(self):
        cache_path = self.temp / "reddit.sqlite3"
        cache, item = RedditCache(cache_path), PoolItem("NVDA")
        post = make_post("p1")
        good_client = FakeClient([post], {
            "p1": [make_comment("p1", 1, "Nvidia revenue rose because demand improved strongly.")]
        })
        prefetch_reddit([item], client=good_client, cache=cache, as_of=AS_OF)
        before = cache.get_raw_snapshot("NVDA", "2026-08-31")["payload_json"]

        class FailingClient:
            def search_posts(self, *_args, **_kwargs):
                raise RedditFetchError("429 Too Many Requests")

        report = prefetch_reddit(
            [item], client=FailingClient(), cache=cache, as_of=AS_OF, reuse_existing=False
        )
        self.assertEqual(report.results[0].status, "failed")
        row = cache.get_snapshot("NVDA", "2026-08-31")
        self.assertIsNone(row)
        raw_row = cache.get_raw_snapshot("NVDA", "2026-08-31")
        self.assertEqual(raw_row["status"], "success")
        self.assertEqual(raw_row["payload_json"], before)

    def test_prefetch_stores_all_raw_posts_without_selection(self):
        posts = [make_post("p1"), make_post("p2"), make_post("p3")]
        comments = {
            post.post_id: [
                make_comment(
                    post.post_id, 1,
                    "Nvidia revenue rose 25% because datacenter demand improved strongly.",
                )
            ]
            for post in posts
        }
        cache = RedditCache(self.temp / "reddit.sqlite3")
        with (
            patch.object(reddit_core, "select_posts", side_effect=AssertionError("prefetch selected posts")),
            patch.object(reddit_core, "_comments_in_window", side_effect=AssertionError("prefetch filtered comments")),
            patch.object(reddit_core, "select_comments", side_effect=AssertionError("prefetch selected comments")),
        ):
            report = prefetch_reddit(
                [PoolItem("NVDA")], client=FakeClient(posts, comments),
                cache=cache, as_of=AS_OF,
            )
        result = report.results[0]
        self.assertEqual(result.status, "success")
        self.assertEqual(result.posts_seen, 3)
        self.assertEqual(result.posts_fetched, 3)
        self.assertEqual(result.comments_selected, 0)
        payload = json.loads(cache.get_raw_snapshot("NVDA", "2026-08-31")["payload_json"])
        self.assertEqual({post["post_id"] for post in payload["posts"]}, {"p1", "p2", "p3"})
        self.assertEqual(payload["stats"]["comments_stored"], 3)

    def test_new_cli_defaults_use_week_raw_cache(self):
        defaults = reddit_cli.parse_args(["NVDA"])
        self.assertEqual(defaults.min_interval_seconds, 30.0)
        self.assertEqual(defaults.window_hours, 168)
        self.assertEqual(defaults.comments_per_post, 20)
        self.assertEqual(defaults.thread_cache_hours, 168.0)
        self.assertEqual(defaults.retention_days, 7)
        self.assertFalse(hasattr(defaults, "selected_comments"))

    def test_partial_run_does_not_replace_same_day_success(self):
        cache, item = RedditCache(self.temp / "reddit.sqlite3"), PoolItem("NVDA")
        posts = [make_post("p1"), make_post("p2"), make_post("p3")]
        comments = {
            post.post_id: [make_comment(
                post.post_id, 1,
                f"Nvidia {post.post_id} revenue rose because demand improved strongly."
            )]
            for post in posts
        }
        prefetch_reddit(
            [item], client=FakeClient(posts, comments), cache=cache, as_of=AS_OF
        )
        before = cache.get_raw_snapshot("NVDA", "2026-08-31")["payload_json"]

        class PartialClient(FakeClient):
            def search_posts(self, item, **_kwargs):
                self.search_calls.append(item.ticker)
                return SearchOutcome((posts[0],), ("r/investing: timed out",))

        report = prefetch_reddit(
            [item], client=PartialClient([posts[0]], {
                "p1": [make_comment(
                    "p1", 1, "Nvidia revenue rose because datacenter demand improved strongly."
                )]
            }), cache=cache, as_of=AS_OF, analysis_date="2026-08-31",
            reuse_existing=False,
        )
        self.assertEqual(report.results[0].status, "partial")
        row = cache.get_raw_snapshot("NVDA", "2026-08-31")
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["payload_json"], before)

    def test_same_as_of_resume_skips_completed_ticker(self):
        post = make_post("p1")
        comments = {
            "p1": [make_comment("p1", 1, "Nvidia revenue rose 25% because demand improved strongly.")]
        }
        cache = RedditCache(self.temp / "reddit.sqlite3")
        first_client = FakeClient([post], comments)
        prefetch_reddit(
            [PoolItem("NVDA", ("Nvidia",))], client=first_client, cache=cache,
            as_of=AS_OF,
        )
        second_client = FakeClient([post], comments)
        report = prefetch_reddit(
            [PoolItem("NVDA", ("Nvidia",))], client=second_client, cache=cache,
            as_of=AS_OF,
        )
        self.assertEqual(report.results[0].status, "cached")
        self.assertEqual(second_client.search_calls, [])
        self.assertEqual(second_client.comment_calls, [])

    def test_missing_and_stale_cache_are_explicit(self):
        cache_path = self.temp / "reddit.sqlite3"
        missing = load_cached_reddit("AMD", "2026-08-31", cache_path=cache_path, now=AS_OF)
        self.assertEqual(missing.status, "missing")
        self.assertIn("No live fallback", missing.text)
        self.assertFalse(cache_path.exists())
        cache = RedditCache(cache_path)
        payload = {
            "schema_version": 1, "ticker": "AMD", "as_of": AS_OF.isoformat(),
            "fetched_at": (AS_OF - timedelta(hours=30)).isoformat(), "status": "empty",
            "errors": [],
            "stats": {"posts_seen": 0, "posts_fetched": 0, "comments_seen": 0, "comments_selected": 0},
            "posts": [],
        }
        cache.put_snapshot(PoolItem("AMD"), "2026-08-31", "empty", payload)
        stale = load_cached_reddit(
            "AMD", "2026-08-31", cache_path=cache_path, max_age_hours=24, now=AS_OF,
        )
        self.assertEqual(stale.status, "stale")
        self.assertIn("STALE", stale.text)

    def test_analysis_date_is_independent_from_utc_boundary(self):
        cache_path = self.temp / "reddit.sqlite3"
        as_of = datetime(2026, 8, 31, 7, 30, tzinfo=timezone.utc)
        post = RedditPost(
            "p1", "NVDA discussion", "stocks",
            "https://www.reddit.com/r/stocks/comments/p1/topic/", "Nvidia revenue",
            "2026-08-31T07:00:00+00:00",
        )
        report = prefetch_reddit(
            [PoolItem("NVDA")], client=FakeClient([post], {
                "p1": [RedditComment(
                    "c1", "p1", post.title, "stocks", "analyst",
                    "Nvidia revenue rose because datacenter demand improved strongly.",
                    "https://reddit.test/p1/c1", "2026-08-31T07:10:00+00:00",
                )]
            }), cache=RedditCache(cache_path), as_of=as_of,
            analysis_date="2026-08-30",
        )
        self.assertEqual(report.analysis_date, "2026-08-30")
        evidence = load_cached_reddit(
            "NVDA", "2026-08-30", cache_path=cache_path,
            now=as_of + timedelta(minutes=5), max_comments=1,
        )
        self.assertEqual(evidence.status, "success")

    def test_loader_selects_at_most_24_from_all_raw_posts(self):
        posts = [make_post(f"p{index}") for index in range(1, 13)]
        comments = {
            post.post_id: [
                make_comment(
                    post.post_id,
                    number,
                    (
                        f"Nvidia {post.post_id} scenario {number} revenue changed "
                        f"{number * 7}% because catalyst code {post.post_id}-{number} "
                        "affected demand margins cashflow and valuation."
                    ),
                    author=f"{post.post_id}-author-{number}",
                )
                for number in range(1, 4)
            ]
            for post in posts
        }
        cache_path = self.temp / "reddit.sqlite3"
        report = prefetch_reddit(
            [PoolItem("NVDA", ("Nvidia",))], client=FakeClient(posts, comments),
            cache=RedditCache(cache_path), as_of=AS_OF,
        )
        self.assertEqual(report.results[0].comments_selected, 0)
        evidence = load_cached_reddit(
            "NVDA", "2026-08-31", cache_path=cache_path,
            max_comments=24, now=AS_OF + timedelta(hours=1),
        )
        self.assertEqual(evidence.payload["selection"]["selected_count"], 24)
        selected_post_ids = {
            comment["post_id"] for comment in evidence.payload["selection"]["comments"]
        }
        self.assertIn("p11", selected_post_ids)
        self.assertIn("p12", selected_post_ids)

    def test_raw_cache_purge_and_query_hash(self):
        cache = RedditCache(self.temp / "reddit.sqlite3")
        post = make_post("p1")
        comments = {
            "p1": [make_comment(
                "p1", 1, "Nvidia revenue rose because demand improved strongly."
            )]
        }
        first_client = FakeClient([post], comments)
        prefetch_reddit(
            [PoolItem("NVDA")], client=first_client,
            cache=cache, as_of=AS_OF, comments_per_post=20,
        )
        smaller_limit_client = FakeClient([post], comments)
        prefetch_reddit(
            [PoolItem("NVDA")], client=smaller_limit_client,
            cache=cache, as_of=AS_OF, comments_per_post=10, reuse_existing=False,
        )
        larger_limit_client = FakeClient([post], comments)
        prefetch_reddit(
            [PoolItem("NVDA")], client=larger_limit_client,
            cache=cache, as_of=AS_OF, comments_per_post=30, reuse_existing=False,
        )
        self.assertEqual(smaller_limit_client.comment_calls, [])
        self.assertEqual(larger_limit_client.comment_calls, ["p1"])
        with cache.session() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM raw_snapshots WHERE ticker = 'NVDA'"
            ).fetchone()[0]
            connection.execute(
                "UPDATE raw_snapshots SET fetched_at = ? WHERE ticker = 'NVDA'",
                (AS_OF.isoformat(),),
            )
        at_boundary = load_cached_reddit(
            "NVDA", "2026-08-31", cache_path=cache.path,
            now=AS_OF + timedelta(hours=168), max_comments=1,
        )
        just_expired = load_cached_reddit(
            "NVDA", "2026-08-31", cache_path=cache.path,
            now=AS_OF + timedelta(hours=168, seconds=1), max_comments=1,
        )
        self.assertTrue(at_boundary.fresh)
        self.assertEqual(just_expired.status, "stale")
        with cache.session() as connection:
            connection.execute(
                "UPDATE raw_snapshots SET fetched_at = ? WHERE ticker = 'NVDA'",
                ((AS_OF - timedelta(days=8)).isoformat(),),
            )
            connection.execute(
                "UPDATE thread_cache SET fetched_at = ?",
                ((AS_OF - timedelta(days=8)).isoformat(),),
            )
        self.assertEqual(count, 3)
        cache.purge(before=AS_OF - timedelta(days=7))
        self.assertIsNone(cache.get_raw_snapshot("NVDA", "2026-08-31"))
        with cache.session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM thread_cache").fetchone()[0], 0)

    def test_legacy_v1_snapshot_is_explicit(self):
        cache_path = self.temp / "reddit.sqlite3"
        post = make_post("p1")
        comment = make_comment(
            "p1", 1, "Nvidia revenue rose because demand improved strongly."
        )
        payload = {
            "schema_version": 1, "ticker": "NVDA", "aliases": [],
            "as_of": AS_OF.isoformat(), "fetched_at": AS_OF.isoformat(),
            "status": "success", "errors": [],
            "stats": {"posts_seen": 1, "posts_fetched": 1, "comments_seen": 1,
                      "comments_selected": 1},
            "posts": [{**post.__dict__, "selected_comments": [comment.__dict__]}],
        }
        connection = sqlite3.connect(cache_path)
        try:
            connection.execute("""CREATE TABLE ticker_snapshots (
                ticker TEXT NOT NULL, as_of_date TEXT NOT NULL, fetched_at TEXT NOT NULL,
                status TEXT NOT NULL, posts_seen INTEGER NOT NULL, comments_seen INTEGER NOT NULL,
                comments_selected INTEGER NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY (ticker, as_of_date)
            )""")
            connection.execute(
                "INSERT INTO ticker_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "NVDA", "2026-08-31", AS_OF.isoformat(), "success", 1, 1, 1,
                    json.dumps(payload),
                ),
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()
        cache = RedditCache(cache_path)
        with cache.session() as migrated:
            self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertIsNotNone(cache.get_snapshot("NVDA", "2026-08-31"))
        evidence = load_cached_reddit(
            "NVDA", "2026-08-31", cache_path=cache_path,
            now=AS_OF + timedelta(hours=1), max_comments=24,
        )
        self.assertEqual(evidence.status, "legacy")
        self.assertIn("Legacy schema v1 fallback", evidence.text)
        self.assertEqual(evidence.payload["selection"]["selected_count"], 1)

    def test_json_summary_stdout_is_pure_json(self):
        report = PrefetchReport(
            run_id="reddit-test", analysis_date="2026-08-31",
            as_of=AS_OF.isoformat(), started_at=AS_OF.isoformat(),
            finished_at=AS_OF.isoformat(),
            results=[TickerResult("NVDA", "success", 3, 3, 20, 10)],
        )

        def fake_prefetch(*_args, **kwargs):
            kwargs["progress"]("NVDA: test progress")
            return report

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(reddit_cli, "RedditCache"),
            patch.object(reddit_cli, "RedditRSSClient"),
            patch.object(reddit_cli, "prefetch_reddit", side_effect=fake_prefetch),
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            exit_code = reddit_cli.main([
                "NVDA", "--analysis-date", "2026-08-31", "--as-of", AS_OF.isoformat(),
                "--json-summary", "--cache", str(self.temp / "reddit.sqlite3"),
            ])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["run_id"], "reddit-test")
        self.assertIn("test progress", stderr.getvalue())

    def test_rate_gate_uses_reset_and_persistent_state(self):
        current, sleeps = [100.0], []

        def sleeper(seconds):
            sleeps.append(seconds)
            current[0] += seconds

        responses = iter([
            FakeResponse(b"first", {"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "46"}),
            FakeResponse(b"second", {"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "2"}),
        ])
        client = RedditRSSClient(
            min_interval_seconds=10, lock_path=self.temp / "rate.lock",
            state_path=self.temp / "rate.json", opener=lambda *_a, **_k: next(responses),
            sleeper=sleeper, clock=lambda: current[0],
        )
        self.assertEqual(client.get("https://reddit.test/one"), b"first")
        self.assertEqual(client.get("https://reddit.test/two"), b"second")
        self.assertEqual(sleeps, [48.0])

    def test_search_query_includes_pool_aliases(self):
        urls = []

        def opener(request, **_kwargs):
            urls.append(request.full_url)
            return FakeResponse(SEARCH_ATOM)

        client = RedditRSSClient(
            min_interval_seconds=0, lock_path=self.temp / "rate.lock",
            state_path=self.temp / "rate.json", opener=opener,
        )
        outcome = client.search_posts(
            PoolItem("NKE", ("Nike",)), subreddits=("stocks",), window_hours=24,
        )
        self.assertEqual(len(outcome.posts), 3)
        self.assertIn("NKE+OR+Nike", urls[0])

    def test_rate_gate_retries_429_once(self):
        current = [0.0]
        error = HTTPError(
            "https://reddit.test", 429, "Too Many Requests",
            FakeHeaders({"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "5"}), None,
        )
        responses = iter([error, FakeResponse(b"ok")])

        def opener(*_args, **_kwargs):
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        def sleeper(seconds):
            current[0] += seconds

        client = RedditRSSClient(
            min_interval_seconds=1, lock_path=self.temp / "rate.lock",
            state_path=self.temp / "rate.json", opener=opener,
            sleeper=sleeper, clock=lambda: current[0],
        )
        self.assertEqual(client.get("https://reddit.test"), b"ok")
        self.assertEqual(current[0], 7.0)


if __name__ == "__main__":
    unittest.main()
