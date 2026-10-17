#!/usr/bin/env python3
"""tests for tg_bot/free_sources.py 免费搜索源(全部网络访问都是假的,不联网)"""

from urllib.parse import parse_qs, urlparse
import json
import unittest

from tg_bot import free_sources as fs


WIKI_JSON = json.dumps({"query": {"search": [
    {"title": "量子计算", "snippet": '<span class="searchmatch">量子</span>计算 是一种 &amp; 利用'},
    {"title": "Quantum computing", "snippet": "a <span>quantum</span> one"},
]}}, ensure_ascii=False)

GITHUB_JSON = json.dumps({"items": [
    {"full_name": "psf/requests", "html_url": "https://github.com/psf/requests",
     "description": "A simple HTTP library", "stargazers_count": 51000,
     "pushed_at": "2026-10-01T12:00:00Z"},
    {"full_name": "nodesc/x", "html_url": "https://github.com/nodesc/x",
     "description": None, "stargazers_count": 3, "pushed_at": "2025-01-02T00:00:00Z"},
]}, ensure_ascii=False)

HN_JSON = json.dumps({"hits": [
    {"title": "Show HN: a tool", "url": "https://example.com/tool", "objectID": "111",
     "points": 120, "num_comments": 45, "created_at": "2026-10-10T08:00:00.000Z"},
    {"title": "Ask HN: text only", "url": None, "objectID": "222",
     "points": 7, "num_comments": 2, "created_at": "2026-10-09T08:00:00.000Z"},
]}, ensure_ascii=False)


class FakeGet:
    """按 URL 路由返回预设正文;记录每次调用的 url 与 headers。"""

    def __init__(self, routes=None, default=None, error=None):
        self.routes = routes or {}
        self.default = default
        self.error = error
        self.calls = []

    def __call__(self, url, headers=None):
        self.calls.append((url, dict(headers or {})))
        if self.error is not None:
            raise self.error
        for key, body in self.routes.items():
            if key in url:
                return body
        if self.default is not None:
            return self.default
        raise AssertionError("unexpected url: " + url)

    def urls(self):
        return [url for url, _ in self.calls]


class WikiRowsTests(unittest.TestCase):
    def test_zh_query_uses_zh_wiki_and_parses_rows(self):
        get = FakeGet({"wikipedia": WIKI_JSON})
        rows = fs.wiki_rows("量子计算", get=get)
        self.assertEqual(len(rows), 2)
        qs = parse_qs(urlparse(get.urls()[0]).query)
        self.assertTrue(get.urls()[0].startswith("https://zh.wikipedia.org/w/api.php?"))
        self.assertEqual(qs["list"], ["search"])
        self.assertEqual(qs["srsearch"], ["量子计算"])
        self.assertEqual(qs["srlimit"], ["4"])
        self.assertEqual(qs["format"], ["json"])
        self.assertEqual(rows[0]["url"], "https://zh.wikipedia.org/wiki/%E9%87%8F%E5%AD%90%E8%AE%A1%E7%AE%97")
        self.assertEqual(rows[0]["title"], "量子计算")
        self.assertIsNone(rows[0]["published_at"])
        self.assertEqual(rows[0]["source"], "wikipedia")

    def test_en_query_uses_en_wiki(self):
        get = FakeGet({"wikipedia": WIKI_JSON})
        rows = fs.wiki_rows("quantum", get=get)
        self.assertTrue(get.urls()[0].startswith("https://en.wikipedia.org/w/api.php?"))
        self.assertEqual(rows[1]["url"], "https://en.wikipedia.org/wiki/Quantum%20computing")

    def test_snippet_html_stripped_and_unescaped(self):
        get = FakeGet({"wikipedia": WIKI_JSON})
        rows = fs.wiki_rows("量子计算", get=get)
        self.assertEqual(rows[0]["snippet"], "量子计算 是一种 & 利用")
        self.assertEqual(rows[1]["snippet"], "a quantum one")

    def test_failures_return_empty(self):
        self.assertEqual(fs.wiki_rows("x", get=FakeGet(error=OSError("down"))), [])
        self.assertEqual(fs.wiki_rows("x", get=FakeGet(default="not json")), [])
        self.assertEqual(fs.wiki_rows("x", get=FakeGet(default='{"query": 1}')), [])


class GithubRowsTests(unittest.TestCase):
    def test_parses_rows_with_headers(self):
        get = FakeGet({"api.github.com": GITHUB_JSON})
        rows = fs.github_rows("python http", get=get)
        url, headers = get.calls[0]
        qs = parse_qs(urlparse(url).query)
        self.assertEqual(qs["q"], ["python http"])
        self.assertEqual(qs["sort"], ["stars"])
        self.assertEqual(qs["per_page"], ["4"])
        self.assertEqual(headers.get("Accept"), "application/vnd.github+json")
        self.assertEqual(rows[0]["url"], "https://github.com/psf/requests")
        self.assertEqual(rows[0]["title"], "psf/requests")
        self.assertEqual(rows[0]["snippet"], "A simple HTTP library · ★51000 · 更新于2026-10-01")
        self.assertEqual(rows[0]["published_at"], "2026-10-01T12:00:00Z")
        self.assertEqual(rows[0]["source"], "github")
        self.assertEqual(rows[1]["snippet"], "★3 · 更新于2025-01-02")

    def test_token_adds_authorization_header(self):
        import os
        old = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "tok123"
        try:
            get = FakeGet({"api.github.com": GITHUB_JSON})
            fs.github_rows("python", get=get)
            self.assertEqual(get.calls[0][1].get("Authorization"), "Bearer tok123")
        finally:
            if old is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = old

    def test_no_token_no_authorization_header(self):
        import os
        old = os.environ.pop("GITHUB_TOKEN", None)
        try:
            get = FakeGet({"api.github.com": GITHUB_JSON})
            fs.github_rows("python", get=get)
            self.assertNotIn("Authorization", get.calls[0][1])
        finally:
            if old is not None:
                os.environ["GITHUB_TOKEN"] = old

    def test_failures_return_empty(self):
        self.assertEqual(fs.github_rows("x", get=FakeGet(error=TimeoutError())), [])
        self.assertEqual(fs.github_rows("x", get=FakeGet(default="<html>")), [])
        self.assertEqual(fs.github_rows("x", get=FakeGet(default='{"items": "bad"}')), [])


class HnRowsTests(unittest.TestCase):
    def test_parses_rows_and_fallback_url(self):
        get = FakeGet({"algolia": HN_JSON})
        rows = fs.hn_rows("rust async", get=get)
        qs = parse_qs(urlparse(get.urls()[0]).query)
        self.assertEqual(qs["tags"], ["story"])
        self.assertEqual(qs["hitsPerPage"], ["4"])
        self.assertEqual(rows[0]["url"], "https://example.com/tool")
        self.assertEqual(rows[0]["snippet"], "120 points · 45 评论")
        self.assertEqual(rows[0]["published_at"], "2026-10-10T08:00:00.000Z")
        self.assertEqual(rows[0]["source"], "hn")
        self.assertEqual(rows[1]["url"], "https://news.ycombinator.com/item?id=222")

    def test_failures_return_empty(self):
        self.assertEqual(fs.hn_rows("x", get=FakeGet(error=OSError())), [])
        self.assertEqual(fs.hn_rows("x", get=FakeGet(default="][")), [])
        self.assertEqual(fs.hn_rows("x", get=FakeGet(default='{"hits": null}')), [])


class FreeRowsRoutingTests(unittest.TestCase):
    def _routes(self, query, time_range):
        get = FakeGet(default='{"query": {"search": []}, "items": [], "hits": []}')
        fs.free_rows(query, time_range, get=get)
        return " ".join(get.urls())

    def test_chinese_any_includes_wiki(self):
        urls = self._routes("量子计算", "any")
        self.assertIn("wikipedia.org", urls)

    def test_day_excludes_wiki(self):
        urls = self._routes("量子计算", "day")
        self.assertNotIn("wikipedia.org", urls)

    def test_month_includes_wiki(self):
        self.assertIn("wikipedia.org", self._routes("量子计算", "month"))

    def test_pure_tech_term_includes_github_and_hn(self):
        urls = self._routes("python", "week")
        self.assertIn("api.github.com", urls)
        self.assertIn("hn.algolia.com", urls)

    def test_hn_skipped_on_day(self):
        urls = self._routes("python", "day")
        self.assertIn("api.github.com", urls)
        self.assertNotIn("hn.algolia.com", urls)

    def test_plain_chinese_news_skips_github_and_hn(self):
        urls = self._routes("今天股市", "day")
        self.assertNotIn("api.github.com", urls)
        self.assertNotIn("hn.algolia.com", urls)

    def test_plain_latin_phrase_does_not_trigger_github(self):
        urls = self._routes("Apple news", "week")
        self.assertNotIn("api.github.com", urls)
        self.assertNotIn("hn.algolia.com", urls)

    def test_merges_rows_from_all_routed_sources(self):
        get = FakeGet({"wikipedia": WIKI_JSON, "api.github.com": GITHUB_JSON,
                       "algolia": HN_JSON})
        rows = fs.free_rows("python", "any", get=get)
        sources = [row["source"] for row in rows]
        self.assertIn("github", sources)
        self.assertIn("hn", sources)
        self.assertIn("wikipedia", sources)

    def test_never_raises(self):
        self.assertEqual(fs.free_rows("python", "any", get=FakeGet(error=RuntimeError("x"))), [])
        self.assertEqual(fs.free_rows("", "any", get=FakeGet(default="{}")), [])
        self.assertEqual(fs.free_rows(None, None, get=FakeGet(default="{}")), [])


if __name__ == "__main__":
    unittest.main()
