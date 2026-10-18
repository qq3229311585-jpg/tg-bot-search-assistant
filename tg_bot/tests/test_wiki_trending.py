#!/usr/bin/env python3
"""tests for tg_bot/wiki_trending.py 别国维基热榜(全部网络、模型与睡眠都是假的)"""

from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse
import json
import unittest

from tg_bot import wiki_trending as wt

NOW = datetime(2026, 10, 18, 1, 0, tzinfo=timezone.utc)
YESTERDAY = date(2026, 10, 17)
DAY_BEFORE = date(2026, 10, 16)
OCT15 = date(2026, 10, 15)
EXTRACT = (
    "这是一个测试条目，它在1998年首次出现，其规模达到了3.5万人。"
    "该条目描述的是一种虚构的海洋生物，栖息于深海热泉附近，身体呈半透明状态，"
    "科学家通过多次潜水观测记录了它的形态与习性，并整理出一份详尽的资料。" * 2
)
EXTRACT = " ".join(EXTRACT.split())


def _no_sleep(seconds):
    return None


def _recorder():
    calls = []
    return calls, calls.append


def _top_json(titles):
    return json.dumps({"items": [{"articles": [
        {"article": t, "views": 100000 - i, "rank": i + 1} for i, t in enumerate(titles)
    ]}]}, ensure_ascii=False)


def _daily_json(views_by_day):
    return json.dumps({"items": [
        {"timestamp": f"{d}00", "views": v} for d, v in views_by_day.items()
    ]})


def _days(end, counts):
    """counts: 长度 15 的列表，最后一个是 end 当天"""
    out = {}
    for i, value in enumerate(counts):
        day = end - timedelta(days=len(counts) - 1 - i)
        out[f"{day:%Y%m%d}"] = value
    return out


class FakeWiki:
    """按 URL 分派的假网络。

    top: lang -> 标题列表(None 表示该语言无数据)
    top_dates: 哪些日期有热榜数据(默认只有 YESTERDAY)
    empty_first: URL 子串 -> 该子串前 N 次请求返回空(模拟 429 / 延迟)
    """

    def __init__(self, top=None, daily=None, extracts=None, langlinks=None, default="",
                 top_dates=None, empty_first=None):
        self.top = top or {}
        self.daily = daily or {}
        self.extracts = extracts or {}
        self.langlinks = langlinks or {}
        self.default = default
        self.top_dates = {YESTERDAY} if top_dates is None else set(top_dates)
        self.empty_first = dict(empty_first or {})
        self._seen = {}
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for key, n in self.empty_first.items():
            if key in url:
                count = self._seen.get(key, 0)
                self._seen[key] = count + 1
                if count < n:
                    return ""
        if "/metrics/pageviews/top/" in url:
            lang = url.split("/top/")[1].split(".wikipedia")[0]
            titles = self.top.get(lang)
            if titles is None:
                return ""
            y, m, d = (int(part) for part in url.rstrip("/").split("/")[-3:])
            if date(y, m, d) not in self.top_dates:
                return ""
            return _top_json(titles)
        if "/per-article/" in url:
            title = unquote(url.split("/user/")[1].split("/daily/")[0])
            views = self.daily.get(title)
            if views is None:
                return ""
            return _daily_json(views)
        if "action=query&prop=extracts" in url:
            host = urlparse(url).netloc.split(".")[0]
            title = parse_qs(urlparse(url).query)["titles"][0]
            text = self.extracts.get((host, title))
            if text is None:
                return ""
            return json.dumps({"query": {"pages": {"1": {"extract": text}}}}, ensure_ascii=False)
        if "prop=langlinks" in url:
            title = parse_qs(urlparse(url).query)["titles"][0]
            zh = self.langlinks.get(title)
            if zh is None:
                return json.dumps({"query": {"pages": {"1": {"title": title}}}})
            return json.dumps({"query": {"pages": {"1": {"langlinks": [{"lang": "zh", "*": zh}]}}}}, ensure_ascii=False)
        return self.default

    def top_urls(self, lang=None):
        return [u for u in self.calls if "/metrics/pageviews/top/" in u
                and (lang is None or f"/top/{lang}.wikipedia" in u)]


def _build(fake, model, day=0, **kwargs):
    """统一入口：睡眠注入为空函数"""
    kwargs.setdefault("sleep", _no_sleep)
    return wt.build_wiki_trending(day, NOW, get=fake, model=model, **kwargs)


def _surging_daily(today=5000, base=500, end=YESTERDAY):
    """15 天数据：前 14 天基线 base，当天 today"""
    counts = [base] * 14 + [today]
    return _days(end, counts)


def _good_model(system_prompt, payload):
    return {
        "summary": "这是一种栖息在深海热泉附近的虚构海洋生物，身体半透明。",
        "quote": "它在1998年首次出现",
        "quote_zh": "它在1998年首次出现",
    }


class LanguageRotationTests(unittest.TestCase):
    def test_rotation_starts_at_day_index_and_wraps(self):
        langs = wt.rotation_languages(0)
        self.assertEqual([code for code, _ in langs], ["ja", "de", "fr", "es"])
        last = wt.rotation_languages(len(wt.LANGS) - 1)
        self.assertEqual([code for code, _ in last], ["he", "ja", "de", "fr"])
        self.assertEqual(len(wt.LANGS), 20)

    def test_falls_through_to_next_language_when_today_has_no_result(self):
        fake = FakeWiki(
            top={"ja": ["Main_Page", "Some_Title"], "de": ["Deutsche_Seite"]},
            daily={"Deutsche_Seite": _surging_daily()},
            extracts={("de", "Deutsche_Seite"): EXTRACT},
            langlinks={"Deutsche_Seite": "德语测试条目"},
        )
        seen = []

        def model(system_prompt, payload):
            seen.append(payload["lang_zh"])
            return _good_model(system_prompt, payload)

        result = _build(fake, model)
        self.assertIsNotNone(result)
        self.assertEqual(seen, ["德语"])
        self.assertTrue(result.startswith("德语维基百科 · 《德语测试条目》"))

    def test_gives_up_after_four_languages(self):
        fake = FakeWiki(top={code: ["X_Title"] for code, _ in wt.LANGS})
        model_calls = []
        result = _build(fake, lambda *a: model_calls.append(a) or {})
        self.assertIsNone(result)
        self.assertEqual(model_calls, [])
        langs_tried = {u.split("/top/")[1].split(".wikipedia")[0] for u in fake.top_urls()}
        self.assertEqual(len(langs_tried), wt.LANG_FALLBACK_TRIES)


class TopTitlesTests(unittest.TestCase):
    def test_filters_home_namespace_and_takes_top_25(self):
        titles = (
            ["Main_Page", "Special:Search", "Wikipedia:About", "Hauptseite", "Title_0"]
            + [f"Title_{i}" for i in range(1, 40)]
        )
        fake = FakeWiki(top={"de": titles})
        result = wt.top_titles("de", YESTERDAY, fake, sleep=_no_sleep)
        self.assertNotIn("Main_Page", result)
        self.assertNotIn("Special:Search", result)
        self.assertNotIn("Wikipedia:About", result)
        self.assertEqual(len(result), wt.TOP_LIMIT)
        self.assertEqual(result[0], "Title_0")

    def test_home_name_in_other_languages_is_filtered(self):
        fake = FakeWiki(top={"ru": ["Заглавная_страница", "Тест"]})
        self.assertEqual(wt.top_titles("ru", YESTERDAY, fake, sleep=_no_sleep), ["Тест"])

    def test_broken_json_returns_empty(self):
        self.assertEqual(wt.top_titles("ja", YESTERDAY, lambda url: "not json", sleep=_no_sleep), [])
        self.assertEqual(wt.top_titles("ja", YESTERDAY, lambda url: "", sleep=_no_sleep), [])

    def test_empty_response_retried_with_2s_then_4s(self):
        fake = FakeWiki(top={"ja": ["T_One"]}, empty_first={"/top/ja.wikipedia": 2})
        sleeps, record = _recorder()
        result = wt.top_titles("ja", YESTERDAY, fake, sleep=record)
        self.assertEqual(result, ["T_One"])
        self.assertEqual(sleeps, [2, 4])
        self.assertEqual(len(fake.top_urls("ja")), 3)

    def test_persistent_empty_response_gives_up_after_two_retries(self):
        fake = FakeWiki(top={"ja": ["T_One"]}, empty_first={"/top/ja.wikipedia": 99})
        sleeps, record = _recorder()
        self.assertEqual(wt.top_titles("ja", YESTERDAY, fake, sleep=record), [])
        self.assertEqual(sleeps, [2, 4])
        self.assertEqual(len(fake.top_urls("ja")), 3)


class ProbeTests(unittest.TestCase):
    def test_probe_falls_back_to_earlier_day_when_yesterday_missing(self):
        # 昨天(10/17)、前天(10/16) 都 404，10/15 才有数据
        fake = FakeWiki(top={"ja": ["T_One"]}, top_dates={OCT15})
        sleeps, record = _recorder()
        found = wt.probe_end_date("ja", YESTERDAY, fake, sleep=record)
        self.assertEqual(found, (OCT15, ["T_One"]))
        # 每个空日期只重试 1 次(2 秒)，找到数据后不再等待
        self.assertEqual(sleeps, [2, 2])

    def test_probe_gives_up_after_five_dates(self):
        fake = FakeWiki(top={"ja": ["T_One"]}, top_dates=set())
        sleeps, record = _recorder()
        self.assertIsNone(wt.probe_end_date("ja", YESTERDAY, fake, sleep=record))
        dates = {u.rstrip("/").split("/")[-3:][-1] for u in fake.top_urls("ja")}
        self.assertEqual(len(dates), wt.PROBE_DAYS)
        self.assertEqual(sleeps, [2] * wt.PROBE_DAYS)
        self.assertEqual(len(fake.top_urls("ja")), wt.PROBE_DAYS * 2)

    def test_probe_result_is_cached_per_language(self):
        fake = FakeWiki(top={"ja": ["T_One"]})
        cache = {}
        first = wt.probe_end_date("ja", YESTERDAY, fake, sleep=_no_sleep, cache=cache)
        calls_after_first = len(fake.calls)
        second = wt.probe_end_date("ja", YESTERDAY, fake, sleep=_no_sleep, cache=cache)
        self.assertEqual(first, second)
        self.assertEqual(len(fake.calls), calls_after_first)

    def test_probe_failure_is_cached_too(self):
        fake = FakeWiki(top={"ja": ["T_One"]}, top_dates=set())
        cache = {}
        self.assertIsNone(wt.probe_end_date("ja", YESTERDAY, fake, sleep=_no_sleep, cache=cache))
        calls_after_first = len(fake.calls)
        self.assertIsNone(wt.probe_end_date("ja", YESTERDAY, fake, sleep=_no_sleep, cache=cache))
        self.assertEqual(len(fake.calls), calls_after_first)

    def test_build_uses_probed_date_for_surge_and_output(self):
        fake = FakeWiki(
            top={"ja": ["Deep_Sea_Creature"]},
            top_dates={OCT15},
            daily={"Deep_Sea_Creature": _surging_daily(end=OCT15)},
            extracts={("ja", "Deep_Sea_Creature"): EXTRACT},
            langlinks={"Deep_Sea_Creature": "深海生物"},
        )
        result = _build(fake, _good_model)
        self.assertEqual(
            result.split("\n")[4],
            "10月15日浏览 5,000 次,约为平日的 10 倍(基线日均约 500)",
        )
        self.assertIn("/per-article/ja.wikipedia/all-access/user/Deep_Sea_Creature/daily/20261001/20261015", "\n".join(fake.calls))


class SurgeTests(unittest.TestCase):
    def test_ratio_and_baseline_are_computed_from_previous_14_days(self):
        counts = [400] * 7 + [600] * 7 + [4800]
        fake = FakeWiki(daily={"T": _days(YESTERDAY, counts)})
        views, baseline, ratio = wt.surge("ja", "T", YESTERDAY, fake, sleep=_no_sleep)
        self.assertEqual(views, 4800)
        self.assertEqual(baseline, 500)  # 中位数 (400+600)/2
        self.assertAlmostEqual(ratio, 9.6)

    def test_request_window_is_15_days_ending_yesterday(self):
        fake = FakeWiki(daily={"T": _surging_daily()})
        wt.surge("ja", "T", YESTERDAY, fake, sleep=_no_sleep)
        url = fake.calls[-1]
        self.assertIn("/per-article/ja.wikipedia/all-access/user/T/daily/20261003/20261017", url)

    def test_empty_response_retried_with_2s_then_4s(self):
        fake = FakeWiki(daily={"T": _surging_daily()}, empty_first={"per-article/ja.wikipedia": 2})
        sleeps, record = _recorder()
        views, baseline, ratio = wt.surge("ja", "T", YESTERDAY, fake, sleep=record)
        self.assertEqual((views, baseline), (5000, 500))
        self.assertEqual(sleeps, [2, 4])

    def test_below_view_threshold_returns_none(self):
        fake = FakeWiki(daily={"T": _surging_daily(today=2999, base=100)})
        self.assertIsNone(wt.surge("ja", "T", YESTERDAY, fake, sleep=_no_sleep))

    def test_below_ratio_threshold_returns_none(self):
        fake = FakeWiki(daily={"T": _surging_daily(today=5000, base=2000)})
        self.assertIsNone(wt.surge("ja", "T", YESTERDAY, fake, sleep=_no_sleep))

    def test_exactly_at_threshold_passes(self):
        fake = FakeWiki(daily={"T": _surging_daily(today=3000, base=1000)})
        views, baseline, ratio = wt.surge("ja", "T", YESTERDAY, fake, sleep=_no_sleep)
        self.assertEqual((views, baseline), (3000, 1000))
        self.assertEqual(ratio, 3.0)

    def test_insufficient_history_returns_none(self):
        counts = [0] * 10 + [500] * 4 + [9000]  # 有效基线点只有 4 个 < 7
        days = {k: v for k, v in _days(YESTERDAY, counts).items() if v}
        fake = FakeWiki(daily={"T": days})
        self.assertIsNone(wt.surge("ja", "T", YESTERDAY, fake, sleep=_no_sleep))

    def test_missing_today_returns_none(self):
        fake = FakeWiki(daily={"T": _days(DAY_BEFORE, [500] * 14 + [9000])})
        self.assertIsNone(wt.surge("ja", "T", YESTERDAY, fake, sleep=_no_sleep))


class ExtractTests(unittest.TestCase):
    def test_compresses_whitespace_and_caps_length(self):
        long_text = ("字字字字  \n\t" * 400)  # 压缩后约 1999 字，超过 1500 上限
        fake = FakeWiki(extracts={("ja", "T"): long_text})
        text = wt.extract_intro("ja", "T", fake)
        self.assertEqual(len(text), wt.EXTRACT_MAX_CHARS)
        self.assertNotIn("\n", text)
        self.assertNotIn("  ", text)

    def test_too_short_is_failure(self):
        fake = FakeWiki(extracts={("ja", "T"): "短" * 149})
        self.assertIsNone(wt.extract_intro("ja", "T", fake))
        fake = FakeWiki(extracts={("ja", "T"): "短" * 150})
        self.assertIsNotNone(wt.extract_intro("ja", "T", fake))

    def test_missing_page_is_failure(self):
        self.assertIsNone(wt.extract_intro("ja", "T", FakeWiki()))


class ZhTitleTests(unittest.TestCase):
    def test_langlink_used_when_present_and_original_otherwise(self):
        fake = FakeWiki(langlinks={"Foo_Bar": "福巴"})
        self.assertEqual(wt.zh_title("de", "Foo_Bar", fake), "福巴")
        self.assertIsNone(wt.zh_title("de", "Nope", fake))


class BuildTests(unittest.TestCase):
    def _fake(self, title="Deep_Sea_Creature", extract=EXTRACT, daily=None):
        return FakeWiki(
            top={"ja": [title]},
            daily={title: daily if daily is not None else _surging_daily()},
            extracts={("ja", title): extract},
            langlinks={title: "深海生物"},
        )

    def test_success_output_has_six_lines_with_code_filled_numbers(self):
        fake = self._fake()
        payloads = []

        def model(system_prompt, payload):
            payloads.append(payload)
            return _good_model(system_prompt, payload)

        result = _build(fake, model)
        lines = result.split("\n")
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[0], "日语维基百科 · 《深海生物》(原题:Deep Sea Creature)")
        self.assertEqual(lines[1], "这是一种栖息在深海热泉附近的虚构海洋生物，身体半透明。")
        self.assertEqual(lines[2], "原文:“它在1998年首次出现”")
        self.assertEqual(lines[3], "译:它在1998年首次出现")
        self.assertEqual(lines[4], "10月17日浏览 5,000 次,约为平日的 10 倍(基线日均约 500)")
        self.assertEqual(
            lines[5],
            "出处: 日语维基百科 https://ja.wikipedia.org/wiki/Deep_Sea_Creature",
        )
        self.assertEqual(payloads[0]["lang_zh"], "日语")
        self.assertEqual(payloads[0]["title"], "Deep Sea Creature")
        self.assertEqual(payloads[0]["views"], 5000)
        self.assertEqual(payloads[0]["baseline"], 500)

    def test_same_title_has_no_original_suffix(self):
        fake = self._fake(title="Plain")
        fake.langlinks["Plain"] = "Plain"
        result = _build(fake, _good_model)
        self.assertEqual(result.split("\n")[0], "日语维基百科 · 《Plain》")

    def test_no_langlink_uses_original_title(self):
        fake = self._fake(title="Lonely_Page")
        fake.langlinks.clear()
        result = _build(fake, _good_model)
        self.assertEqual(result.split("\n")[0], "日语维基百科 · 《Lonely Page》")

    def test_quote_must_be_verbatim_substring(self):
        def model(system_prompt, payload):
            data = _good_model(system_prompt, payload)
            data["quote"] = "它在1998年被发现"  # 不是原文
            return data

        self.assertIsNone(_build(self._fake(), model))

    def test_quote_length_is_bounded(self):
        def model(system_prompt, payload):
            data = _good_model(system_prompt, payload)
            data["quote"] = "短句"  # 少于 8 字符
            return data

        self.assertIsNone(_build(self._fake(), model))

    def test_number_missing_from_extract_is_rejected(self):
        def model(system_prompt, payload):
            data = _good_model(system_prompt, payload)
            data["summary"] = "这是一种2077年出现的虚构海洋生物，栖息在深海热泉附近。"
            return data

        self.assertIsNone(_build(self._fake(), model))

    def test_number_in_quote_zh_missing_from_extract_is_rejected(self):
        def model(system_prompt, payload):
            data = _good_model(system_prompt, payload)
            data["quote_zh"] = "它在2001年首次出现"
            return data

        self.assertIsNone(_build(self._fake(), model))

    def test_invalid_model_then_second_candidate_succeeds(self):
        fake = FakeWiki(
            top={"ja": ["Bad_One", "Good_One"]},
            daily={
                "Bad_One": _days(YESTERDAY, [500] * 14 + [9000]),
                "Good_One": _days(YESTERDAY, [500] * 14 + [5000]),
            },
            extracts={("ja", "Bad_One"): EXTRACT, ("ja", "Good_One"): EXTRACT},
        )
        calls = []

        def model(system_prompt, payload):
            calls.append(payload["title"])
            data = _good_model(system_prompt, payload)
            if payload["title"] == "Bad One":
                data["quote"] = "不在原文里的句子"
            return data

        result = _build(fake, model)
        self.assertEqual(calls, ["Bad One", "Good One"])
        self.assertIn("(基线日均约 500)", result)
        self.assertIn("10月17日浏览 5,000 次", result)

    def test_at_most_two_model_calls_per_language(self):
        titles = [f"Title_{i}" for i in range(5)]
        fake = FakeWiki(
            top={"ja": titles},
            daily={t: _days(YESTERDAY, [500] * 14 + [5000 + i]) for i, t in enumerate(titles)},
            extracts={("ja", t): EXTRACT for t in titles},
        )
        calls = []

        def model(system_prompt, payload):
            calls.append(payload["title"])
            return {"summary": "", "quote": "", "quote_zh": ""}

        self.assertIsNone(_build(fake, model))
        self.assertEqual(len(calls), wt.MAX_MODEL_CANDIDATES)

    def test_only_top_12_surge_candidates_are_evaluated(self):
        titles = [f"Title_{i}" for i in range(13)]
        daily = {t: _days(YESTERDAY, [500] * 15) for t in titles}
        daily["Title_12"] = _surging_daily()  # 第 13 名暴涨，不应被评估
        fake = FakeWiki(top={"ja": titles}, daily=daily,
                        extracts={("ja", t): EXTRACT for t in titles})
        self.assertIsNone(_build(fake, _good_model))

        daily["Title_12"] = _days(YESTERDAY, [500] * 15)
        daily["Title_11"] = _surging_daily()  # 第 12 名暴涨，应被评估
        fake = FakeWiki(top={"ja": titles}, daily=daily,
                        extracts={("ja", t): EXTRACT for t in titles})
        result = _build(fake, _good_model)
        self.assertIsNotNone(result)
        self.assertIn("Title 11", result.split("\n")[0])

    def test_surge_requests_are_spaced_by_0_4_seconds(self):
        titles = ["Title_0", "Title_1", "Title_2"]
        fake = FakeWiki(
            top={"ja": titles},
            daily={t: _surging_daily() for t in titles},
            extracts={("ja", t): EXTRACT for t in titles},
        )
        sleeps, record = _recorder()
        _build(fake, _good_model, sleep=record)
        self.assertEqual(sleeps, [wt.SURGE_GAP_SECONDS, wt.SURGE_GAP_SECONDS])

    def test_model_exception_returns_none(self):
        def model(system_prompt, payload):
            raise RuntimeError("boom")

        self.assertIsNone(_build(self._fake(), model))

    def test_model_non_dict_returns_none(self):
        self.assertIsNone(_build(self._fake(), lambda *a: "text"))

    def test_overall_exception_returns_none(self):
        def broken_get(url):
            raise OSError("network down")

        self.assertIsNone(wt.build_wiki_trending(0, NOW, get=broken_get, model=_good_model, sleep=_no_sleep))

    def test_avoid_titles_are_skipped(self):
        self.assertIsNone(_build(self._fake(), _good_model, avoid_titles=("Deep Sea Creature",)))

    def test_namespace_pages_never_reach_model(self):
        fake = FakeWiki(
            top={"ja": ["Special:Random", "Wikipedia:Foo"]},
            daily={"Special:Random": _surging_daily(), "Wikipedia:Foo": _surging_daily()},
        )
        calls = []
        result = _build(fake, lambda *a: calls.append(a) or {})
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_prompt_has_no_topic_restriction(self):
        self.assertNotIn("拒绝", wt.WIKI_TRENDING_PROMPT.split("6.")[0])
        self.assertIn("不要因为条目涉及的题材", wt.WIKI_TRENDING_PROMPT)


if __name__ == "__main__":
    unittest.main()
