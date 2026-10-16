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
        self.assertEqual(rejected, [{"text": "结论二", "reason": "仅单一非权威来源", "quote_ids": ["Q1", "Q4"]}])

    def test_corroborate_single_nonauth_rejected(self):
        accepted, rejected = ms.corroborate(
            [{"text": "结论三", "quote_ids": ["Q2"]}], QUOTES, SOURCES)
        self.assertEqual(accepted, [])
        self.assertEqual(rejected, [{"text": "结论三", "reason": "仅单一非权威来源", "quote_ids": ["Q2"]}])

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


_AUTO = object()
_DEFAULT_REWRITE = {"queries": [], "time_range": "any", "must_terms": []}
URL_A = "https://a.example.com/news/1"
URL_B = "https://b.example.org/story/2"
TITLE_A = "甲站报道"
TITLE_B = "乙站报道"
QUOTE_A = "该项目计划于明年第一季度投产"
QUOTE_B = "乙站确认该项目计划于明年第一季度投产"
BODY_A = "甲站记者报道，" + QUOTE_A + "。" + "相关设备已完成安装调试，" * 24
BODY_B = "乙站综合报道：" + QUOTE_B + "。" + "现场施工进展顺利，" * 24
ROWS = [
    {"url": URL_A, "title": TITLE_A, "summary": "", "published_at": None},
    {"url": URL_B, "title": TITLE_B, "summary": "", "published_at": None},
]
PAGES = {URL_A: BODY_A, URL_B: BODY_B}
QUOTE_BY_TITLE = {TITLE_A: QUOTE_A, TITLE_B: QUOTE_B}
CLAIMS_OK = {"claims": [{"text": "该项目计划于明年第一季度投产", "quote_ids": ["Q1", "Q2"]}],
             "conflicts": []}


class RunQueryTests(unittest.TestCase):
    def _deps(self, *, rows=ROWS, pages=PAGES, rewrite=_DEFAULT_REWRITE, extract=_AUTO,
              claims=CLAIMS_OK, verify="pass", web_error=None, calls=None):
        def model(system_prompt, payload):
            if system_prompt == ms.REWRITE_PROMPT:
                return rewrite
            if system_prompt == ms.EXTRACT_PROMPT:
                if extract is not _AUTO:
                    return extract
                return {"quotes": [{"sid": p["sid"], "quote": QUOTE_BY_TITLE[p["title"]]}
                                   for p in payload["pages"] if p["title"] in QUOTE_BY_TITLE]}
            if system_prompt == ms.CLAIMS_PROMPT:
                return claims
            if system_prompt == ms.VERIFY_PROMPT:
                return {"verdict": verify}
            return None

        def web(query):
            if calls is not None:
                calls.append(query)
            if web_error is not None:
                raise web_error
            return list(rows), {}

        def fetch(url):
            return pages.get(url, "正文抓取失败：无数据")

        return {"web": web, "news": lambda query: ([], {}), "fetch": fetch,
                "model": model, "canon": lambda url: url}

    def test_happy_path_two_sites(self):
        out = ms.run_query("新项目什么时候投产", deps=self._deps(), now=NOW)
        self.assertIn("该项目计划于明年第一季度投产", out)
        self.assertIn(URL_A, out)
        self.assertIn(URL_B, out)
        self.assertIn(f'"{QUOTE_A}"', out)
        self.assertIn(f'"{QUOTE_B}"', out)
        self.assertIn("互证 2 站", out)

    def test_fabricated_quote_dropped(self):
        extract = {"quotes": [{"sid": "S1", "quote": "这句话在页面里根本不存在的编造内容"}]}
        out = ms.run_query("新项目什么时候投产", deps=self._deps(extract=extract), now=NOW)
        self.assertIn("没有找到", out)
        self.assertIn("没有找到可核对的原文引文", out)

    def test_single_nonauthority_rejected(self):
        claims = {"claims": [{"text": "该项目计划于明年第一季度投产", "quote_ids": ["Q1"]}],
                  "conflicts": []}
        out = ms.run_query("新项目什么时候投产",
                           deps=self._deps(rows=[ROWS[0]], claims=claims), now=NOW)
        self.assertNotIn("## 结论", out)
        self.assertIn("仅单一非权威来源", out)

    def test_verify_fail_demotes(self):
        out = ms.run_query("新项目什么时候投产", deps=self._deps(verify="fail"), now=NOW)
        self.assertIn("核查未通过", out)
        self.assertNotIn("### C1", out)

    def test_no_pages_returns_not_found(self):
        out = ms.run_query("新项目什么时候投产", deps=self._deps(pages={}), now=NOW)
        self.assertIn("没有找到", out)
        self.assertIn("未能获取任何可用页面", out)

    def test_rewrite_failure_falls_back(self):
        calls = []
        q = "新项目什么时候投产"
        out = ms.run_query(q, deps=self._deps(rewrite=None, calls=calls), now=NOW)
        self.assertIn(q, calls)
        self.assertIn("互证 2 站", out)

    def test_conflict_mapped(self):
        claims = {"claims": [],
                  "conflicts": [{"point": "投产时间说法不一", "qids": ["Q1", "Q2"]}]}
        out = ms.run_query("新项目什么时候投产", deps=self._deps(claims=claims), now=NOW)
        self.assertIn("来源说法不一致", out)
        self.assertIn(URL_A, out)

    def test_never_raises(self):
        out = ms.run_query("新项目什么时候投产",
                           deps=self._deps(rewrite=None, web_error=RuntimeError("boom")),
                           now=NOW)
        self.assertIsInstance(out, str)
        self.assertIn("没有找到", out)


class CliTests(unittest.TestCase):
    def _env(self, tmp, **extra):
        import os
        env = {
            "MUSE_SEARCH_DISABLED_FILE": os.path.join(tmp, "disabled"),
            "MUSE_SEARCH_LOCK_DIR": os.path.join(tmp, "locks"),
            "MUSE_SEARCH_LOG": os.path.join(tmp, "audit.log"),
            "MUSE_SEARCH_TIMEOUT": "10",
        }
        env.update(extra)
        return env

    def _main(self, env, runner, stdin=b'{"q":"ab"}'):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ms.main(stdin_bytes=stdin, runner=runner, env=env)
        return code, buf.getvalue()

    def test_disabled_flag_exits_3(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            with open(env["MUSE_SEARCH_DISABLED_FILE"], "w") as fh:
                fh.write("1")
            calls = []
            code, out = self._main(env, lambda q: calls.append(q) or "X")
            self.assertEqual(code, 3)
            self.assertIn("暂停", out)
            self.assertEqual(calls, [])

    def test_invalid_input_exits_2(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._main(self._env(tmp), lambda q: "X", stdin=b"not json")
            self.assertEqual(code, 2)
            self.assertIn("输入无效", out)

    def test_ok_path_writes_audit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp, FAKE_API_KEY="SECRET_VALUE_X")
            code, out = self._main(env, lambda q: "RESULT")
            self.assertEqual(code, 0)
            self.assertIn("RESULT", out)
            with open(env["MUSE_SEARCH_LOG"], encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("SECRET_VALUE_X", text)
            lines = text.splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["status"], "ok")
            self.assertEqual(len(rec["q_sha256"]), 64)

    def test_busy_exits_4(self):
        import fcntl
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            os.makedirs(env["MUSE_SEARCH_LOCK_DIR"], exist_ok=True)
            held = []
            try:
                for name in ("slot0", "slot1"):
                    fh = open(os.path.join(env["MUSE_SEARCH_LOCK_DIR"], name), "a+")
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(fh)
                calls = []
                code, out = self._main(env, lambda q: calls.append(q) or "X")
                self.assertEqual(code, 4)
                self.assertIn("服务正忙", out)
                self.assertEqual(calls, [])
            finally:
                for fh in held:
                    fh.close()

    def test_lock_released_after_run(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp)
            self.assertEqual(self._main(env, lambda q: "A")[0], 0)
            self.assertEqual(self._main(env, lambda q: "B")[0], 0)

    def test_timeout_exits_5(self):
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp, MUSE_SEARCH_TIMEOUT="1")
            code, out = self._main(env, lambda q: time.sleep(3) or "X")
            self.assertEqual(code, 5)
            self.assertIn("超时", out)

    def test_timeout_not_swallowed_by_runner_except_exception(self):
        import contextlib, io, tempfile, time
        from tg_bot import muse_search as m

        def swallowing_runner(q):
            try:
                time.sleep(3)
            except Exception:
                return "swallowed"
            return "done"

        with tempfile.TemporaryDirectory() as d:
            env = {
                "MUSE_SEARCH_DISABLED_FILE": d + "/none",
                "MUSE_SEARCH_LOCK_DIR": d + "/locks",
                "MUSE_SEARCH_LOG": d + "/log",
                "MUSE_SEARCH_TIMEOUT": "1",
            }
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = m.main(stdin_bytes=b'{"q":"ab"}', runner=swallowing_runner, env=env)
        self.assertEqual(code, 5)
        self.assertIn("超时", buf.getvalue())
        self.assertNotIn("swallowed", buf.getvalue())

    def test_load_env_file_parses_and_does_not_override(self):
        import tempfile
        from tg_bot import muse_search as m
        with tempfile.TemporaryDirectory() as d:
            path = d + "/e.env"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# c\nA=1\nB = \"two words\"\nBAD KEY=x\nC=keep\n$X=1\n")
            environ = {"C": "already"}
            n = m.load_env_file(path, environ)
        self.assertEqual(environ["A"], "1")
        self.assertEqual(environ["B"], "two words")
        self.assertEqual(environ["C"], "already")
        self.assertNotIn("BAD KEY", environ)
        self.assertNotIn("$X", environ)
        self.assertEqual(n, 3)

    def test_load_env_file_missing_returns_zero(self):
        from tg_bot import muse_search as m
        self.assertEqual(m.load_env_file("/nonexistent/x.env", {}), 0)

    def test_rank_drops_stale_dated_candidates(self):
        from datetime import datetime, timezone
        from tg_bot import muse_search as m
        now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        cands = [
            {"title": "旧闻", "url": "https://a.example.com/1", "summary": "x", "published_at": "Feb 8, 2021"},
            {"title": "新闻", "url": "https://b.example.org/2", "summary": "x", "published_at": "2026-10-15"},
            {"title": "未知", "url": "https://c.example.net/3", "summary": "x", "published_at": None},
        ]
        week = [c["title"] for c in m.rank_candidates(cands, [], "week", now)]
        self.assertNotIn("旧闻", week)
        self.assertIn("新闻", week)
        self.assertIn("未知", week)
        anyr = [c["title"] for c in m.rank_candidates(cands, [], "any", now)]
        self.assertIn("旧闻", anyr)

    def test_parse_published_extra_formats(self):
        from datetime import datetime, timezone
        from tg_bot import muse_search as m
        now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.assertEqual(m._parse_published("Feb 8, 2021", now).year, 2021)
        self.assertEqual(m._parse_published("2026年10月15日", now).day, 15)
        self.assertIsNone(m._parse_published("昨天吧", now))

    def test_rejected_section_lists_source_urls(self):
        from tg_bot import muse_search as m
        quotes = [{"qid": "Q1", "sid": "S1", "quote": "某条原文引文内容"}]
        sources = {"S1": {"url": "https://only.example.com/a", "title": "t", "site": "example.com", "date": "未知", "domain": "only.example.com"}}
        rejected = [{"text": "某结论", "reason": "仅单一非权威来源", "quote_ids": ["Q1"]}]
        out = m.render_markdown("q", "any", "2026-10-16T00:00:00+00:00", ["q"], [], quotes, sources, [], rejected)
        self.assertIn("https://only.example.com/a", out)
        self.assertIn("仅单一非权威来源", out)

    def test_infer_page_date(self):
        from datetime import datetime, timezone
        from tg_bot import muse_search as m
        now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.assertEqual(m._infer_page_date("2021年2月8日 桃田贤斗眼窝骨折", now).year, 2021)
        self.assertEqual(m._infer_page_date("发布于 2026-10-15 来源", now).day, 15)
        self.assertIsNone(m._infer_page_date("没有日期的正文", now))
        self.assertIsNone(m._infer_page_date("预计 2099-01-01 上市", now))

    def test_runner_exception_exits_1(self):
        import tempfile

        def boom(q):
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._main(self._env(tmp), boom)
            self.assertEqual(code, 1)
            self.assertIn("查询失败: RuntimeError", out)

    def test_audit_failure_does_not_break(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = self._env(tmp, MUSE_SEARCH_LOG="/proc/nonexistent/x.log")
            code, out = self._main(env, lambda q: "RESULT")
            self.assertEqual(code, 0)
            self.assertIn("RESULT", out)


def _sites(n, prefix="s"):
    return [{"url": f"https://{prefix}{i}.com/a", "title": f"t{prefix}{i}",
             "summary": "", "published_at": None} for i in range(n)]


class AdaptiveSearchTests(unittest.TestCase):
    QUERIES = ["q1", "q2", "q3"]

    def _run(self, *, time_range="any", deps_extra=None, legacy=False):
        calls = {"web_brave": [], "news_brave": [], "web_serper": [], "web_tavily": [],
                 "web": [], "news": []}
        rewrite = {"queries": list(self.QUERIES), "time_range": time_range, "must_terms": []}

        def model(system_prompt, payload):
            if system_prompt == ms.REWRITE_PROMPT:
                return rewrite
            return None

        def recorder(name, rows):
            def fn(query):
                calls[name].append(query)
                return list(rows), {}
            return fn

        deps = {"model": model, "fetch": lambda url: "", "canon": lambda url: url}
        if legacy:
            deps["web"] = recorder("web", _sites(1))
            deps["news"] = recorder("news", [])
        else:
            deps["web_brave"] = recorder("web_brave", _sites(6, "b"))
            deps["news_brave"] = recorder("news_brave", [])
            deps["web_serper"] = recorder("web_serper", [])
            deps["web_tavily"] = recorder("web_tavily", [])
        deps.update(deps_extra or {})
        ms.run_query("问题", deps=deps, now=NOW)
        return calls

    def test_stops_after_brave_when_enough_sites(self):
        calls = self._run()
        self.assertEqual(calls["web_serper"], [])
        self.assertEqual(calls["web_tavily"], [])
        self.assertEqual(ms.LAST_SEARCH_CALLS, 3)

    def test_escalates_to_serper_when_few_sites(self):
        serper_calls = []

        def serper(q):
            serper_calls.append(q)
            return _sites(4, "c"), {}

        calls = self._run(deps_extra={"web_brave": lambda q: (_sites(2, "b"), {}),
                                      "web_serper": serper})
        self.assertEqual(len(serper_calls), 3)
        self.assertEqual(calls["web_tavily"], [])  # 2 + 4 sites reaches ENOUGH_SITES
        self.assertEqual(ms.LAST_SEARCH_CALLS, 6)

    def test_tavily_called_once_at_most(self):
        tavily_calls = []

        def tavily(q):
            tavily_calls.append(q)
            return _sites(1, "t"), {}

        self._run(deps_extra={
            "web_brave": lambda q: (_sites(1, "b"), {}),
            "web_serper": lambda q: (_sites(1, "c"), {}),
            "web_tavily": tavily,
        })
        self.assertEqual(tavily_calls, ["问题"])
        self.assertEqual(ms.LAST_SEARCH_CALLS, 7)

    def test_news_brave_only_for_day_week(self):
        calls = self._run(time_range="day")
        self.assertEqual(len(calls["news_brave"]), 3)
        calls = self._run(time_range="any")
        self.assertEqual(calls["news_brave"], [])

    def test_legacy_deps_still_work(self):
        calls = self._run(legacy=True)
        self.assertEqual(sorted(calls["web"]), sorted(["问题", "q1", "q2", "q3"]))
        self.assertEqual(calls["news"], [])
        self.assertEqual(calls["web_brave"], [])
        self.assertEqual(ms.LAST_SEARCH_CALLS, 4)  # legacy path keeps queries[:4]

    def test_audit_extra_written(self):
        import os
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.log")
            ms._audit(path, "ok", "q", time.monotonic(), 3, extra={"search_calls": 5})
            with open(path, encoding="utf-8") as fh:
                record = json.loads(fh.readline())
        self.assertEqual(record["search_calls"], 5)
        self.assertEqual(record["status"], "ok")


class QuotaGuardTests(unittest.TestCase):
    QUERIES = ["q1", "q2", "q3"]

    def _run(self, *, quota, brave_rows=None, serper_rows=None, tavily_rows=None):
        calls = {"web_brave": [], "web_serper": [], "web_tavily": []}
        rewrite = {"queries": list(self.QUERIES), "time_range": "any", "must_terms": []}

        def model(system_prompt, payload):
            return rewrite if system_prompt == ms.REWRITE_PROMPT else None

        def recorder(name, rows):
            def fn(query):
                calls[name].append(query)
                return list(rows or []), {}
            return fn

        deps = {"model": model, "fetch": lambda url: "", "canon": lambda url: url,
                "web_brave": recorder("web_brave", brave_rows),
                "web_serper": recorder("web_serper", serper_rows),
                "web_tavily": recorder("web_tavily", tavily_rows)}
        if quota is not None:
            deps["quota"] = quota
        out = ms.run_query("问题", deps=deps, now=NOW)
        return out, calls

    def test_quota_status_thresholds(self):
        limits = {"brave": 100, "serper": 100, "tavily_0": 50, "tavily_1": 50}
        self.assertTrue(ms.quota_status({"brave": 79}, limits)["brave"])
        self.assertFalse(ms.quota_status({"brave": 80}, limits)["brave"])
        self.assertTrue(ms.quota_status({"brave": 999}, {"brave": 0})["brave"])
        self.assertTrue(ms.quota_status({"tavily_0": 30, "tavily_1": 30}, limits)["tavily"] is True)
        self.assertFalse(ms.quota_status({"tavily_0": 40, "tavily_1": 40}, limits)["tavily"])

    def test_quota_status_bad_input(self):
        result = ms.quota_status(None, None)
        self.assertEqual(result, {"brave": True, "serper": True, "tavily": True})

    def test_all_over_pauses_query(self):
        out, calls = self._run(quota=lambda: {"brave": False, "serper": False, "tavily": False},
                               brave_rows=_sites(6, "b"))
        self.assertIn("已暂停", out)
        self.assertEqual(calls, {"web_brave": [], "web_serper": [], "web_tavily": []})
        self.assertEqual(ms.LAST_SEARCH_CALLS, 0)

    def test_skips_unavailable_provider(self):
        quota = lambda: {"brave": False, "serper": True, "tavily": True}
        _, calls = self._run(quota=quota, serper_rows=_sites(6, "s"))
        self.assertEqual(calls["web_brave"], [])
        self.assertEqual(len(calls["web_serper"]), 3)

    def test_quota_error_defaults_to_allowed(self):
        def boom():
            raise RuntimeError("quota unavailable")
        _, calls = self._run(quota=boom, brave_rows=_sites(6, "b"))
        self.assertEqual(len(calls["web_brave"]), 3)

    def test_load_quota_status_never_raises(self):
        status = ms._load_quota_status()
        self.assertIsInstance(status, dict)
        for key in ("brave", "serper", "tavily"):
            self.assertIsInstance(status[key], bool)
