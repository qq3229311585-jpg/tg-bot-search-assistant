#!/usr/bin/env python3
"""tests for tg_bot/cold_ground.py 冷知识取材(全部网络与模型调用都是假的)"""

from urllib.parse import parse_qs, quote, urlparse
import json
import unittest

from tg_bot import cold_ground as cg

LONG = "这是一段很长的维基百科材料内容。" * 80  # 1280 字，超过 1200 的材料长度门槛


def _search_json(titles):
    return json.dumps({"query": {"search": [{"title": t} for t in titles]}}, ensure_ascii=False)


def _extract_json(text):
    return json.dumps({"query": {"pages": {"1": {"extract": text}}}}, ensure_ascii=False)


class FakeWiki:
    """按 URL 参数返回假的搜索结果和条目正文"""

    def __init__(self, titles, extracts):
        self.titles = titles
        self.extracts = extracts

    def __call__(self, url):
        params = parse_qs(urlparse(url).query)
        if "srsearch" in params:
            return _search_json(self.titles)
        title = params["titles"][0]
        return _extract_json(self.extracts.get(title, ""))


def _content(year="1998"):
    return f"依据材料，{year}年发生了一件事。" + "这是测试用的正文内容，" * 5


class SearchTests(unittest.TestCase):
    def test_wiki_search_parses_and_filters(self):
        seen = []

        def get(url):
            seen.append(url)
            return _search_json([
                "猫", "猫的列表", "水消歧义", "恐龙年表", "分类:动物",
                "这是一个超过二十个字符长度的标题非常长用于测试过滤功能啊", "狗",
            ])

        self.assertEqual(cg.wiki_search("动物", get=get), ["猫", "狗"])
        self.assertIn("list=search", seen[0])

    def test_wiki_search_filters_institution_titles(self):
        def get(url):
            return _search_json(["某某研究中心", "某某研究所", "某某大学", "某某电影", "恒星"])

        self.assertEqual(cg.wiki_search("天文", get=get), ["恒星"])

    def test_wiki_search_bad_response_returns_empty(self):
        for raw in ("not json", "", '{"foo": 1}'):
            self.assertEqual(cg.wiki_search("动物", get=lambda url, r=raw: r), [])


class ExtractTests(unittest.TestCase):
    def test_wiki_extract_collapses_whitespace_and_truncates(self):
        raw = "第一段\n\n\n第二段   " + "字" * 5000
        result = cg.wiki_extract("甲", get=lambda url: _extract_json(raw))
        self.assertTrue(result.startswith("第一段 第二段 字"))
        self.assertEqual(len(result), 4000)

    def test_wiki_extract_failure_returns_empty(self):
        self.assertEqual(cg.wiki_extract("甲", get=lambda url: ""), "")


class NumbersTests(unittest.TestCase):
    def test_numbers_grounded_pass_and_fail(self):
        self.assertTrue(cg.numbers_grounded("1998年发生，比例2.5倍", "1998年的事件，比例为2.5倍"))
        self.assertFalse(cg.numbers_grounded("2024年发生", "1998年的事件"))
        self.assertTrue(cg.numbers_grounded("没有数字的一句话", "任何材料"))

    def test_numbers_grounded_thousands_separator(self):
        self.assertTrue(cg.numbers_grounded("约1,000人", "当时有1000人"))
        self.assertTrue(cg.numbers_grounded("约1,000人", "当时有1,000人"))
        self.assertFalse(cg.numbers_grounded("约2,000人", "当时有1,000人"))


class BuildGroundedTests(unittest.TestCase):
    def test_build_grounded_success_contains_source(self):
        get = FakeWiki(["甲"], {"甲": "1998年" + LONG})
        result = cg.build_grounded(
            "主题", [], 0, get=get, model=lambda p, d: {"content": _content()}
        )
        self.assertIsNotNone(result)
        self.assertIn("出处: 维基百科《甲》", result)
        self.assertIn(cg.wiki_url("甲"), result)

    def test_build_grounded_switches_title_on_bad_numbers(self):
        get = FakeWiki(["甲", "乙"], {"甲": "1998年" + LONG, "乙": "1998年" + LONG})
        calls = []

        def model(prompt, payload):
            calls.append(payload["title"])
            return {"content": _content("2024" if payload["title"] == "甲" else "1998")}

        result = cg.build_grounded("主题", [], 0, get=get, model=model)
        self.assertIn("《乙》", result)
        self.assertEqual(calls, ["甲", "乙"])

    def test_build_grounded_skips_short_extract_and_avoided_title(self):
        get = FakeWiki(["甲", "乙", "丙"], {"甲": "短", "乙": "1998年" + LONG, "丙": "1998年" + LONG})
        calls = []

        def model(prompt, payload):
            calls.append(payload["title"])
            return {"content": _content()}

        result = cg.build_grounded("主题", ["乙相关的旧讲解"], 0, get=get, model=model)
        self.assertEqual(calls, ["丙"])
        self.assertIn("《丙》", result)

    def test_build_grounded_rotates_only_within_first_eight_titles(self):
        titles = [f"条目{i}" for i in range(12)]
        get = FakeWiki(titles, {t: "1998年" + LONG for t in titles})
        for day in range(12):
            calls = []

            def model(prompt, payload):
                calls.append(payload["title"])
                return {"content": _content()}

            cg.build_grounded("主题", [], day, get=get, model=model)
            self.assertIn(calls[0], titles[:8])

    def test_build_grounded_length_gate_is_1200(self):
        get = FakeWiki(["甲", "乙"], {"甲": "1998年" + "这" * 1000, "乙": "1998年" + "这" * 1300})
        calls = []

        def model(prompt, payload):
            calls.append(payload["title"])
            return {"content": _content()}

        result = cg.build_grounded("主题", [], 0, get=get, model=model)
        self.assertEqual(calls, ["乙"])
        self.assertIn("《乙》", result)

    def test_build_grounded_model_errors_return_none_after_max_attempts(self):
        get = FakeWiki(["甲", "乙", "丙", "丁"], {t: "1998年" + LONG for t in "甲乙丙丁"})
        calls = []

        def model(prompt, payload):
            calls.append(payload["title"])
            raise RuntimeError("boom")

        self.assertIsNone(cg.build_grounded("主题", [], 0, get=get, model=model))
        self.assertEqual(len(calls), 3)


class SensitiveTitleTests(unittest.TestCase):
    def test_sensitive_titles_are_filtered(self):
        titles = ["古羅馬同性戀", "纳粹德国", "罗马水道", "古罗马建筑"]
        body = json.dumps({"query": {"search": [{"title": t} for t in titles]}})
        self.assertEqual(cg.wiki_search("古罗马", get=lambda url: body),
                         ["罗马水道", "古罗马建筑"])



if __name__ == "__main__":
    unittest.main()
