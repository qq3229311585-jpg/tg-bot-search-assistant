#!/usr/bin/env python3
"""tests for tg_bot/muse_search.py 纯函数积木"""

from datetime import datetime, timezone
import json
import unittest

from tg_bot import muse_search as ms

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _q(value):
    return json.dumps({"q": value}, ensure_ascii=False)


class ValidateRequestTests(unittest.TestCase):
    def test_validate_request_ok(self):
        self.assertEqual(ms.validate_request(_q("  今天的新闻  ")), ("今天的新闻", ""))

    def test_reject_ctrl_chars(self):
        for value in ("a\nb c", "a\tb c", "a\x00b c"):
            with self.subTest(value=repr(value)):
                q, err = ms.validate_request(_q(value))
                self.assertIsNone(q)
                self.assertTrue(err)

    def test_reject_oversize(self):
        raw = b'{"q":"' + b"a" * 4090 + b'"}'
        self.assertGreater(len(raw), ms.MAX_STDIN_BYTES)
        q, err = ms.validate_request(raw)
        self.assertIsNone(q)
        self.assertTrue(err)

    def test_reject_extra_keys(self):
        q, err = ms.validate_request('{"q": "abc", "x": 1}')
        self.assertIsNone(q)
        self.assertTrue(err)

    def test_reject_non_json(self):
        q, err = ms.validate_request("not json")
        self.assertIsNone(q)
        self.assertTrue(err)
        q, err = ms.validate_request(b"\xff\xfe\xfa")
        self.assertIsNone(q)
        self.assertTrue(err)

    def test_reject_short_or_long_q(self):
        for value in ("a", " a ", "a" * 301):
            with self.subTest(length=len(value)):
                q, err = ms.validate_request(_q(value))
                self.assertIsNone(q)
                self.assertTrue(err)
        q, err = ms.validate_request(_q("a" * 300))
        self.assertEqual((q, err), ("a" * 300, ""))

    def test_reject_non_string_q(self):
        q, err = ms.validate_request('{"q": 123}')
        self.assertIsNone(q)
        self.assertTrue(err)

    def test_nfkc_applied(self):
        q, err = ms.validate_request(_q("ＡＢＣ１２３"))
        self.assertEqual((q, err), ("ABC123", ""))


class ParseRewriteTests(unittest.TestCase):
    def test_parse_rewrite_defaults(self):
        self.assertEqual(ms.parse_rewrite(None, "原问题"), {
            "queries": ["原问题"], "time_range": "any", "must_terms": [],
        })

    def test_parse_rewrite_keeps_original_first(self):
        result = ms.parse_rewrite({
            "queries": ["甲", "原问题", "乙"],
            "time_range": "week",
            "must_terms": ["芯片"],
        }, "原问题")
        self.assertEqual(result["queries"], ["原问题", "甲", "乙"])
        self.assertEqual(result["time_range"], "week")
        self.assertEqual(result["must_terms"], ["芯片"])

    def test_parse_rewrite_bad_time_range(self):
        result = ms.parse_rewrite({"queries": [], "time_range": "year"}, "原问题")
        self.assertEqual(result["time_range"], "any")

    def test_parse_rewrite_caps_queries(self):
        raw = {"queries": ["a1", "a1", " ", "b" * 121, "a2", "a3", "a4", "a5", 7]}
        result = ms.parse_rewrite(raw, "原问题")
        self.assertEqual(len(result["queries"]), 4)
        self.assertEqual(result["queries"][0], "原问题")
        self.assertEqual(result["queries"], ["原问题", "a1", "a2", "a3"])


class RankCandidatesTests(unittest.TestCase):
    def test_rank_caps_per_site(self):
        cands = [
            {"title": "1", "url": "https://k.sina.cn/1", "summary": "", "published_at": None},
            {"title": "2", "url": "https://finance.sina.com.cn/2", "summary": "", "published_at": None},
            {"title": "3", "url": "https://k.sina.cn/3", "summary": "", "published_at": None},
            {"title": "4", "url": "https://other.test/4", "summary": "", "published_at": None},
        ]
        result = ms.rank_candidates(cands, [], "any", now=NOW, per_site=2, top=6)
        sina = [r for r in result if r["site"] == "sina.com.cn"]
        self.assertEqual(len(sina), 2)
        self.assertIn("https://other.test/4", [r["url"] for r in result])

    def test_rank_prefers_authority_and_hits(self):
        cands = [
            {"title": "blog", "url": "https://blog.test/y", "summary": "", "published_at": None},
            {"title": "AI", "url": "https://other.test/z", "summary": "chip", "published_at": None},
            {"title": "AI chip news", "url": "https://reuters.com/x", "summary": "", "published_at": None},
        ]
        result = ms.rank_candidates(cands, ["AI", "chip"], "any", now=NOW)
        self.assertEqual([r["url"] for r in result], [
            "https://reuters.com/x", "https://other.test/z", "https://blog.test/y",
        ])
        self.assertAlmostEqual(result[0]["score"], 3 + 2 * 1.0 + 0.2)
        self.assertEqual(result[0]["site"], "reuters.com")

    def test_rank_freshness_unknown_lower(self):
        cands = [
            {"title": "t", "url": "https://a.test/1", "summary": "", "published_at": None},
            {"title": "t", "url": "https://b.test/2", "summary": "", "published_at": "2026-10-16T08:00:00Z"},
        ]
        result = ms.rank_candidates(cands, [], "day", now=NOW)
        self.assertEqual(result[0]["url"], "https://b.test/2")
        self.assertAlmostEqual(result[0]["score"], 1.0 + (1 - 4 / 24))
        self.assertAlmostEqual(result[1]["score"], 1.0 + 0.2)

    def test_rank_relative_time_parsing(self):
        cands = [
            {"title": "t", "url": "https://a.test/1", "summary": "", "published_at": "3 hours ago"},
            {"title": "t", "url": "https://b.test/2", "summary": "", "published_at": "2天前"},
        ]
        result = ms.rank_candidates(cands, [], "week", now=NOW)
        by_url = {r["url"]: r["score"] for r in result}
        self.assertAlmostEqual(by_url["https://a.test/1"], 1.0 + (1 - 3 / 168))
        self.assertAlmostEqual(by_url["https://b.test/2"], 1.0 + (1 - 48 / 168))
        self.assertEqual(result[0]["url"], "https://a.test/1")

    def test_rank_relative_time_outside_window_scores_zero_freshness(self):
        cands = [{"title": "t", "url": "https://a.test/1", "summary": "", "published_at": "2天前"}]
        result = ms.rank_candidates(cands, [], "day", now=NOW)
        self.assertAlmostEqual(result[0]["score"], 1.0)


class QuoteVerificationTests(unittest.TestCase):
    PAGES = {"S1": "第一行\n\n中国 人工智能　发展很快，据报道增长明显。"}

    def test_quote_must_be_substring(self):
        ok = ms.verify_quotes(
            [{"sid": "S1", "quote": "中国 人工智能 发展很快，据报道增长明显。"}], self.PAGES)
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["quote"], "中国 人工智能 发展很快，据报道增长明显。")
        changed = ms.verify_quotes(
            [{"sid": "S1", "quote": "中国 人工智能 发展很快，据报道增长很大。"}], self.PAGES)
        self.assertEqual(changed, [])
        short = ms.verify_quotes([{"sid": "S1", "quote": "中国"}], self.PAGES)
        self.assertEqual(short, [])

    def test_verify_quotes_assigns_qids(self):
        kept = ms.verify_quotes([
            {"sid": "S1", "quote": "中国 人工智能 发展很快"},
            {"sid": "S1", "quote": "这段话不在正文里面"},
            {"sid": "S9", "quote": "中国 人工智能 发展很快"},
            {"sid": "S1", "quote": "据报道增长明显。"},
        ], self.PAGES)
        self.assertEqual([k["qid"] for k in kept], ["Q1", "Q2"])
        self.assertEqual([k["sid"] for k in kept], ["S1", "S1"])
        self.assertEqual(kept[1]["quote"], "据报道增长明显。")


SOURCES = {
    "S1": {"url": "https://a.test/1", "title": "A", "site": "a.test", "date": "2026-10-15", "domain": "a.test"},
    "S2": {"url": "https://b.test/2", "title": "B", "site": "b.test", "date": None, "domain": "b.test"},
    "S3": {"url": "https://reuters.com/3", "title": "R", "site": "reuters.com", "date": "2026-10-14", "domain": "reuters.com"},
    "S4": {"url": "https://a.test/4", "title": "A2", "site": "a.test", "date": "2026-10-15", "domain": "a.test"},
}
QUOTES = [
    {"sid": "S1", "quote": "甲方说明了事实的细节", "qid": "Q1"},
    {"sid": "S2", "quote": "乙方也确认了这件事", "qid": "Q2"},
    {"sid": "S3", "quote": "路透社报道了这一消息", "qid": "Q3"},
    {"sid": "S4", "quote": "同站另一篇的引文内容", "qid": "Q4"},
]


class CorroborateTests(unittest.TestCase):
    def test_corroborate_two_sites_pass(self):
        accepted, rejected = ms.corroborate(
            [{"text": "结论一", "quote_ids": ["Q1", "Q2"]}], QUOTES, SOURCES)
        self.assertEqual(rejected, [])
        self.assertEqual(accepted, [{
            "text": "结论一", "quote_ids": ["Q1", "Q2"],
            "credibility": "互证 2 站 (a.test, b.test)",
        }])

    def test_corroborate_same_site_not_corroborate(self):
        accepted, rejected = ms.corroborate(
            [{"text": "结论二", "quote_ids": ["Q1", "Q4"]}], QUOTES, SOURCES)
        self.assertEqual(accepted, [])
        self.assertEqual(rejected, [{"text": "结论二", "reason": "仅单一非权威来源"}])

    def test_corroborate_single_nonauth_rejected(self):
        accepted, rejected = ms.corroborate(
            [{"text": "结论三", "quote_ids": ["Q2"]}], QUOTES, SOURCES)
        self.assertEqual(accepted, [])
        self.assertEqual(rejected, [{"text": "结论三", "reason": "仅单一非权威来源"}])

    def test_corroborate_authority_single_ok(self):
        accepted, rejected = ms.corroborate(
            [{"text": "结论四", "quote_ids": ["Q3"]}], QUOTES, SOURCES)
        self.assertEqual(rejected, [])
        self.assertEqual(accepted[0]["credibility"], "权威 reuters.com")

    def test_corroborate_invalid_quote_ids(self):
        claims = [
            {"text": "a", "quote_ids": ["Q9"]},
            {"text": "b", "quote_ids": []},
            {"text": "c", "quote_ids": "Q1"},
            {"text": "d"},
            {"text": "  "},
        ]
        accepted, rejected = ms.corroborate(claims, QUOTES, SOURCES)
        self.assertEqual(accepted, [])
        self.assertEqual([r["reason"] for r in rejected[:4]], ["引用无效"] * 4)
        self.assertEqual(len(rejected), 5)


class RenderMarkdownTests(unittest.TestCase):
    def test_render_markdown_none_found(self):
        text = ms.render_markdown(
            "问题", "any", "2026-10-16T12:00:00+00:00", ["问题"],
            [], [], SOURCES, [], [{"text": "结论三", "reason": "仅单一非权威来源"}],
        )
        self.assertIn("# 查询: 问题", text)
        self.assertIn("## 没有找到", text)
        self.assertNotIn("## 结论", text)
        self.assertIn("结论三: 仅单一非权威来源", text)

    def test_render_markdown_has_urls_and_quotes(self):
        accepted, _ = ms.corroborate(
            [{"text": "结论一", "quote_ids": ["Q1", "Q2"]}], QUOTES, SOURCES)
        conflicts = [{"point": "数字不同", "positions": [
            {"sid": "S1", "quote": "甲方说明了事实的细节"},
            {"sid": "S3", "quote": "路透社报道了这一消息"},
        ]}]
        text = ms.render_markdown(
            "问题", "week", "2026-10-16T12:00:00+00:00", ["问题", "变体"],
            accepted, QUOTES, SOURCES, conflicts,
            [{"text": "结论三", "reason": "仅单一非权威来源"}],
        )
        self.assertIn("变体: 问题; 变体", text)
        self.assertIn("## 结论", text)
        self.assertIn("### C1 结论一", text)
        self.assertIn("可信度: 互证 2 站 (a.test, b.test)", text)
        self.assertIn("https://a.test/1", text)
        self.assertIn("https://b.test/2", text)
        self.assertIn('> "甲方说明了事实的细节"', text)
        self.assertIn('> "乙方也确认了这件事"', text)
        self.assertIn("## 来源说法不一致", text)
        self.assertIn("https://reuters.com/3", text)
        self.assertIn("## 没有找到 / 未通过核查", text)


if __name__ == "__main__":
    unittest.main()


class ModelLayerTests(unittest.TestCase):
    def _fake_config(self):
        """tg_bot.config 需要真实环境变量，测试里用假模块替代。"""
        import sys
        from types import SimpleNamespace
        from unittest import mock
        fake = SimpleNamespace(_next_ds_key=lambda: "k", DEEPSEEK_MODEL="model-test",
                               DEEPSEEK_API_BASE="https://example.invalid")
        return mock.patch.dict(sys.modules, {"tg_bot.config": fake})

    def test_call_model_returns_dict(self):
        calls = []

        def fake_post(url, body, headers=None, timeout=None):
            calls.append(body)
            return {"choices": [{"message": {"content": '{"a": 1}'}}]}

        with self._fake_config():
            result = ms.call_model("sys", {"q": "x"}, post=fake_post)
        self.assertEqual(result, {"a": 1})
        self.assertEqual(calls[0]["model"], "model-test")
        self.assertEqual(calls[0]["thinking"], {"type": "disabled"})
        self.assertEqual(calls[0]["temperature"], 0.0)

    def test_call_model_bad_json_returns_none(self):
        def fake_post(url, body, headers=None, timeout=None):
            return {"choices": [{"message": {"content": "not json"}}]}
        with self._fake_config():
            self.assertIsNone(ms.call_model("sys", {"q": "x"}, post=fake_post))

    def test_call_model_exception_returns_none(self):
        def fake_post(url, body, headers=None, timeout=None):
            raise RuntimeError("boom")
        with self._fake_config():
            self.assertIsNone(ms.call_model("sys", {"q": "x"}, post=fake_post))

    def test_parse_extract_caps_and_filters(self):
        raw = {"quotes": [
            {"sid": "a", "quote": "一"}, {"sid": "a", "quote": "二"},
            {"sid": "a", "quote": "三"}, {"sid": "a", "quote": "四"},
            {"sid": "b", "quote": ""}, {"quote": "无 sid"}, {"sid": "c"},
            {"sid": "c", "quote": "有效"},
        ]}
        result = ms.parse_extract(raw)
        self.assertEqual([r["quote"] for r in result], ["一", "二", "三", "有效"])

    def test_parse_extract_invalid_returns_empty(self):
        for raw in (None, [], {"quotes": "x"}, {}):
            with self.subTest(raw=raw):
                self.assertEqual(ms.parse_extract(raw), [])

    def test_parse_claims_valid(self):
        raw = {
            "claims": [{"text": "结论", "quote_ids": ["q1", "q2"]}],
            "conflicts": [{"point": "分歧", "qids": ["q1", "q3"]}],
        }
        claims, conflicts = ms.parse_claims(raw)
        self.assertEqual(claims, [{"text": "结论", "quote_ids": ["q1", "q2"]}])
        self.assertEqual(conflicts, [{"point": "分歧", "qids": ["q1", "q3"]}])

    def test_parse_claims_drops_bad_items(self):
        raw = {
            "claims": [
                {"text": "无引用", "quote_ids": []},
                {"text": "", "quote_ids": ["q1"]},
                {"text": "有效", "quote_ids": ["q1", "q1", 3]},
            ],
            "conflicts": [
                {"point": "只有一方", "qids": ["q1"]},
                {"point": "", "qids": ["q1", "q2"]},
            ],
        }
        claims, conflicts = ms.parse_claims(raw)
        self.assertEqual(claims, [{"text": "有效", "quote_ids": ["q1"]}])
        self.assertEqual(conflicts, [])
        self.assertEqual(ms.parse_claims("bad"), ([], []))

    def test_parse_verify_only_pass(self):
        self.assertTrue(ms.parse_verify({"verdict": "pass"}))
        for raw in ({"verdict": "fail"}, {"verdict": "unknown"}, {}, "pass", None):
            with self.subTest(raw=raw):
                self.assertFalse(ms.parse_verify(raw))

    def test_prompts_nonempty_and_mention_json(self):
        for prompt in (ms.REWRITE_PROMPT, ms.EXTRACT_PROMPT, ms.CLAIMS_PROMPT, ms.VERIFY_PROMPT):
            with self.subTest(prompt=prompt[:10]):
                self.assertTrue(prompt.strip())
                self.assertIn("JSON", prompt)
