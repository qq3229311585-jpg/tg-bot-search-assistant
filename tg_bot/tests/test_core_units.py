#!/usr/bin/env python3
import contextlib
import importlib
import os
import shutil
import stat
import sys
import tempfile
import unittest

from tg_bot.core.pipeline import _critic_budget, _enforce_short_length
from tg_bot.lanes.router import decide_lane
from tg_bot.search_policy import decide_search_policy
from tg_bot.workers.source_utils import (
    cache_match_score,
    compact_excerpt,
    fact_list_supports_query,
    is_nav_or_empty,
    source_matches_query,
)
from tg_bot.workers.gather_tools import (
    build_cache_entries,
    build_fetch_entry,
    build_wikipedia_entry,
    extract_fetch_title,
    parse_search_entries,
)
from tg_bot.workers.display import clean_reply_for_user
from tg_bot.workers.facts_builder import build_minimal_facts_json
from tg_bot.agents.curator import curate
from tg_bot.core.contracts import PipelineConfig, Source, WriteRequest
import tg_bot.workers.gather_executor as gather_executor
from tg_bot.workers.gather_executor import GatherExecContext, execute_gather_tool
from tg_bot.workers.gather_fallback import finalize_round_limit, parse_gather_completion
from tg_bot.workers.source_backfill import complete_source_index, dedupe_source_index


@contextlib.contextmanager
def temporary_env(**updates):
    """Temporarily update env vars and restore the process exactly afterward."""
    sentinel = object()
    original = {key: os.environ.get(key, sentinel) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in original.items():
            if value is sentinel:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def reload_config():
    sys.modules.pop("tg_bot.config", None)
    return importlib.import_module("tg_bot.config")


def required_env(**overrides):
    values = {
        "BOT_TOKEN": "test-bot-token",
        "ALLOWED_CHAT": "1",
        "DEEPSEEK_KEY_0": "test-writing-key",
        "DEEPSEEK_VERIFY_KEY_0": "test-verify-key",
        "BRAVE_KEY": "test-brave-key",
        "TAVILY_KEY_0": "test-tavily-key",
        "SERPER_KEY_0": "test-serper-key",
    }
    values.update(overrides)
    return values


class ConfigTests(unittest.TestCase):
    def test_config_uses_custom_data_dir_without_import_side_effect(self):
        temp_dir = tempfile.mkdtemp(prefix="tg-bot-config-")
        shutil.rmtree(temp_dir)
        try:
            with temporary_env(**required_env(TG_BOT_DATA_DIR=temp_dir)):
                cfg = reload_config()
                self.assertEqual(cfg.DATA_DIR, temp_dir)
                self.assertFalse(os.path.exists(temp_dir))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_config_reports_missing_deepseek_key(self):
        with temporary_env(**required_env(DEEPSEEK_KEY_0=None)):
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_KEY_0"):
                reload_config()

    def test_config_allows_search_provider_keys_to_be_empty(self):
        with temporary_env(**required_env(TAVILY_KEY_0=None, SERPER_KEY_0=None)):
            cfg = reload_config()
            self.assertEqual(cfg.TAVILY_KEYS, [])
            self.assertEqual(cfg.SERPER_KEYS, [])

    def test_ensure_data_dir_creates_private_directory(self):
        temp_dir = tempfile.mkdtemp(prefix="tg-bot-config-")
        shutil.rmtree(temp_dir)
        try:
            with temporary_env(**required_env(TG_BOT_DATA_DIR=temp_dir)):
                cfg = reload_config()
                cfg.ensure_data_dir()
                mode = stat.S_IMODE(os.stat(temp_dir).st_mode)
                self.assertEqual(mode, 0o700)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class SearchPolicyTests(unittest.TestCase):
    def test_emotion_with_today_stays_fast(self):
        pre = {"needs_search": False, "query_type": "闲聊", "keywords": []}
        route = decide_search_policy("我今天很难过", pre)
        self.assertEqual(route["route"], "fast")
        self.assertEqual(route["category"], "情感闲聊")

    def test_assistant_mood_with_today_stays_fast(self):
        pre = {"needs_search": False, "query_type": "闲聊", "keywords": []}
        route = decide_search_policy("你今天心情如何？", pre)
        self.assertEqual(route["route"], "fast")
        self.assertEqual(route["category"], "闲聊")

    def test_fresh_news_searches(self):
        pre = {"needs_search": False, "query_type": "其他", "keywords": []}
        route = decide_search_policy("今天 AI 有什么新闻", pre)
        self.assertEqual(route["route"], "search")

    def test_assistant_continue_stays_fast(self):
        pre = {
            "needs_search": False,
            "query_type": "闲聊",
            "keywords": [],
            "speech_act": "continue_previous",
            "addressing_assistant": True,
            "needs_external_evidence": False,
            "reason": "用户回应助手上一轮社交提议",
        }
        route = decide_search_policy("你讲吧", pre)
        self.assertEqual(route["route"], "fast")
        self.assertEqual(route["category"], "闲聊")

    def test_explicit_continue_search_still_searches(self):
        pre = {
            "needs_search": True,
            "query_type": "搜索",
            "keywords": ["世界杯", "具体球场"],
            "speech_act": "continue_previous",
            "needs_external_evidence": True,
        }
        route = decide_search_policy("继续查一下世界杯具体球场", pre)
        self.assertEqual(route["route"], "search")

    def test_local_tool_path_search_route(self):
        pre = {
            "needs_search": True,
            "query_type": "系统查询",
            "keywords": ["VPS流量"],
            "speech_act": "tool_request",
            "needs_local_tool": True,
            "local_tool_hint": "vps_traffic",
            "needs_external_evidence": False,
        }
        route = decide_search_policy("查一下 VPS 流量", pre)
        self.assertEqual(route["route"], "search")
        self.assertTrue(route["needs_local_tool"])


class LaneRouterTests(unittest.TestCase):
    def test_fast_lane(self):
        lane = decide_lane(needs_search=False, route_info={"category": "闲聊"})
        self.assertEqual(lane.name, "fast")

    def test_search_lane(self):
        lane = decide_lane(needs_search=True, route_info={"category": "知识/原理"})
        self.assertEqual(lane.name, "search")

    def test_vps_traffic_lane(self):
        lane = decide_lane(
            needs_search=False,
            route_info={"category": "系统查询"},
            local_evidence_kind="vps_traffic",
        )
        self.assertEqual(lane.name, "local_tool")

    def test_report_lane(self):
        lane = decide_lane(
            needs_search=False,
            route_info={"category": "闲聊"},
            local_evidence_kind="today_report",
        )
        self.assertEqual(lane.name, "report")


class LengthGuardTests(unittest.TestCase):
    def test_short_length_guard_trims_overlong_reply(self):
        text = "。".join([f"事实{i}" for i in range(80)]) + "。"
        trimmed = _enforce_short_length(text, "写一个300字的介绍", (255, 345))
        self.assertLessEqual(len(trimmed), 345)
        self.assertGreater(len(trimmed), 0)

    def test_long_length_guard_does_not_touch_long_targets(self):
        text = "这是正常长文。"
        self.assertEqual(_enforce_short_length(text, "详细介绍", (600, 1200)), text)

    def test_critic_budget_short_single_item(self):
        req = WriteRequest("搜一个冷知识", [], (80, 200), style_hints=["single_item"])
        b = _critic_budget("搜一个冷知识", req, PipelineConfig(max_rewrites=2))
        self.assertEqual(b["level"], "short")
        self.assertFalse(b["reaudit_after_fix"])
        self.assertFalse(b["allow_rewrite"])
        self.assertEqual(b["max_fix_cycles"], 1)

    def test_critic_budget_normal_one_fix_cycle(self):
        req = WriteRequest("讲讲苏格兰独角兽", [], (500, 1200))
        b = _critic_budget("讲讲苏格兰独角兽", req, PipelineConfig(max_rewrites=2))
        self.assertEqual(b["level"], "normal")
        self.assertTrue(b["reaudit_after_fix"])
        self.assertEqual(b["max_fix_cycles"], 1)

    def test_critic_budget_high_risk_two_fix_cycles(self):
        req = WriteRequest("这个药物治疗方案可靠吗", [], (500, 1200))
        b = _critic_budget("这个药物治疗方案可靠吗", req, PipelineConfig(max_rewrites=3))
        self.assertEqual(b["level"], "high_risk")
        self.assertEqual(b["max_fix_cycles"], 2)


class SourceUtilsTests(unittest.TestCase):
    def test_cross_language_disaster_match(self):
        self.assertTrue(source_matches_query(
            "龙卷风 应对",
            "Tornado Preparedness",
            "Know where to shelter during a tornado.",
            "ready.gov",
            "https://www.ready.gov/tornadoes",
        ))

    def test_nav_or_empty_page(self):
        self.assertTrue(is_nav_or_empty("News Headlines", "short"))
        self.assertFalse(is_nav_or_empty("Tornado Safety", "正文" * 120))

    def test_cache_match_score_ignores_generic_terms(self):
        self.assertGreater(cache_match_score("许海峰 1984 洛杉矶奥运 首金", ["中国", "许海峰"]), 0)

    def test_fact_list_supports_chinese_short_keywords(self):
        fact_list = "[F001] 龙卷风发生时应进入低层无窗房间。"
        self.assertTrue(fact_list_supports_query(fact_list, ["龙卷风", "应对"]))

    def test_compact_excerpt_prefers_matching_lines(self):
        text = "导航菜单\n" + "龙卷风来临时，应立即前往地下室或低层无窗房间躲避。" * 4
        self.assertIn("龙卷风", compact_excerpt(text, "龙卷风 应对", 120))


class DisplayAndFactsTests(unittest.TestCase):
    def test_clean_reply_for_user_removes_source_markers(self):
        self.assertEqual(clean_reply_for_user("A[来源1][来源2]  B"), "A B")

    def test_minimal_facts_json_from_sources(self):
        facts = build_minimal_facts_json([
            {"id": "R001", "title": "标题1", "domain": "a.com", "url": "https://a.com", "snippet": "摘要1" * 80, "tool": "web_search"},
            {"id": "R002", "title": "标题2", "domain": "b.com", "url": "https://b.com", "full_content": "正文2" * 120, "tool": "fetch_content"},
        ])
        self.assertEqual(facts["fact_count"], 2)
        self.assertEqual(facts["facts"][0]["fact_id"], "F001")
        self.assertEqual(facts["facts"][1]["source_id"], "R002")

    def test_minimal_facts_json_filters_ad_lines(self):
        facts = build_minimal_facts_json([
            {
                "id": "R001",
                "title": "177 Weird Facts",
                "domain": "classpop.com",
                "url": "https://www.classpop.com/magazine/weird-facts",
                "full_content": (
                    "[正文来源：https://www.classpop.com/magazine/weird-facts]\n"
                    "BUY A GIFT CARD\n"
                    "![ad](data:image/svg+xml;base64,abc)\n"
                    "[Listen](javascript:popUpplayer('x'))\n"
                    "Recommended by\n"
                    "Giraffes are 30 times more likely to be killed by lightning than humans."
                ),
                "tool": "fetch_content",
            }
        ])
        excerpt = facts["facts"][0]["excerpt"]
        self.assertNotIn("GIFT CARD", excerpt)
        self.assertNotIn("正文来源", excerpt)
        self.assertNotIn("javascript", excerpt)
        self.assertNotIn("data:image", excerpt)
        self.assertIn("Giraffes", excerpt)


class GatherToolWorkerTests(unittest.TestCase):
    def test_parse_search_entries(self):
        seq = iter(["R001"])
        result = "• Tornado Safety\nShelter in a basement.\nhttps://www.ready.gov/tornadoes"
        entries = parse_search_entries(
            result=result, query="龙卷风 应对", tool="web_search", next_rid=lambda: next(seq)
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "R001")
        self.assertEqual(entries[0]["domain"], "www.ready.gov")

    def test_build_fetch_entry_skips_nav(self):
        entry = build_fetch_entry(
            url="https://example.com/nav",
            result="[正文来源：https://example.com/nav]\nNews Headlines",
            next_rid=lambda: "R001",
        )
        self.assertIsNone(entry)

    def test_extract_fetch_title_prefers_heading_over_ad_banner(self):
        result = (
            "[正文来源：https://www.classpop.com/magazine/weird-facts]\n"
            "10% OFF GIFT CARDS\n"
            "# 177 Weird Facts That Are Strange But True\n"
            "Here are surprising facts."
        )
        self.assertEqual(
            extract_fetch_title(result, "https://www.classpop.com/magazine/weird-facts"),
            "177 Weird Facts That Are Strange But True",
        )

    def test_build_wikipedia_entry(self):
        entry = build_wikipedia_entry(
            query="Mars",
            result="【英文Wikipedia】Mars:\nMars is the fourth planet.",
            next_rid=lambda: "R001",
        )
        self.assertEqual(entry["title"], "Mars")
        self.assertEqual(entry["tool"], "wikipedia_lookup")

    def test_build_cache_entries(self):
        rows = '[{"id":"C001","title":"缓存标题","url":"https://example.com/a","snippet":"摘要"}]'
        entries = build_cache_entries(result=rows, next_rid=lambda: "R001", existing_ids=set())
        self.assertEqual(entries[0]["id"], "C001")
        self.assertEqual(entries[0]["tool"], "read_today_cache")


class GatherExecutorTests(unittest.TestCase):
    def _ctx(self):
        saved = []
        seq = iter(["R001", "R002", "R003"])
        ctx = GatherExecContext(
            user_text="测试问题",
            source_index=[],
            url_to_entry={},
            meta={"tool_results": [], "fetched_pages": [], "failed_urls": []},
            next_rid=lambda: next(seq),
            persist=saved.append,
        )
        return ctx, saved

    def test_execute_web_search_adds_source(self):
        old = gather_executor.execute_search
        try:
            gather_executor.execute_search = lambda q, stype: (
                "• Tornado Safety\nShelter in a basement.\nhttps://www.ready.gov/tornadoes"
            )
            ctx, saved = self._ctx()
            result = execute_gather_tool("web_search", {"query": "龙卷风"}, ctx)
            self.assertIn("Tornado Safety", result)
            self.assertEqual(ctx.source_index[0]["domain"], "www.ready.gov")
            self.assertEqual(saved[0]["tool"], "web_search")
        finally:
            gather_executor.execute_search = old

    def test_execute_fetch_blocks_known_domain(self):
        ctx, _saved = self._ctx()
        result = execute_gather_tool("fetch_content", {"url": "https://www.zhihu.com/question/1"}, ctx)
        self.assertIn("跳过抓取", result)
        self.assertEqual(ctx.meta["failed_urls"], ["https://www.zhihu.com/question/1"])
        self.assertFalse(ctx.source_index)

    def test_execute_wikipedia_adds_source(self):
        old = gather_executor.execute_wikipedia
        try:
            gather_executor.execute_wikipedia = lambda q: "【英文Wikipedia】Mars:\nMars is the fourth planet."
            ctx, saved = self._ctx()
            result = execute_gather_tool("wikipedia_lookup", {"query": "Mars"}, ctx)
            self.assertIn("Mars is", result)
            self.assertEqual(ctx.source_index[0]["title"], "Mars")
            self.assertEqual(saved[0]["tool"], "wikipedia_lookup")
        finally:
            gather_executor.execute_wikipedia = old

    def test_execute_cache_adds_entries_without_persisting(self):
        old = gather_executor.execute_read_cache
        try:
            gather_executor.execute_read_cache = lambda ids, level: (
                '[{"id":"C001","title":"缓存标题","url":"https://example.com/a","snippet":"摘要"}]'
            )
            ctx, saved = self._ctx()
            result = execute_gather_tool("read_today_cache", {"ids": ["C001"], "level": "snippet"}, ctx)
            self.assertIn("缓存标题", result)
            self.assertEqual(ctx.source_index[0]["id"], "C001")
            self.assertFalse(saved)
        finally:
            gather_executor.execute_read_cache = old


class SourceBackfillTests(unittest.TestCase):
    def test_complete_source_index_from_search_result(self):
        seq = iter(["R001"])
        tool_results = [{
            "tool": "web_search",
            "query": "龙卷风",
            "snippet": "• Tornado Safety\nShelter in a basement.\nhttps://www.ready.gov/tornadoes",
        }]
        sources = complete_source_index([], tool_results, lambda: next(seq), {"check_weather"})
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["domain"], "www.ready.gov")

    def test_dedupe_keeps_latest_direct_api(self):
        sources = dedupe_source_index([
            {"tool": "check_weather", "domain": "check_weather", "snippet": "old"},
            {"tool": "check_weather", "domain": "check_weather", "snippet": "new"},
        ])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["snippet"], "new")


class CuratorTests(unittest.TestCase):
    def test_single_item_hint_for_one_fun_fact(self):
        req = curate(
            [
                Source(
                    id="R001",
                    url="https://example.com/a",
                    domain="example.com",
                    title="fun facts",
                    snippet="长颈鹿被闪电击中的概率更高。" * 20,
                    full_content="长颈鹿被闪电击中的概率更高。" * 80,
                    tool="fetch_content",
                )
            ],
            user_query="那你搜一个冷知识吧",
            keywords=["冷知识"],
        )
        self.assertIn("single_item", req.style_hints)
        self.assertEqual(req.target_words, (80, 200))


class GatherFallbackTests(unittest.TestCase):
    def test_parse_gather_completion(self):
        parsed = parse_gather_completion('说明 {"sufficient": false, "reason": "素材不足", "suggested_length": "short"}')
        self.assertFalse(parsed["sufficient"])
        self.assertEqual(parsed["reason"], "素材不足")
        self.assertEqual(parsed["suggested_length"], "short")

    def test_finalize_round_limit(self):
        sources, meta = finalize_round_limit([{"id": "R001"}], {})
        self.assertTrue(meta["sufficient"])
        self.assertEqual(meta["source_index"], sources)


if __name__ == "__main__":
    unittest.main()
