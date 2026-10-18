#!/usr/bin/env python3
"""tests for tg_bot/deepseek_search.py (全部网络访问都是假的,不联网)"""

from datetime import date
import json
import os
import unittest
from unittest.mock import patch

from tg_bot import deepseek_search as ds


SEARCH_OK = {"results": [
    {"url": "https://a.test/1", "title": "A", "snippet": "short snip",
     "summary": "S" * 700, "date": "2026-10-15T08:00:00Z", "authority": 3,
     "rerank_score": 2.4, "content": "", "site_name": "a.test"},
    {"url": "https://b.test/2", "title": "B", "snippet": "only snippet",
     "summary": "", "date": None, "authority": -1, "rerank_score": 0.5},
    {"url": "https://c.test/3", "title": "C", "snippet": "no score",
     "summary": "", "date": None},
    {"url": "https://d.test/4", "title": "D", "snippet": "none score",
     "summary": "", "date": None, "rerank_score": None},
    {"url": "https://e.test/5", "title": "E", "snippet": "low", "summary": "",
     "rerank_score": 1.0},
    {"url": "", "title": "no url", "summary": "x", "rerank_score": 2},
    {"url": "https://f.test/6", "title": "", "summary": "x", "rerank_score": 2},
]}


class FakePost:
    """按顺序返回预设的 (status, text);记录每次调用的 url / payload / key。"""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def __call__(self, url, payload, key):
        self.calls.append((url, payload, key))
        if self.error is not None:
            raise self.error
        if not self.responses:
            return 500, ""
        return self.responses.pop(0)


def _ok(data):
    return 200, json.dumps(data, ensure_ascii=False)


class DeepseekSearchTests(unittest.TestCase):
    def setUp(self):
        ds._state.update(last=None, calls=0, next_key=0)
        env = patch.dict(os.environ, {"DEEPSEEK_KEY_0": "k0-test", "DEEPSEEK_KEY_1": "k1-test"})
        env.start()
        self.addCleanup(env.stop)
        sleep = patch.object(ds.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def test_rows_map_summary_first_and_keep_authority_and_score(self):
        post = FakePost([_ok(SEARCH_OK)])
        rows = ds.ds_search_rows("query", "any", post=post)
        self.assertEqual([r["url"] for r in rows], ["https://a.test/1", "https://c.test/3",
                                                    "https://d.test/4", "https://e.test/5"])
        first = rows[0]
        self.assertEqual(first["title"], "A")
        self.assertEqual(first["snippet"], "S" * 600)
        self.assertEqual(first["published_at"], "2026-10-15T08:00:00Z")
        self.assertEqual(first["source"], "deepseek")
        self.assertEqual(first["authority"], 3)
        self.assertEqual(first["rerank_score"], 2.4)
        self.assertEqual(rows[1]["snippet"], "no score")  # no summary -> snippet
        self.assertNotIn("authority", rows[1])
        self.assertEqual(set(rows[0]), {"url", "title", "snippet", "published_at",
                                        "source", "authority", "rerank_score"})

    def test_rerank_filter_drops_below_one_and_keeps_missing_score(self):
        rows = ds.ds_search_rows("query", "any", post=FakePost([_ok(SEARCH_OK)]))
        urls = {r["url"] for r in rows}
        self.assertNotIn("https://b.test/2", urls)
        self.assertIn("https://e.test/5", urls)  # rerank 1.0 is kept (>= 1.0)
        self.assertIn("https://c.test/3", urls)
        self.assertIn("https://d.test/4", urls)

    def test_request_body_and_headers_use_search_endpoint(self):
        post = FakePost([_ok({"results": []})])
        ds.ds_search_rows("query", "any", post=post, count=5)
        url, payload, key = post.calls[0]
        self.assertEqual(url, "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(payload, {"query": "query", "count": 5, "need_content": False})
        self.assertEqual(key, "k0-test")

    def test_time_range_maps_to_after_operator(self):
        today = date(2026, 10, 18)
        self.assertEqual(ds.build_query("新闻", "day", today=today), "新闻 after:2026-10-16")
        self.assertEqual(ds.build_query("news", "week", today=today), "news after:2026-10-10")
        self.assertEqual(ds.build_query("news", "month", today=today), "news after:2026-09-16")
        self.assertEqual(ds.build_query("news", "any", today=today), "news")

    def test_time_range_operator_is_sent_in_search_payload(self):
        post = FakePost([_ok({"results": []})])
        ds.ds_search_rows("ai chips", "day", post=post)
        query = post.calls[0][1]["query"]
        self.assertTrue(query.startswith("ai chips after:"), query)
        self.assertRegex(query, r" after:\d{4}-\d{2}-\d{2}$")

    def test_long_query_is_truncated_to_128_including_operator(self):
        long_query = "x" * 200
        today = date(2026, 10, 18)
        built = ds.build_query(long_query, "day", today=today)
        self.assertLessEqual(len(built), 128)
        self.assertTrue(built.endswith(" after:2026-10-16"))
        plain = ds.build_query(long_query, "any", today=today)
        self.assertEqual(len(plain), 128)

    def test_429_waits_then_retries_once(self):
        post = FakePost([(429, ""), _ok(SEARCH_OK)])
        rows = ds.ds_search_rows("query", "any", post=post)
        self.assertEqual(len(post.calls), 2)
        self.assertIn(1.5, [c.args[0] for c in self.sleep.call_args_list])
        self.assertTrue(rows)

    def test_429_twice_gives_up_with_empty_result(self):
        post = FakePost([(429, ""), (429, "")])
        self.assertEqual(ds.ds_search_rows("query", "any", post=post), [])
        self.assertEqual(len(post.calls), 2)

    def test_missing_keys_skip_source_without_calling_post(self):
        post = FakePost([_ok(SEARCH_OK)])
        with patch.dict(os.environ, {"DEEPSEEK_KEY_0": "", "DEEPSEEK_KEY_1": ""}):
            self.assertEqual(ds.ds_search_rows("query", "any", post=post), [])
            self.assertEqual(ds.ds_fetch("https://a.test/1", post=post), "")
        self.assertEqual(post.calls, [])

    def test_keys_rotate_between_requests(self):
        post = FakePost([_ok({"results": []})] * 2)
        ds.ds_search_rows("q", "any", post=post)
        ds.ds_search_rows("q", "any", post=post)
        self.assertEqual([c[2] for c in post.calls], ["k0-test", "k1-test"])

    def test_post_exception_returns_empty(self):
        self.assertEqual(ds.ds_search_rows("q", "any", post=FakePost(error=OSError("net"))), [])

    def test_non_200_and_bad_json_return_empty(self):
        self.assertEqual(ds.ds_search_rows("q", "any", post=FakePost([(500, "boom")])), [])
        self.assertEqual(ds.ds_search_rows("q", "any", post=FakePost([(200, "not json")])), [])
        self.assertEqual(ds.ds_search_rows("q", "any", post=FakePost([(200, "[]")])), [])

    def test_empty_query_returns_empty_without_request(self):
        post = FakePost([_ok(SEARCH_OK)])
        self.assertEqual(ds.ds_search_rows("   ", "any", post=post), [])
        self.assertEqual(post.calls, [])

    def test_requests_are_spaced_at_least_quarter_second(self):
        clock = iter([100.0, 100.05, 100.05])
        with patch.object(ds.time, "monotonic", side_effect=lambda: next(clock)):
            post = FakePost([_ok({"results": []}), _ok({"results": []})])
            ds.ds_search_rows("q", "any", post=post)
            ds.ds_search_rows("q", "any", post=post)
        waits = [c.args[0] for c in self.sleep.call_args_list if c.args[0] != 1.5]
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], 0.2, places=6)

    def test_pop_call_count_counts_successes_and_resets(self):
        post = FakePost([_ok({"results": []}), (500, ""), _ok({"results": []})])
        for _ in range(3):
            ds.ds_search_rows("q", "any", post=post)
        self.assertEqual(ds.pop_call_count(), 2)
        self.assertEqual(ds.pop_call_count(), 0)


class DeepseekFetchTests(unittest.TestCase):
    def setUp(self):
        ds._state.update(last=None, calls=0, next_key=0)
        env = patch.dict(os.environ, {"DEEPSEEK_KEY_0": "k0-test"})
        env.start()
        self.addCleanup(env.stop)
        sleep = patch.object(ds.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_fetch_returns_markdown_content(self):
        post = FakePost([_ok({"title": "T", "content": "# 标题\n\n正文"})])
        self.assertEqual(ds.ds_fetch("https://a.test/1", post=post), "# 标题\n\n正文")
        url, payload, key = post.calls[0]
        self.assertEqual(url, "https://api.deepseek.com/v1/tools/fetch")
        self.assertEqual(payload, {"url": "https://a.test/1"})
        self.assertEqual(key, "k0-test")

    def test_fetch_404_returns_empty(self):
        self.assertEqual(ds.ds_fetch("https://a.test/1", post=FakePost([(404, "")])), "")

    def test_fetch_empty_or_missing_content_returns_empty(self):
        self.assertEqual(ds.ds_fetch("https://a.test/1", post=FakePost([_ok({"content": "  "})])), "")
        self.assertEqual(ds.ds_fetch("https://a.test/1", post=FakePost([_ok({"title": "T"})])), "")

    def test_fetch_exception_returns_empty(self):
        self.assertEqual(ds.ds_fetch("https://a.test/1", post=FakePost(error=RuntimeError("x"))), "")


if __name__ == "__main__":
    unittest.main()
