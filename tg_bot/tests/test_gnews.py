#!/usr/bin/env python3
"""tests for tg_bot/gnews.py Google 新闻 RSS 提供方(全部网络访问都是假的,不联网)"""

from urllib.parse import parse_qs, urlparse
import unittest

from tg_bot import gnews as gn

ARTICLE_HTML = '<c-wiz data-n-a-sg="SIG" data-n-a-ts="123"></c-wiz>'
RSS_ITEMS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>Apple 发布新品 - 新华社</title><link>https://news.google.com/rss/articles/CBMi1?oc=5</link>
<pubDate>Thu, 15 Oct 2026 08:30:00 GMT</pubDate><source url="https://xinhuanet.com">新华社</source></item>
<item><title>Second story - Reuters</title><link>https://news.google.com/rss/articles/CBMi2?oc=5</link>
<pubDate>bogus date</pubDate><source url="https://reuters.com">Reuters</source></item>
<item><title>Third story - BBC</title><link>https://news.google.com/rss/articles/CBMi3?oc=5</link>
<pubDate>Fri, 16 Oct 2026 01:00:00 +0800</pubDate><source url="https://bbc.com">BBC</source></item>
</channel></rss>"""


def batch_body(url):
    # 与真实 batchexecute 响应一致:garturlres\",\"<url>\",1]
    return 'garturlres\\",\\"' + url + '\\",1]'


class FakeWeb:
    """假的 get/post:RSS 搜索返回 rss,文章页返回带签名的 html,post 按调用次数造跳转 URL。"""

    def __init__(self, rss=RSS_ITEMS, post_body=None):
        self.rss = rss
        self.post_body = post_body or (lambda n: batch_body(f"https://pub.example/{n}"))
        self.gets = []
        self.posts = []

    def get(self, url, timeout=15):
        self.gets.append(url)
        return self.rss if "/rss/search" in url else ARTICLE_HTML

    def post(self, url, data, timeout=15):
        self.posts.append(data)
        return self.post_body(len(self.posts))


class GnewsTests(unittest.TestCase):

    def test_build_rss_url_time_range_and_locale(self):
        for time_range, suffix in [("day", " when:1d"), ("week", " when:7d"),
                                   ("month", " when:30d"), ("any", "")]:
            with self.subTest(time_range=time_range):
                q = parse_qs(urlparse(gn.build_rss_url("Apple news", time_range)).query)["q"][0]
                self.assertEqual(q, "Apple news" + suffix)
        self.assertEqual(gn.build_rss_url("Apple news", "any"),
                         "https://news.google.com/rss/search?q=Apple%20news&hl=en-US&gl=US&ceid=US:en")
        zh = parse_qs(urlparse(gn.build_rss_url("iPhone 发售", "week")).query)
        self.assertEqual(zh["hl"], ["zh-CN"])
        self.assertEqual(zh["gl"], ["CN"])
        self.assertEqual(zh["ceid"], ["CN:zh-Hans"])
        self.assertEqual(zh["q"], ["iPhone 发售 when:7d"])

    def test_parse_rss_fields_suffix_and_dates(self):
        rows = gn.parse_rss(RSS_ITEMS)
        self.assertEqual(len(rows), 3)
        first = rows[0]
        self.assertEqual(first["title"], "Apple 发布新品")  # " - 新华社" 尾缀已去掉
        self.assertEqual(first["source"], "新华社")
        self.assertEqual(first["source_url"], "https://xinhuanet.com")
        self.assertEqual(first["google_link"], "https://news.google.com/rss/articles/CBMi1?oc=5")
        self.assertEqual(first["published_at"], "2026-10-15T08:30:00+00:00")
        self.assertIsNone(rows[1]["published_at"])  # 坏日期 -> None
        # +0800 换算为 UTC
        self.assertEqual(rows[2]["published_at"], "2026-10-15T17:00:00+00:00")

    def test_parse_rss_bad_xml_returns_empty(self):
        self.assertEqual(gn.parse_rss("<rss><channel><item>"), [])
        self.assertEqual(gn.parse_rss("not xml at all"), [])

    def test_decode_link_success_and_unescape(self):
        web = FakeWeb(post_body=lambda n: batch_body("https://x.example/a\\u003db"))
        url = gn.decode_link("https://news.google.com/rss/articles/CBMi9?oc=5", get=web.get, post=web.post)
        self.assertEqual(url, "https://x.example/a=b")  # = 被还原为 =
        self.assertEqual(web.gets[0], "https://news.google.com/rss/articles/CBMi9?hl=en-US&gl=US&ceid=US:en")
        self.assertIn(b"f.req=", web.posts[0])

    def test_decode_link_failures_return_none(self):
        link = "https://news.google.com/rss/articles/CBMi9?oc=5"

        def no_sig_get(url, timeout=15):
            return "<html>no signature here</html>"

        def boom_get(url, timeout=15):
            raise OSError("network down")

        web = FakeWeb(post_body=lambda n: "no url in this body")
        with self.subTest("missing signature"):
            self.assertIsNone(gn.decode_link(link, get=no_sig_get, post=web.post))
        with self.subTest("exception"):
            self.assertIsNone(gn.decode_link(link, get=boom_get, post=web.post))
        with self.subTest("body without garturlres"):
            self.assertIsNone(gn.decode_link(link, get=web.get, post=web.post))

    def test_gnews_rows_row_format(self):
        web = FakeWeb()
        sleeps = []
        rows = gn.gnews_rows("Apple news", "week", get=web.get, post=web.post, sleep=sleeps.append)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], {
            "url": "https://pub.example/1",
            "title": "Apple 发布新品",
            "snippet": "新华社 · Apple 发布新品",
            "published_at": "2026-10-15T08:30:00+00:00",
            "source": "gnews",
        })
        self.assertEqual(sleeps, [0.15, 0.15])  # 两次解码之间各停一次
        self.assertIn("when:7d", parse_qs(urlparse(web.gets[0]).query)["q"][0])

    def test_gnews_rows_stops_after_three_failures(self):
        web = FakeWeb(post_body=lambda n: "no url in this body")
        rows = gn.gnews_rows("Apple news", get=web.get, post=web.post, sleep=lambda s: None)
        self.assertEqual(rows, [])
        self.assertEqual(len(web.posts), 3)  # 连续 3 次失败后不再解码第 4 条

    def test_gnews_rows_cache_hit_and_max_items(self):
        web = FakeWeb()
        cache = {"https://news.google.com/rss/articles/CBMi1?oc=5": "https://cached.example/1"}
        rows = gn.gnews_rows("Apple news", get=web.get, post=web.post,
                             sleep=lambda s: None, cache=cache, max_items=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["url"], "https://cached.example/1")  # 命中缓存,不解码
        self.assertEqual(len(web.posts), 1)  # 只为第 2 条发起一次解码
        self.assertNotIn("CBMi1", " ".join(web.gets[1:]))
        self.assertNotIn("CBMi3", " ".join(web.gets))  # max_items=2 时第 3 条不请求
        self.assertEqual(cache["https://news.google.com/rss/articles/CBMi2?oc=5"], "https://pub.example/1")


if __name__ == "__main__":
    unittest.main()
