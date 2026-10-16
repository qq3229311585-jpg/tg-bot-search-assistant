#!/usr/bin/env python3
import contextlib
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import http.client
import importlib
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

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
from tg_bot.memory import build_memory_context


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

    def test_invalid_daily_report_timezone_is_rejected(self):
        with temporary_env(**required_env(DAILY_REPORT_TIMEZONE="Not/AZone")):
            with self.assertRaisesRegex(RuntimeError, "DAILY_REPORT_TIMEZONE"):
                reload_config()

    def test_daily_report_event_section_target_defaults_to_three(self):
        with temporary_env(**required_env(DAILY_REPORT_EVENT_SECTION_TARGET=None)):
            cfg = reload_config()
            self.assertEqual(cfg.DAILY_REPORT_EVENT_SECTION_TARGET, 3)

    def test_daily_report_event_section_target_accepts_only_one_through_six(self):
        for value in (1, 6):
            with self.subTest(value=value), temporary_env(**required_env(
                DAILY_REPORT_EVENT_SECTION_TARGET=str(value),
            )):
                cfg = reload_config()
                self.assertEqual(cfg.DAILY_REPORT_EVENT_SECTION_TARGET, value)
        for value in (0, 7):
            with self.subTest(value=value), temporary_env(**required_env(
                DAILY_REPORT_EVENT_SECTION_TARGET=str(value),
            )):
                with self.assertRaisesRegex(RuntimeError, "DAILY_REPORT_EVENT_SECTION_TARGET"):
                    reload_config()

    def test_daily_report_research_budgets_have_bounded_defaults(self):
        keys = {
            "DAILY_REPORT_FOLLOWUP_QUERIES_PER_EVENT": None,
            "DAILY_REPORT_FETCHES_PER_EVENT": None,
            "DAILY_REPORT_MIN_INDEPENDENT_SOURCES": None,
            "DAILY_REPORT_EVIDENCE_TOP_K": None,
            "DAILY_REPORT_MAX_EVIDENCE_CHARS": None,
            "DAILY_REPORT_SUMMARY_RETRIES": None,
            "DAILY_REPORT_VERIFY_RETRIES": None,
        }
        with temporary_env(**required_env(**keys)):
            cfg = reload_config()
        self.assertEqual(cfg.DAILY_REPORT_FOLLOWUP_QUERIES_PER_EVENT, 2)
        self.assertEqual(cfg.DAILY_REPORT_FETCHES_PER_EVENT, 3)
        self.assertEqual(cfg.DAILY_REPORT_MIN_INDEPENDENT_SOURCES, 1)
        self.assertEqual(cfg.DAILY_REPORT_EVIDENCE_TOP_K, 3)
        self.assertEqual(cfg.DAILY_REPORT_MAX_EVIDENCE_CHARS, 18000)
        self.assertEqual(cfg.DAILY_REPORT_SUMMARY_RETRIES, 2)
        self.assertEqual(cfg.DAILY_REPORT_VERIFY_RETRIES, 1)

    def test_daily_report_research_budgets_accept_only_documented_bounds(self):
        cases = (
            ("DAILY_REPORT_FOLLOWUP_QUERIES_PER_EVENT", 1, 3),
            ("DAILY_REPORT_FETCHES_PER_EVENT", 1, 5),
            ("DAILY_REPORT_MIN_INDEPENDENT_SOURCES", 1, 3),
            ("DAILY_REPORT_EVIDENCE_TOP_K", 1, 6),
            ("DAILY_REPORT_MAX_EVIDENCE_CHARS", 4000, 40000),
            ("DAILY_REPORT_SUMMARY_RETRIES", 1, 4),
            ("DAILY_REPORT_VERIFY_RETRIES", 1, 3),
        )
        for key, minimum, maximum in cases:
            for value in (minimum, maximum):
                with self.subTest(key=key, value=value), temporary_env(
                    **required_env(**{key: str(value)})
                ):
                    self.assertEqual(getattr(reload_config(), key), value)
            for value in (minimum - 1, maximum + 1):
                with self.subTest(key=key, value=value), temporary_env(
                    **required_env(**{key: str(value)})
                ):
                    with self.assertRaisesRegex(RuntimeError, key):
                        reload_config()

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

    def test_short_api_token_is_rejected_by_validation(self):
        with temporary_env(**required_env(ASK_API_TOKEN="short")):
            cfg = reload_config()
            diagnostics = cfg.validate_config()
            self.assertFalse(diagnostics["ok"])
            self.assertTrue(any("ASK_API_TOKEN" in item for item in diagnostics["errors"]))

    def test_daily_report_health_rejects_stale_status(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-health-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_MAX_STALE_HOURS="1",
            )):
                cfg = reload_config()
                with open(cfg.DAILY_REPORT_STATUS_FILE, "w", encoding="utf-8") as handle:
                    json.dump({
                        "status": "fresh",
                        "generated_at": "2026-07-12T10:00:00+00:00",
                    }, handle)
                health = cfg.daily_report_health(
                    now=cfg.datetime(2026, 7, 12, 12, 30, tzinfo=cfg.timezone.utc)
                )
                self.assertFalse(health["ok"])
                self.assertEqual(health["status"], "stale")
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_daily_report_health_accepts_fresh_no_publishable_content(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-health-empty-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_MAX_STALE_HOURS="1",
            )):
                cfg = reload_config()
                with open(cfg.DAILY_REPORT_STATUS_FILE, "w", encoding="utf-8") as handle:
                    json.dump({
                        "status": "no_publishable_content",
                        "generated_at": "2026-07-12T12:00:00+00:00",
                    }, handle)
                health = cfg.daily_report_health(
                    now=cfg.datetime(2026, 7, 12, 12, 30, tzinfo=cfg.timezone.utc)
                )
                self.assertTrue(health["ok"])
                self.assertEqual(health["status"], "no_publishable_content")
                self.assertTrue(any("无可发布内容" in item for item in health["warnings"]))
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_daily_report_health_rejects_stale_no_publishable_content(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-health-empty-stale-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_MAX_STALE_HOURS="1",
            )):
                cfg = reload_config()
                with open(cfg.DAILY_REPORT_STATUS_FILE, "w", encoding="utf-8") as handle:
                    json.dump({
                        "status": "no_publishable_content",
                        "generated_at": "2026-07-12T10:00:00+00:00",
                    }, handle)
                health = cfg.daily_report_health(
                    now=cfg.datetime(2026, 7, 12, 12, 30, tzinfo=cfg.timezone.utc)
                )
                self.assertFalse(health["ok"])
                self.assertEqual(health["status"], "stale")
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


class SearchProviderTests(unittest.TestCase):
    def test_serper_without_key_is_explicitly_unavailable(self):
        with temporary_env(**required_env(TAVILY_KEY_0=None, SERPER_KEY_0=None)):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            self.assertEqual(search._execute_serper("test", "general"), "Serper 未配置")

    def test_all_news_candidates_calls_every_provider_and_dedupes_tracking_urls(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            calls = []
            first = {
                "title": "First report",
                "summary": "kept summary",
                "url": "HTTPS://X.TEST/story/?utm_source=newsletter&GCLID=click&keep=1#details",
                "published_at": "2026-07-17T04:00:00+00:00",
                "relevance": 0.9,
                "source": "brave",
            }
            duplicate = {
                "title": "Duplicate report",
                "summary": "later duplicate",
                "url": "https://x.test/story?keep=1&utm_medium=feed",
                "published_at": "2026-07-17T04:05:00+00:00",
                "source": "tavily",
            }
            distinct = {
                "title": "Distinct report",
                "summary": "distinct summary",
                "url": "https://y.test/story?keep=1&fbclid=click",
                "published_at": "2026-07-17T04:10:00+00:00",
                "source": "serper",
            }

            def fake(name, rows):
                def run(_query):
                    calls.append(name)
                    return rows, []
                return run

            items, diagnostics = search.execute_news_candidates_all(
                "query",
                providers=(
                    ("brave", fake("brave", [first])),
                    ("tavily", fake("tavily", [duplicate])),
                    ("serper", fake("serper", [distinct])),
                ),
            )
            self.assertEqual(calls, ["brave", "tavily", "serper"])
            self.assertEqual(items, [first, distinct])
            self.assertEqual(diagnostics, [])

    def test_all_news_candidates_preserves_identity_query_parameters(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            for parameter in ("source", "from", "ref", "referrer"):
                with self.subTest(parameter=parameter):
                    first = {"title": "A", "url": f"https://x.test/story?{parameter}=A"}
                    second = {"title": "B", "url": f"https://x.test/story?{parameter}=B"}
                    items, diagnostics = search.execute_news_candidates_all(
                        "query",
                        providers=(
                            ("brave", lambda _query, row=first: ([row], [])),
                            ("tavily", lambda _query, row=second: ([row], [])),
                        ),
                    )
                    self.assertEqual(items, [first, second])
                    self.assertEqual(diagnostics, [])

    def test_all_news_candidates_keeps_query_parameter_order_distinct(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            first = {"title": "A", "url": "https://x.test/story?a=1&b=2"}
            second = {"title": "B", "url": "https://x.test/story?b=2&a=1"}
            items, diagnostics = search.execute_news_candidates_all(
                "query",
                providers=(
                    ("brave", lambda _query: ([first], [])),
                    ("tavily", lambda _query: ([second], [])),
                ),
            )
            self.assertEqual(items, [first, second])
            self.assertEqual(diagnostics, [])

    def test_all_news_candidates_continues_after_provider_exception(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            calls = []

            def first(_query):
                calls.append("brave")
                return [{"title": "First", "url": "https://one.test/a"}], []

            def broken(_query):
                calls.append("tavily")
                raise RuntimeError("secret-key-must-not-appear")

            def last(_query):
                calls.append("serper")
                return [{"title": "Last", "url": "https://two.test/b"}], []

            items, diagnostics = search.execute_news_candidates_all(
                "query",
                providers=(("brave", first), ("tavily", broken), ("serper", last)),
            )
            self.assertEqual(calls, ["brave", "tavily", "serper"])
            self.assertEqual([item["title"] for item in items], ["First", "Last"])
            self.assertEqual(diagnostics, ["tavily: provider_error:RuntimeError"])
            self.assertNotIn("secret-key", " ".join(diagnostics))

    def test_all_news_candidates_skips_bad_rows_and_keeps_valid_rows(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            calls = []
            brave_item = {
                "title": "Brave valid",
                "url": "https://brave.test/a",
                "source": "brave",
            }

            def mixed(_query):
                calls.append("brave")
                return [None, "bad", brave_item], ["brave: partial"]

            def tavily(_query):
                calls.append("tavily")
                return [{"title": "Tavily", "url": "https://tavily.test/a"}], []

            def serper(_query):
                calls.append("serper")
                return [{"title": "Serper", "url": "https://serper.test/a"}], []

            try:
                items, diagnostics = search.execute_news_candidates_all(
                    "query",
                    providers=(("brave", mixed), ("tavily", tavily), ("serper", serper)),
                )
            except Exception as exc:
                self.fail(f"malformed row escaped provider isolation: {type(exc).__name__}")
            self.assertEqual(calls, ["brave", "tavily", "serper"])
            self.assertEqual(
                [item["title"] for item in items],
                ["Brave valid", "Tavily", "Serper"],
            )
            self.assertEqual(diagnostics, [
                "brave: partial",
                "brave: provider_error:TypeError",
            ])

    def test_all_news_candidates_isolates_provider_with_only_bad_rows(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            calls = []

            def first(_query):
                calls.append("brave")
                return [{"title": "First", "url": "https://one.test/a"}], []

            def malformed(_query):
                calls.append("tavily")
                return [None, "bad"], []

            def last(_query):
                calls.append("serper")
                return [{"title": "Last", "url": "https://two.test/b"}], []

            try:
                items, diagnostics = search.execute_news_candidates_all(
                    "query",
                    providers=(("brave", first), ("tavily", malformed), ("serper", last)),
                )
            except Exception as exc:
                self.fail(f"malformed provider escaped isolation: {type(exc).__name__}")
            self.assertEqual(calls, ["brave", "tavily", "serper"])
            self.assertEqual([item["title"] for item in items], ["First", "Last"])
            self.assertEqual(diagnostics, ["tavily: provider_error:TypeError"])

    def test_all_news_candidates_continues_after_empty_provider(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            calls = []

            def empty(_query):
                calls.append("brave")
                return [], ["brave: empty"]

            def found(_query):
                calls.append("tavily")
                return [{"title": "Found", "url": "https://found.test/a"}], []

            items, diagnostics = search.execute_news_candidates_all(
                "query",
                providers=(("brave", empty), ("tavily", found)),
            )
            self.assertEqual(calls, ["brave", "tavily"])
            self.assertEqual([item["title"] for item in items], ["Found"])
            self.assertEqual(diagnostics, ["brave: empty"])

    def test_all_news_candidates_reports_every_unconfigured_provider(self):
        with temporary_env(**required_env()):
            cfg = reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            with patch.object(search, "BRAVE_KEY", ""), patch.object(
                search, "TAVILY_KEYS", []
            ), patch.object(cfg, "SERPER_KEYS", []):
                items, diagnostics = search.execute_news_candidates_all("query")
            self.assertEqual(items, [])
            self.assertEqual(diagnostics, [
                "brave: unavailable",
                "tavily: unavailable",
                "serper: unavailable",
            ])

    def test_all_news_candidates_keeps_items_without_valid_urls_distinct(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            missing = {"title": "Missing URL", "url": "", "source": "brave"}
            malformed = {"title": "Malformed URL", "url": "not a url", "source": "tavily"}
            items, diagnostics = search.execute_news_candidates_all(
                "query",
                providers=(
                    ("brave", lambda _query: ([missing], [])),
                    ("tavily", lambda _query: ([malformed], [])),
                ),
            )
            self.assertEqual(items, [missing, malformed])
            self.assertEqual(diagnostics, [])

    def test_legacy_news_candidates_still_stops_after_first_success(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            brave_item = {"title": "First", "url": "https://first.test/a"}
            with patch.object(
                search, "_news_from_brave", return_value=([brave_item], [])
            ) as brave, patch.object(
                search, "_news_from_tavily", return_value=([{"title": "Second"}], [])
            ) as tavily, patch.object(
                search, "_news_from_serper", return_value=([{"title": "Third"}], [])
            ) as serper:
                items, diagnostics = search.execute_news_candidates("query")
            self.assertEqual(items, [brave_item])
            self.assertEqual(diagnostics, [])
            brave.assert_called_once_with("query")
            tavily.assert_not_called()
            serper.assert_not_called()

    def test_legacy_news_candidates_preserves_nonempty_raw_response_semantics(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            payload = json.dumps({
                "results": [{"title": "", "url": "https://filtered.test/a"}],
            })
            with patch(
                "tg_bot.tools.fetch.http_get", return_value=payload
            ), patch.object(search, "inc_quota") as quota, patch.object(
                search,
                "_news_from_tavily",
                return_value=([{"title": "Later", "url": "https://later.test/a"}], []),
            ) as tavily:
                items, diagnostics = search.execute_news_candidates("query")
            self.assertEqual(items, [])
            self.assertEqual(diagnostics, [])
            quota.assert_called_once_with("brave")
            tavily.assert_not_called()

    def test_brave_adapter_keeps_valid_rows_when_response_is_mixed(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            payload = json.dumps({"results": [{
                "title": "Brave valid",
                "description": "summary",
                "url": "https://brave.test/a",
                "page_age": "2026-07-17T04:00:00+00:00",
                "score": 0.9,
            }, None, "bad"]})
            with patch(
                "tg_bot.tools.fetch.http_get", return_value=payload
            ), patch.object(search, "inc_quota") as quota:
                items, diagnostics = search._news_from_brave("query")
            self.assertEqual([item["title"] for item in items], ["Brave valid"])
            self.assertEqual(items[0]["source"], "brave")
            self.assertEqual(diagnostics, ["brave: provider_error:TypeError"])
            quota.assert_called_once_with("brave")

    def test_tavily_adapter_keeps_valid_rows_when_response_is_mixed(self):
        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            payload = {"results": [{
                "title": "Tavily valid",
                "content": "summary",
                "url": "https://tavily.test/a",
                "published_date": "2026-07-17T04:00:00+00:00",
                "score": 0.8,
            }, None, "bad"]}
            with patch.object(search, "_tavily_request", return_value=payload):
                items, diagnostics = search._news_from_tavily("query")
            self.assertEqual([item["title"] for item in items], ["Tavily valid"])
            self.assertEqual(items[0]["source"], "tavily")
            self.assertEqual(diagnostics, ["tavily: provider_error:TypeError"])

    def test_serper_adapter_keeps_valid_rows_when_response_is_mixed(self):
        with temporary_env(**required_env(SERPER_KEY_1=None, SERPER_KEY_2=None)):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")

            class FakeResponse:
                def __enter__(self):
                    return self
                def __exit__(self, *_args):
                    return False
                def read(self):
                    return json.dumps({"news": [{
                        "title": "Serper valid",
                        "snippet": "summary",
                        "link": "https://serper.test/a",
                        "date": "1 hour ago",
                        "score": 0.7,
                    }, None, "bad"]}).encode()

            with patch.object(search, "urlopen", return_value=FakeResponse()), patch.object(
                search, "inc_quota"
            ) as quota:
                items, diagnostics = search._news_from_serper("query")
            self.assertEqual([item["title"] for item in items], ["Serper valid"])
            self.assertEqual(items[0]["source"], "serper")
            self.assertEqual(diagnostics, ["serper: provider_error:TypeError"])
            quota.assert_called_once_with("serper")

    def test_tavily_concurrent_retries_use_each_key_once_per_call(self):
        with temporary_env(**required_env(TAVILY_KEY_0="t1", TAVILY_KEY_1="t2")):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            observed = {"lane-a": [], "lane-b": []}
            results = {}
            record_lock = threading.Lock()
            lane_a_first = threading.Event()
            lane_b_first = threading.Event()
            lane_a_second = threading.Event()

            class FakeResponse:
                def __enter__(self):
                    return self
                def __exit__(self, *_args):
                    return False
                def read(self):
                    return json.dumps({"results": [{"title": "ok"}]}).encode()

            def fake_urlopen(request, **_kwargs):
                payload = json.loads(request.data)
                lane = payload["query"]
                key = payload["api_key"]
                with record_lock:
                    observed[lane].append(key)
                    attempt = len(observed[lane])
                    first_key = observed[lane][0]
                if attempt == 1:
                    if lane == "lane-a":
                        lane_a_first.set()
                        lane_b_first.wait(2)
                    else:
                        lane_b_first.set()
                        lane_a_second.wait(2)
                    raise RuntimeError("429 quota")
                if lane == "lane-a":
                    lane_a_second.set()
                if key == first_key:
                    raise RuntimeError("429 quota")
                return FakeResponse()

            def run(lane):
                results[lane] = search._tavily_request(
                    "https://api.tavily.com/search", {"query": lane}
                )

            with patch.object(search, "urlopen", side_effect=fake_urlopen), patch.object(
                search, "inc_quota"
            ) as quota:
                first = threading.Thread(target=run, args=("lane-a",), daemon=True)
                second = threading.Thread(target=run, args=("lane-b",), daemon=True)
                first.start()
                self.assertTrue(lane_a_first.wait(1))
                second.start()
                first.join(3)
                second.join(3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(observed, {
                "lane-a": ["t1", "t2"],
                "lane-b": ["t2", "t1"],
            })
            self.assertTrue(all(results.values()))
            self.assertCountEqual(
                [item.args[0] for item in quota.call_args_list],
                ["tavily_1", "tavily_0"],
            )

    def test_serper_concurrent_retries_use_each_key_once_per_call(self):
        with temporary_env(**required_env(SERPER_KEY_0="s1", SERPER_KEY_1="s2")):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            observed = {"lane-a": [], "lane-b": []}
            results = {}
            record_lock = threading.Lock()
            lane_a_first = threading.Event()
            lane_b_first = threading.Event()
            lane_a_second = threading.Event()

            class FakeResponse:
                def __enter__(self):
                    return self
                def __exit__(self, *_args):
                    return False
                def read(self):
                    return json.dumps({
                        "news": [{"title": "ok", "link": "https://example.test/ok"}],
                    }).encode()

            def fake_urlopen(request, **_kwargs):
                payload = json.loads(request.data)
                lane = payload["q"]
                key = request.get_header("X-api-key")
                with record_lock:
                    observed[lane].append(key)
                    attempt = len(observed[lane])
                    first_key = observed[lane][0]
                if attempt == 1:
                    if lane == "lane-a":
                        lane_a_first.set()
                        lane_b_first.wait(2)
                    else:
                        lane_b_first.set()
                        lane_a_second.wait(2)
                    raise RuntimeError("429 quota")
                if lane == "lane-a":
                    lane_a_second.set()
                if key == first_key:
                    raise RuntimeError("429 quota")
                return FakeResponse()

            def run(lane):
                results[lane] = search._news_from_serper(lane)

            with patch.object(search, "urlopen", side_effect=fake_urlopen), patch.object(
                search, "inc_quota"
            ) as quota:
                first = threading.Thread(target=run, args=("lane-a",), daemon=True)
                second = threading.Thread(target=run, args=("lane-b",), daemon=True)
                first.start()
                self.assertTrue(lane_a_first.wait(1))
                second.start()
                first.join(3)
                second.join(3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(observed, {
                "lane-a": ["s1", "s2"],
                "lane-b": ["s2", "s1"],
            })
            self.assertTrue(all(items for items, _notes in results.values()))
            self.assertEqual([item.args[0] for item in quota.call_args_list], ["serper", "serper"])

    def test_structured_news_candidates_preserve_date_and_relevance(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-search-provider-")
        try:
            with temporary_env(**required_env(TG_BOT_DATA_DIR=data_dir)):
                reload_config()
                sys.modules.pop("tg_bot.storage", None)
                sys.modules.pop("tg_bot.tools.search", None)
                search = importlib.import_module("tg_bot.tools.search")
                payload = json.dumps({"results": [{
                    "title": "Breaking event",
                    "description": "Verified summary",
                    "url": "https://example.com/news/1",
                    "page_age": "2026-07-12T11:00:00+00:00",
                    "score": 0.91,
                }]})
                with patch("tg_bot.tools.fetch.http_get", return_value=payload):
                    items, diagnostics = search.execute_news_candidates("event")
                self.assertEqual(items[0]["published_at"], "2026-07-12T11:00:00+00:00")
                self.assertEqual(items[0]["relevance"], 0.91)
                self.assertEqual(items[0]["source"], "brave")
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_structured_news_candidates_rotates_serper_keys(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-search-serper-")
        try:
            with temporary_env(**required_env(TG_BOT_DATA_DIR=data_dir, TAVILY_KEY_0=None, SERPER_KEY_0="k1", SERPER_KEY_1="k2")):
                reload_config()
                sys.modules.pop("tg_bot.storage", None)
                sys.modules.pop("tg_bot.tools.search", None)
                search = importlib.import_module("tg_bot.tools.search")
                cfg = importlib.import_module("tg_bot.config")
                search.BRAVE_KEY = ""
                search.TAVILY_KEYS = []

                class FakeResponse:
                    def __enter__(self):
                        return self
                    def __exit__(self, *_args):
                        return False
                    def read(self):
                        return json.dumps({"news": [{"title": "Serper event", "link": "https://example.com/1", "date": "1 hour ago"}]}).encode()

                with patch.object(search, "urlopen", side_effect=[OSError("first key"), FakeResponse()]):
                    items, diagnostics = search.execute_news_candidates("event")
                self.assertEqual(items[0]["source"], "serper")
                self.assertEqual(cfg._serper_idx, 1)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_structured_news_candidates_advances_serper_key_after_success(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-search-serper-success-")
        try:
            with temporary_env(**required_env(TG_BOT_DATA_DIR=data_dir, TAVILY_KEY_0=None, SERPER_KEY_0="k1", SERPER_KEY_1="k2")):
                reload_config()
                sys.modules.pop("tg_bot.storage", None)
                sys.modules.pop("tg_bot.tools.search", None)
                search = importlib.import_module("tg_bot.tools.search")
                search.BRAVE_KEY = ""
                search.TAVILY_KEYS = []

                class FakeResponse:
                    def __enter__(self):
                        return self
                    def __exit__(self, *_args):
                        return False
                    def read(self):
                        return json.dumps({"news": [{"title": "Serper event", "link": "https://example.com/1", "date": "1 hour ago"}]}).encode()

                observed = []
                def fake_urlopen(request, **_kwargs):
                    observed.append(request.get_header("X-api-key"))
                    return FakeResponse()

                with patch.object(search, "urlopen", side_effect=fake_urlopen):
                    search.execute_news_candidates("event-1")
                    search.execute_news_candidates("event-2")
                self.assertEqual(observed, ["k1", "k2"])
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_direct_serper_search_rotates_keys_after_success(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-direct-serper-success-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                TAVILY_KEY_0=None,
                SERPER_KEY_0="k1",
                SERPER_KEY_1="k2",
            )):
                reload_config()
                sys.modules.pop("tg_bot.tools.search", None)
                search = importlib.import_module("tg_bot.tools.search")

                class FakeResponse:
                    def __enter__(self):
                        return self
                    def __exit__(self, *_args):
                        return False
                    def read(self):
                        return json.dumps({
                            "organic": [{
                                "title": "Serper result",
                                "link": "https://example.com/1",
                                "snippet": "summary",
                            }]
                        }).encode()

                observed = []

                def fake_urlopen(request, **_kwargs):
                    observed.append(request.get_header("X-api-key"))
                    return FakeResponse()

                with patch.object(search, "urlopen", side_effect=fake_urlopen):
                    search._execute_serper("query-1", "general")
                    search._execute_serper("query-2", "general")
                self.assertEqual(observed, ["k1", "k2"])
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_direct_serper_search_retries_all_configured_keys(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-direct-serper-retry-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                TAVILY_KEY_0=None,
                SERPER_KEY_0="k1",
                SERPER_KEY_1="k2",
                SERPER_KEY_2="k3",
            )):
                reload_config()
                sys.modules.pop("tg_bot.tools.search", None)
                search = importlib.import_module("tg_bot.tools.search")

                class FakeResponse:
                    def __enter__(self):
                        return self
                    def __exit__(self, *_args):
                        return False
                    def read(self):
                        return json.dumps({
                            "organic": [{
                                "title": "Serper result",
                                "link": "https://example.com/1",
                                "snippet": "summary",
                            }]
                        }).encode()

                observed = []

                def fake_urlopen(request, **_kwargs):
                    observed.append(request.get_header("X-api-key"))
                    if len(observed) < 3:
                        raise OSError("key unavailable")
                    return FakeResponse()

                with patch.object(search, "urlopen", side_effect=fake_urlopen):
                    result = search._execute_serper("query", "general")
                self.assertIn("Serper result", result)
                self.assertEqual(observed, ["k1", "k2", "k3"])
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


class TokenTests(unittest.TestCase):
    def test_existing_token_file_is_tightened_to_private_mode(self):
        temp_dir = tempfile.mkdtemp(prefix="tg-bot-token-")
        try:
            with temporary_env(**required_env(TG_BOT_DATA_DIR=temp_dir, ASK_API_TOKEN=None)):
                cfg = reload_config()
                cfg.ensure_data_dir()
                with open(cfg.ASK_TOKEN_FILE, "w", encoding="utf-8") as f:
                    f.write("existing-token-value")
                os.chmod(cfg.ASK_TOKEN_FILE, 0o644)
                sys.modules.pop("tg_bot.ask_server", None)
                api = importlib.import_module("tg_bot.ask_server")
                self.assertEqual(api._load_or_create_ask_token(), "existing-token-value")
                self.assertEqual(stat.S_IMODE(os.stat(cfg.ASK_TOKEN_FILE).st_mode), 0o600)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class ApiServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="tg-bot-api-")
        env = required_env(
            TG_BOT_DATA_DIR=self.temp_dir,
            ASK_API_TOKEN="test-api-token-123",
            ASK_API_RATE_LIMIT="10",
            ASK_API_RATE_WINDOW_SECONDS="60",
            ASK_API_MAX_BODY_BYTES="65536",
            ASK_API_MAX_QUERY_CHARS="4000",
        )
        self.env_context = temporary_env(**env)
        self.env_context.__enter__()
        sys.modules.pop("tg_bot.ask_server", None)
        sys.modules.pop("tg_bot.config", None)
        self.api = importlib.import_module("tg_bot.ask_server")
        self.api.ASK_API_TOKEN = "test-api-token-123"
        self.api._rate_state.clear()
        self.fake_bot = types.ModuleType("tg_bot.bot")
        self.fake_bot.handle = lambda chat_id, text, http_mode, brief: f"echo:{text}:{brief}"
        self.old_bot = sys.modules.get("tg_bot.bot")
        sys.modules["tg_bot.bot"] = self.fake_bot
        self.server = self.api.HTTPServer(("127.0.0.1", 0), self.api._AskHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.old_bot is None:
            sys.modules.pop("tg_bot.bot", None)
        else:
            sys.modules["tg_bot.bot"] = self.old_bot
        self.env_context.__exit__(None, None, None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def request(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        body = None
        req_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode("utf-8") if isinstance(payload, dict) else payload
            req_headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=req_headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def raw_request(self, method, path, headers):
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        try:
            lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close"]
            lines.extend(f"{key}: {value}" for key, value in headers.items())
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        raw = b"".join(chunks)
        head, body = raw.split(b"\r\n\r\n", 1)
        status = int(head.splitlines()[0].split()[1])
        return status, json.loads(body.decode("utf-8"))

    def auth_headers(self, **extra):
        headers = {"Authorization": "Bearer test-api-token-123"}
        headers.update(extra)
        return headers

    def test_v1_ask_alias_preserves_reply_shape(self):
        for path in ("/ask", "/v1/ask"):
            status, data = self.request("POST", path, {"query": "hello"}, self.auth_headers())
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(data["reply"], "echo:hello:False")
            self.assertEqual(data["api_version"], "v1")

    def test_health_version_and_capabilities_are_public(self):
        for path in ("/health", "/version", "/capabilities"):
            status, data = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
        status, data = self.request("GET", "/capabilities")
        self.assertIn("/v1/ask", data["endpoints"])
        self.assertEqual(data["methods"]["capabilities"], ["GET"])

    def test_readyz_success_does_not_expose_runtime_details(self):
        status, data = self.request("GET", "/readyz")
        self.assertEqual(status, 200)
        self.assertTrue(data["ready"])
        self.assertNotIn("details", data)

    def test_readyz_reports_configuration_failure(self):
        old = self.api.validate_config
        self.api.validate_config = lambda: {"ok": False, "errors": ["bad config"], "warnings": []}
        try:
            status, data = self.request("GET", "/readyz")
        finally:
            self.api.validate_config = old
        self.assertEqual(status, 503)
        self.assertEqual(data["error"], "not_ready")
        self.assertNotIn("details", data)

    def test_readyz_reports_stale_daily_report(self):
        old = self.api.daily_report_health
        self.api.daily_report_health = lambda: {
            "ok": False,
            "status": "stale",
            "errors": ["日报过期"],
        }
        try:
            status, data = self.request("GET", "/readyz")
        finally:
            self.api.daily_report_health = old
        self.assertEqual(status, 503)
        self.assertEqual(data["error"], "not_ready")
        self.assertNotIn("details", data)

    def test_bearer_and_x_api_key_are_accepted(self):
        for headers in (
            {"Authorization": "Bearer test-api-token-123"},
            {"X-API-Key": "test-api-token-123"},
        ):
            status, data = self.request("POST", "/v1/ask", {"query": "auth"}, headers)
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])

    def test_invalid_token_returns_stable_401(self):
        status, data = self.request(
            "POST", "/v1/ask", {"query": "auth"}, {"Authorization": "Bearer wrong"}
        )
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "invalid_token")
        self.assertNotIn("test-api-token-123", json.dumps(data))

    def test_request_body_and_query_limits_return_413(self):
        old_body = self.api.ASK_API_MAX_BODY_BYTES
        old_query = self.api.ASK_API_MAX_QUERY_CHARS
        self.api.ASK_API_MAX_BODY_BYTES = 64
        self.api.ASK_API_MAX_QUERY_CHARS = 4
        try:
            status, data = self.request(
                "POST", "/ask", {"query": "x" * 100}, self.auth_headers()
            )
            self.assertEqual(status, 413)
            self.assertEqual(data["error"], "request_too_large")
            status, data = self.request(
                "POST", "/ask", {"query": "hello"}, self.auth_headers()
            )
            self.assertEqual(status, 413)
            self.assertEqual(data["error"], "query_too_large")
        finally:
            self.api.ASK_API_MAX_BODY_BYTES = old_body
            self.api.ASK_API_MAX_QUERY_CHARS = old_query

    def test_non_string_query_returns_400(self):
        status, data = self.request("POST", "/ask", {"query": 123}, self.auth_headers())
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "invalid_query")

    def test_missing_or_chunked_content_length_is_rejected(self):
        status, data = self.raw_request("POST", "/ask", self.auth_headers())
        self.assertEqual(status, 411)
        self.assertEqual(data["error"], "content_length_required")
        status, data = self.request(
            "POST", "/ask", None,
            self.auth_headers(**{"Transfer-Encoding": "chunked"}),
        )
        self.assertEqual(status, 411)
        self.assertEqual(data["error"], "chunked_not_supported")

    def test_pipeline_error_does_not_leak_exception_details(self):
        self.fake_bot.handle = lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("secret internal detail")
        )
        with patch.object(self.api.log, "error"):
            status, data = self.request(
                "POST", "/ask", {"query": "fail"}, self.auth_headers()
            )
        self.assertEqual(status, 500)
        self.assertEqual(data["error"], "pipeline_failed")
        self.assertNotIn("secret internal detail", json.dumps(data))

    def test_bind_failure_is_reported_to_startup(self):
        old_server = self.api.HTTPServer
        old_error = self.api.ASK_SERVER_ERROR
        self.api.HTTPServer = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("bind failed")
        )
        self.api.ASK_SERVER_READY.clear()
        self.api.ASK_SERVER_ERROR = None
        try:
            with patch.object(self.api.log, "error"):
                self.api._run_ask_server()
            self.assertTrue(self.api.ASK_SERVER_READY.is_set())
            self.assertIn("bind failed", str(self.api.ASK_SERVER_ERROR))
        finally:
            self.api.HTTPServer = old_server
            self.api.ASK_SERVER_ERROR = old_error

    def test_rate_limit_returns_429_after_threshold(self):
        old_limit = self.api.ASK_API_RATE_LIMIT
        self.api.ASK_API_RATE_LIMIT = 1
        self.api._rate_state.clear()
        try:
            status, _ = self.request("POST", "/ask", {"query": "one"}, self.auth_headers())
            self.assertEqual(status, 200)
            status, data = self.request("POST", "/ask", {"query": "two"}, self.auth_headers())
            self.assertEqual(status, 429)
            self.assertEqual(data["error"], "rate_limited")
        finally:
            self.api.ASK_API_RATE_LIMIT = old_limit
            self.api._rate_state.clear()

    def test_request_id_is_echoed_or_generated(self):
        status, data = self.request(
            "POST", "/v1/ask", {"query": "id"},
            self.auth_headers(**{"X-Request-ID": "client-123"}),
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["request_id"], "client-123")
        status, data = self.request("POST", "/v1/ask", {"query": "id"}, self.auth_headers())
        self.assertEqual(status, 200)
        self.assertTrue(data["request_id"])


class DeploymentCheckTests(unittest.TestCase):
    def run_check(self, values):
        env = os.environ.copy()
        for key, value in values.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return subprocess.run(
            [sys.executable, "scripts/tg-bot-check.py"],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_check_script_accepts_complete_environment(self):
        temp_dir = tempfile.mkdtemp(prefix="tg-bot-check-")
        shutil.rmtree(temp_dir)
        try:
            result = self.run_check(required_env(TG_BOT_DATA_DIR=temp_dir))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)
            self.assertTrue(os.path.isdir(temp_dir))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_check_script_reports_missing_required_key(self):
        values = required_env(TG_BOT_DATA_DIR=tempfile.mkdtemp(prefix="tg-bot-check-"))
        values["DEEPSEEK_KEY_0"] = None
        result = self.run_check(values)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEEPSEEK_KEY_0", result.stderr + result.stdout)

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
        with patch("tg_bot.pipeline.gather.fast_chat", return_value=""):
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


class ReplyStructureTests(unittest.TestCase):
    def setUp(self):
        self.response = importlib.import_module("tg_bot.response")

    def test_empty_conclusion_uses_safe_fallback(self):
        envelope = self.response.ReplyEnvelope(conclusion="", evidence=("事实 A",))
        text = self.response.render_reply(envelope)
        self.assertTrue(text.startswith("目前资料不足，无法确认"))
        self.assertIn("关键依据", text)

    def test_normalize_reply_preserves_first_paragraph_as_conclusion(self):
        envelope = self.response.normalize_reply("第一段结论。\n\n第二段事实。")
        self.assertEqual(envelope.conclusion, "第一段结论。")
        self.assertEqual(envelope.evidence, ("第二段事实。",))

    def test_sources_are_hidden_for_answer_but_visible_for_search(self):
        source = {"title": "来源标题", "domain": "example.com", "url": "https://example.com/a"}
        hidden = self.response.render_reply(self.response.ReplyEnvelope("结论", sources=(source,)))
        visible = self.response.render_reply(self.response.ReplyEnvelope("结论", sources=(source,), mode="search"))
        self.assertNotIn("example.com", hidden)
        self.assertIn("example.com", visible)

    def test_search_render_keeps_source_marker_mapping(self):
        source = {"title": "来源标题", "domain": "example.com", "url": "https://example.com/a"}
        envelope = self.response.ReplyEnvelope("结论", evidence=("事实 [来源1]",), sources=(source,), mode="search")
        text = self.response.render_reply(envelope)
        self.assertIn("事实 [来源1]", text)
        self.assertIn("[来源1] 来源标题", text)

    def test_source_heading_is_not_repeated_as_key_evidence(self):
        envelope = self.response.normalize_reply("结论\n\n【来源】\n来源标题")
        self.assertEqual(envelope.evidence, ())

    def test_render_reply_does_not_emit_reasoning_and_respects_limit(self):
        envelope = self.response.ReplyEnvelope("结论", evidence=("x" * 2000,), actions=("下一步",))
        text = self.response.render_reply(envelope, max_chars=240)
        self.assertLessEqual(len(text), 240)
        self.assertNotIn("reasoning", text.lower())

    def test_bot_display_keeps_plain_reply_layout(self):
        module_name = "tg_bot.bot"
        previous = sys.modules.get(module_name)
        try:
            detailed_log_stub = types.ModuleType("tg_bot.detailed_log")
            detailed_log_stub.save_detailed_log = lambda **kwargs: None
            with temporary_env(**required_env()):
                with patch.dict(sys.modules, {"tg_bot.detailed_log": detailed_log_stub}):
                    sys.modules.pop(module_name, None)
                    bot = importlib.import_module(module_name)
                    text = bot._render_display_reply(
                        "结论。\n\n第二段事实。",
                        meta={"source_index": [{"title": "来源", "domain": "example.com"}]},
                        mode="answer",
                    )
            self.assertEqual(text, "结论。\n\n第二段事实。")
            self.assertNotIn("关键依据", text)
            self.assertNotIn("example.com", text)
        finally:
            sys.modules.pop(module_name, None)
            if previous is not None:
                sys.modules[module_name] = previous


class ThinkingDigestTests(unittest.TestCase):
    def test_thinking_digest_hides_raw_reasoning_and_summarizes_execution(self):
        module_names = ("tg_bot.config", "tg_bot.commands", "tg_bot.commands.info")
        previous = {name: sys.modules.get(name) for name in module_names}
        try:
            with temporary_env(**required_env()):
                for module_name in module_names:
                    sys.modules.pop(module_name, None)
                info = importlib.import_module("tg_bot.commands.info")
            digest = info._format_thinking_digest(
                [
                    {
                        "user": "今天有什么新闻？",
                        "role": "gather",
                        "ts": "2026-07-14 13:01",
                        "rounds": [
                            {
                                "round": 0,
                                "tool_calls": ["web_search", "fetch_content"],
                                "reasoning": "This is a long internal chain that must never be shown to the user.",
                            },
                            {"round": 1, "tool_calls": ["web_search"], "reasoning": "another secret chain"},
                        ],
                    },
                    {
                        "user": "今天有什么新闻？",
                        "role": "verifier",
                        "ts": "2026-07-14 13:02",
                        "verdict": "pass",
                        "reasoning": "private verifier reasoning",
                    },
                ],
                tool_log=[{
                    "ts": "13:02",
                    "user": "今天有什么新闻？",
                    "model_tools": [
                        "web_search(今日新闻)",
                        "fetch_content(https://example.com/news)",
                        "web_search(今日热点)",
                    ],
                    "route_info": {
                        "route": "search",
                        "reason": "问题涉及今天的动态信息",
                    },
                    "verify_status": "pass",
                    "evidence_flags": ["source_index", "fetched_pages", "search_tools"],
                }],
            )
            self.assertIn("执行链", digest)
            self.assertIn("路线：搜索回答", digest)
            self.assertIn("原因：问题涉及今天的动态信息", digest)
            self.assertIn("第1轮：web_search、fetch_content", digest)
            self.assertIn("第2轮：web_search", digest)
            self.assertIn("web_search × 2", digest)
            self.assertIn("fetch_content × 1", digest)
            self.assertIn("web_search(今日新闻)", digest)
            self.assertIn("正文已抓取", digest)
            self.assertIn("核查结论：通过", digest)
            self.assertNotIn("internal chain", digest)
            self.assertNotIn("private verifier", digest)
            self.assertLessEqual(len(digest), 2600)
        finally:
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def test_thinking_digest_reports_latest_fast_route_without_old_search_chain(self):
        module_names = ("tg_bot.config", "tg_bot.commands", "tg_bot.commands.info")
        previous = {name: sys.modules.get(name) for name in module_names}
        try:
            with temporary_env(**required_env()):
                for module_name in module_names:
                    sys.modules.pop(module_name, None)
                info = importlib.import_module("tg_bot.commands.info")
            digest = info._format_thinking_digest(
                [],
                tool_log=[{
                    "ts": "13:05",
                    "user": "你是谁",
                    "model_tools": [],
                    "route_info": {"route": "fast", "reason": "助手身份问题"},
                    "verify_status": "skip",
                    "evidence_flags": [],
                }],
            )
            self.assertIn("用户问：你是谁", digest)
            self.assertIn("路线：快速回答", digest)
            self.assertIn("未调用搜索工具", digest)
            self.assertNotIn("今天有什么新闻", digest)
        finally:
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


class ToolLogDisplayTests(unittest.TestCase):
    def test_user_tool_log_keeps_search_chain_without_raw_reasoning(self):
        module_names = ("tg_bot.config", "tg_bot.commands", "tg_bot.commands.info")
        previous = {name: sys.modules.get(name) for name in module_names}
        try:
            with temporary_env(**required_env()):
                for module_name in module_names:
                    sys.modules.pop(module_name, None)
                info = importlib.import_module("tg_bot.commands.info")
            text = info._format_toollog_for_user([{
                "ts": "13:10",
                "user": "查今天新闻",
                "model_tools": ["web_search(今日新闻)", "fetch_content(https://example.com)"],
                "route_info": {"route": "search", "reason": "涉及实时新闻"},
                "verify_status": "pass",
                "evidence_flags": ["source_index", "fetched_pages"],
                "failed_urls": ["https://failed.example"],
                "reasoning_preview": "private raw chain must not be displayed",
                "reply_preview": "这是最终回答摘要",
            }])
            self.assertIn("搜索回答", text)
            self.assertIn("web_search(今日新闻)", text)
            self.assertIn("正文已抓取", text)
            self.assertIn("https://failed.example", text)
            self.assertIn("这是最终回答摘要", text)
            self.assertNotIn("private raw chain", text)
        finally:
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


class TelegramProgressTests(unittest.TestCase):
    @staticmethod
    def _fake_telegram():
        calls = []
        next_message_id = [1]

        def fake_tg(method, data):
            calls.append((method, dict(data)))
            if method == "sendMessage":
                message_id = next_message_id[0]
                next_message_id[0] += 1
                return {"ok": True, "result": {"message_id": message_id}}
            return {"ok": True, "result": True}

        return calls, fake_tg

    def test_progress_creates_and_edits_one_status_message(self):
        from tg_bot.progress import TelegramProgress

        calls, fake_tg = self._fake_telegram()
        progress = TelegramProgress(1, fake_tg, animate=False)
        progress.set_route("search")
        progress.stage("gather", "start")
        progress.stage("gather", "done", "3")
        progress.sending()
        progress.complete()

        self.assertEqual(calls[0][0], "sendMessage")
        self.assertIn("搜索路径", calls[0][1]["text"])
        edits = [data["text"] for method, data in calls if method == "editMessageText"]
        self.assertTrue(any("搜索采集中" in text for text in edits))
        self.assertTrue(any("采集 · 3条素材" in text for text in edits))
        # 完成后面板保留（用户要能回看每一步），结尾是已发送行，绝不删除
        self.assertIn("✓ 已发送", edits[-1])
        deleted = [method for method, _data in calls if method == "deleteMessage"]
        self.assertEqual(deleted, [])

    def test_progress_review_shards_collapse_into_single_line(self):
        from tg_bot.progress import TelegramProgress

        calls, fake_tg = self._fake_telegram()
        progress = TelegramProgress(1, fake_tg, animate=False)
        progress.set_route("search")
        progress.stage("review_shard", "start", "0/3")
        progress.stage("review_shard", "done", "1/3")
        progress.stage("review_shard", "done", "2/3")
        progress.stage("review_shard", "done", "3/3")

        edits = [data["text"] for method, data in calls if method == "editMessageText"]
        self.assertTrue(any("分段核查中…（1/3 已完成）" in text for text in edits))
        final = edits[-1]
        # 全程只占一行：最终只有一条 ✓ 分段核查，且不再有"核查中"
        self.assertEqual(final.count("分段核查"), 1)
        self.assertIn("✓ 🧩 分段核查（3 段）", final)
        self.assertNotIn("核查中", final)
        progress.close()

    def test_progress_tool_items_fold_into_panel_without_extra_messages(self):
        from tg_bot.progress import TelegramProgress

        calls, fake_tg = self._fake_telegram()
        progress = TelegramProgress(1, fake_tg, animate=False)
        progress.set_route("search")
        for index in range(4):
            progress.stage("gather_item", "start", f"🔍 搜索{index}")

        # 条目折叠进主面板：只有第一次 sendMessage，其余全是编辑，绝无删除
        sends = [method for method, _data in calls if method == "sendMessage"]
        deletes = [method for method, _data in calls if method == "deleteMessage"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(deletes, [])
        edits = [data["text"] for method, data in calls if method == "editMessageText"]
        self.assertTrue(any("└ 🔍 搜索3" in text for text in edits))
        progress.close()

    def test_disabled_progress_never_calls_telegram(self):
        from tg_bot.progress import TelegramProgress

        calls, fake_tg = self._fake_telegram()
        progress = TelegramProgress(1, fake_tg, enabled=False, animate=False)
        progress.set_route("fast")
        progress.stage("writer", "start")
        progress.complete()
        self.assertEqual(calls, [])


class SearchProgressCallbackTests(unittest.TestCase):
    def test_send_tool_status_prefers_callback_over_telegram(self):
        with temporary_env(**required_env()):
            from tg_bot.pipeline.gather import _send_tool_status

        events = []

        def fail_call(*_args, **_kwargs):
            self.fail("Telegram should not be called when a status callback is available")

        _send_tool_status(
            None,
            fail_call,
            fail_call,
            "web_search",
            {"query": "今日新闻"},
            used={"web_search": 1},
            quota={"web_search": 10},
            status_cb=lambda *event: events.append(event),
        )

        self.assertEqual(
            events,
            [("gather_item", "start", "🔍 采集搜索（1/10）：今日新闻")],
        )

    def test_search_pipeline_reports_each_visible_stage(self):
        with temporary_env(**required_env()):
            from tg_bot.core.pipeline import run_search_pipeline

        events = []
        source = {
            "id": "R001",
            "url": "https://example.com/news",
            "domain": "example.com",
            "title": "今日新闻",
            "snippet": "已核实摘要",
            "full_content": "已核实正文",
            "tool": "web_search",
            "query": "今日新闻",
        }
        meta = {
            "rounds": [],
            "tool_calls_summary": ["web_search(今日新闻)"],
            "tool_results": [],
            "fetched_pages": [],
            "failed_urls": [],
            "facts_json": {},
        }
        config = PipelineConfig(
            query_fixer=False,
            curator=False,
            critic=False,
            patcher=False,
        )

        with temporary_env(**required_env()):
            with patch("tg_bot.pipeline.gather.gather_ai", return_value=([source], meta)):
                with patch("tg_bot.agents.writer.write", return_value=("整理后的答案", "")):
                    reply, verdict, _ = run_search_pipeline(
                        "今天有什么新闻？",
                        ["今日新闻"],
                        config=config,
                        progress_cb=lambda *event: events.append(event),
                    )

        self.assertEqual(reply, "整理后的答案")
        self.assertEqual(verdict, "skip")
        self.assertEqual(events[0], ("query_fixer", "start", ""))
        self.assertIn(("gather", "start", ""), events)
        self.assertIn(("gather", "done", "1"), events)
        self.assertIn(("curator", "start", ""), events)
        self.assertIn(("writer", "done", "6"), events)


class DegradedAnswerPipelineTests(unittest.TestCase):
    """run_search_pipeline 里 audit_required/sufficient=False 时的降级回答路径
    （core/pipeline.py 约 217 行附近）：审计未通过不应一票否决拒答，优先用
    审计认可的来源子集降级作答；只有采集本身一无所获时才真正拒答。"""

    _CONFIG = PipelineConfig(query_fixer=False, curator=False, critic=False, patcher=False)

    @staticmethod
    def _source_dict(id_, body):
        return {
            "id": id_,
            "url": f"https://example.com/{id_}",
            "domain": "example.com",
            "title": f"标题{id_}",
            "snippet": body[:60],
            "full_content": body,
            "tool": "web_search",
            "query": "苹果财报",
        }

    def test_partial_supported_sources_degrade_instead_of_reject(self):
        """场景a：supported_source_ids 只认可部分来源 → 用认可子集降级作答。"""
        supported_body = "苹果公司发布最新财报，营收同比增长12%，超出市场预期。" * 3
        unsupported_body = "一些未经核实的传闻，缺乏可靠信源支持。" * 3
        raw_sources_list = [
            self._source_dict("R001", supported_body),
            self._source_dict("R002", unsupported_body),
        ]
        meta = {
            "rounds": [], "tool_calls_summary": [], "tool_results": [],
            "fetched_pages": [], "failed_urls": [], "facts_json": {},
            "audit_required": True, "sufficient": False,
            "supported_source_ids": ["R001"],
        }

        with temporary_env(**required_env()):
            from tg_bot.core.pipeline import run_search_pipeline
            with patch("tg_bot.pipeline.gather.gather_ai", return_value=(raw_sources_list, meta)):
                with patch("tg_bot.agents.writer.write",
                           return_value=("苹果公司最新财报显示营收增长。", "")):
                    reply, verify_status, out_meta = run_search_pipeline(
                        "苹果最新财报数据怎么样",
                        ["苹果财报"],
                        config=self._CONFIG,
                    )

        self.assertIn("苹果公司最新财报显示营收增长。", reply)
        self.assertIn("ⓘ 部分信息未交叉核实", reply)  # 单域名来源单薄，A方案下仍显示标注
        self.assertNotEqual(verify_status, "insufficient_evidence")
        self.assertEqual([s["id"] for s in out_meta["source_index"]], ["R001"])

    def test_empty_supported_ids_keeps_full_source_set_and_degrades(self):
        """场景b：supported_source_ids 为空但采集素材（source_index）非空
        → 审计一条都没认可时保留全部采集素材，仍旧降级作答，不拒答。"""
        body_a = "苹果公司发布最新财报，营收同比增长12%。" * 3
        body_b = "分析师对财报数据的补充解读。" * 3
        raw_sources_list = [
            self._source_dict("R001", body_a),
            self._source_dict("R002", body_b),
        ]
        meta = {
            "rounds": [], "tool_calls_summary": [], "tool_results": [],
            "fetched_pages": [], "failed_urls": [], "facts_json": {},
            "audit_required": True, "sufficient": False,
            "supported_source_ids": [],
        }

        with temporary_env(**required_env()):
            from tg_bot.core.pipeline import run_search_pipeline
            with patch("tg_bot.pipeline.gather.gather_ai", return_value=(raw_sources_list, meta)):
                with patch("tg_bot.agents.writer.write",
                           return_value=("综合两条来源给出的财报解读。", "")):
                    reply, verify_status, out_meta = run_search_pipeline(
                        "苹果最新财报数据怎么样",
                        ["苹果财报"],
                        config=self._CONFIG,
                    )

        self.assertIn("综合两条来源给出的财报解读。", reply)
        self.assertIn("ⓘ 部分信息未交叉核实", reply)  # 单域名来源单薄，A方案下仍显示标注
        self.assertNotEqual(verify_status, "insufficient_evidence")
        self.assertEqual(
            sorted(s["id"] for s in out_meta["source_index"]), ["R001", "R002"],
        )

    def test_completely_empty_gather_returns_rejection(self):
        """场景c：gather 返回完全空的 source_index → 采集本身一无所获，真正拒答。"""
        meta = {
            "rounds": [], "tool_calls_summary": [], "tool_results": [],
            "fetched_pages": [], "failed_urls": [], "facts_json": {},
            "audit_required": True, "sufficient": False,
            "supported_source_ids": [],
        }

        with temporary_env(**required_env()):
            from tg_bot.core.pipeline import run_search_pipeline
            with patch("tg_bot.pipeline.gather.gather_ai", return_value=([], meta)):
                with patch("tg_bot.agents.writer.write") as mock_write:
                    reply, verify_status, out_meta = run_search_pipeline(
                        "苹果最新财报数据怎么样",
                        ["苹果财报"],
                        config=self._CONFIG,
                    )

        mock_write.assert_not_called()
        self.assertEqual(reply, "没有搜索到可用于回答的资料。请换个问法或发「再查一下」。")
        self.assertEqual(verify_status, "insufficient_evidence")
        self.assertEqual(out_meta["source_index"], [])


class MemoryContextTests(unittest.TestCase):
    def test_build_memory_context_includes_summary_and_recent_context(self):
        text = build_memory_context(
            "用户偏好中文、简洁回答。",
            [
                {"user": "上次聊了日报", "assistant": "记录了日报分类和去重要求"},
                {"user": "不要重复", "assistant": "已记录为长期偏好"},
            ],
            profile="用户是技术型用户，喜欢直接、可验证的中文答案。",
        )
        self.assertIn("长期记忆摘要", text)
        self.assertIn("用户偏好中文", text)
        self.assertIn("上次聊了日报", text)
        self.assertIn("已记录为长期偏好", text)
        self.assertIn("用户画像", text)
        self.assertIn("技术型用户", text)

    def test_build_memory_context_has_safe_empty_fallback_and_limit(self):
        text = build_memory_context("", [], max_chars=180)
        self.assertEqual(text, "【长期记忆】暂无可用的历史摘要或近期对话记录。")
        long_text = build_memory_context("x" * 500, [], max_chars=180)
        self.assertLessEqual(len(long_text), 180)


class ReplyIntegrationTests(unittest.TestCase):
    @staticmethod
    def _load_bot_module():
        module_name = "tg_bot.bot"
        detailed_log_stub = types.ModuleType("tg_bot.detailed_log")
        detailed_log_stub.save_detailed_log = lambda **kwargs: None
        with temporary_env(**required_env()):
            with patch.dict(sys.modules, {"tg_bot.detailed_log": detailed_log_stub}):
                sys.modules.pop(module_name, None)
                return importlib.import_module(module_name)

    def test_search_runner_passes_progress_callback_and_marks_reply_sent(self):
        bot = self._load_bot_module()
        events = []

        class FakeProgress:
            def stage(self, *event):
                events.append(event)

            def sending(self):
                events.append(("sending",))

            def complete(self):
                events.append(("complete",))

            def fail(self, detail):
                events.append(("fail", detail))

        progress = FakeProgress()

        def fake_runner(query, keywords, *, progress_cb=None):
            self.assertEqual(query, "今日新闻")
            self.assertEqual(keywords, ["热点"])
            progress_cb("gather", "start", "")
            return "回答", "pass", {"source_index": []}

        result = bot._run_search_with_progress(
            progress,
            fake_runner,
            "今日新闻",
            ["热点"],
        )
        delivered = bot._send_reply_with_progress(
            progress,
            lambda chat_id, text: chat_id == 1 and text == "回答",
            1,
            result[0],
        )

        self.assertTrue(delivered)
        self.assertEqual(
            events,
            [("gather", "start", ""), ("sending",), ("complete",)],
        )

    def test_fast_chat_prompt_is_honest_about_not_searching(self):
        bot = self._load_bot_module()
        prompt = bot._build_fast_system_prompt(["你是中文助手。"])
        self.assertIn("你是中文助手。", prompt)
        self.assertIn("没有调用任何搜索工具", prompt)
        self.assertIn("不要出现「根据搜索结果」", prompt)

    def test_writer_prompt_declares_core_contract(self):
        """新写作规则的核心契约：结论先行、[来源N] 标注、信息密度约束、退回处理。"""
        writer = importlib.import_module("tg_bot.agents.writer")
        for phrase in ("结论先行", "[来源N]", "信息密度", "来源详情由系统自动折叠附加", "SOFT", "HARD"):
            self.assertIn(phrase, writer._SYS_WRITER)

    def test_today_report_pack_declares_report_mode(self):
        with temporary_env(**required_env()):
            for module_name in ("tg_bot.evidence", "tg_bot.storage", "tg_bot.config"):
                sys.modules.pop(module_name, None)
            evidence = importlib.import_module("tg_bot.evidence")
            pack = evidence.build_today_report_pack(
                "今天午报有什么", report_text="AI 新闻\n来源：example.com"
            )
        self.assertEqual(pack["reply_mode"], "report")


class ReportResearchTests(unittest.TestCase):
    def setUp(self):
        self.research = importlib.import_module("tg_bot.report_research")
        self.daily = importlib.import_module("tg_bot.daily_report")

    def _source(self, domain, *, path="story", title="Seed title", summary="seed facts", source="seed"):
        return self.daily.NewsCandidate(
            category="global",
            title=title,
            summary=summary,
            url=f"https://{domain}/{path}",
            domain=domain,
            published_at="2026-07-17T04:00:00+00:00",
            relevance=0.9,
            source=source,
        )

    def _event(self, *, sources=(), title="Acme launches Project Nova"):
        return self.daily.ReportEvent(
            event_id="event-1",
            category="global",
            title=title,
            summary="Acme launched Project Nova with 40 percent lower cost.",
            sources=tuple(sources),
        )

    def _bundle(self):
        sources = (
            self.research.EvidenceSource(
                "S1", "brave", "q1", "Source one", "https://one.test/a",
                "one.test", "2026-07-17T05:00:00+00:00", "snippet one",
                "full facts one", "ok",
            ),
            self.research.EvidenceSource(
                "S2", "serper", "q2", "Source two", "https://two.test/b",
                "two.test", "2026-07-17T05:10:00+00:00", "snippet two",
                "full facts two", "ok",
            ),
        )
        return self.research.EvidenceBundle("event-1", "global", "Seed", sources)

    def test_contracts_are_frozen_and_budget_validates_direct_construction(self):
        budget = self.research.ResearchBudget()
        with self.assertRaises(FrozenInstanceError):
            budget.fetches = 4
        for kwargs in (
            {"followup_queries": 0}, {"followup_queries": 4},
            {"fetches": 0}, {"fetches": 6},
            {"min_sources": 0}, {"min_sources": 4},
            {"fetches": 1, "min_sources": 2},
            {"max_chars": 3999}, {"max_chars": 40001},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.research.ResearchBudget(**kwargs)
        source = self.research.EvidenceSource(
            "S1", "seed", "", "Title", "https://one.test/a", "one.test",
            None, "snippet", "full", "ok",
        )
        bundle = self.research.EvidenceBundle("event-1", "global", "Title", (source,))
        with self.assertRaises(FrozenInstanceError):
            source.title = "changed"
        with self.assertRaises(FrozenInstanceError):
            bundle.sources = ()

    def test_generates_two_distinct_queries_and_keeps_source_metadata(self):
        queries = []

        def search_more(query):
            queries.append(query)
            return ([{
                "title": f"Result {len(queries)}",
                "url": f"https://source{len(queries)}.test/report",
                "summary": f"snippet {len(queries)}",
                "source": "brave" if len(queries) == 1 else "serper",
                "published_at": "2026-07-17T05:00:00+00:00",
            }], [{"ignored": True}])

        bundle = self.research.deepen_event(
            self._event(), search_more, lambda url: f"[正文来源：{url}]\n\nfull facts",
            self.research.ResearchBudget(),
        )

        self.assertEqual(len(queries), 2)
        self.assertEqual(len(set(queries)), 2)
        self.assertEqual(queries[0], "Acme launches Project Nova")
        self.assertIn("official", queries[1].lower())
        self.assertEqual([source.source_id for source in bundle.sources], ["S1", "S2"])
        self.assertEqual([source.provider for source in bundle.sources], ["brave", "serper"])
        self.assertEqual([source.query for source in bundle.sources], queries)
        self.assertEqual(bundle.sources[0].snippet, "snippet 1")
        self.assertEqual(bundle.sources[0].published_at, "2026-07-17T05:00:00+00:00")
        self.assertNotIn("正文来源", bundle.sources[0].full_content)

    def test_third_query_is_recent_update_variant(self):
        queries = []
        self.research.deepen_event(
            self._event(),
            lambda query: (queries.append(query) or [], []),
            lambda _url: "unused",
            self.research.ResearchBudget(followup_queries=3, min_sources=1),
        )
        self.assertEqual(len(queries), 3)
        self.assertEqual(len(set(queries)), 3)
        self.assertRegex(queries[2], r"24|更新|update")

    def test_caps_fetch_calls_and_dedupes_urls_and_domains_in_stable_order(self):
        seed = self._source("seed.test", path="a?utm_source=feed&source=newsletter#part")
        fetched = []
        rows = [
            {"title": "same canonical", "url": "https://seed.test/a", "source": "brave"},
            {"title": "same domain", "url": "https://seed.test/b", "source": "brave"},
            {"title": "two", "url": "https://two.test/a?gclid=x", "source": "brave"},
            {"title": "two duplicate", "url": "https://two.test/a", "source": "brave"},
            {"title": "three", "url": "https://three.test/a", "source": "brave"},
            {"title": "four", "url": "https://four.test/a", "source": "brave"},
        ]

        def fetch(url):
            fetched.append(url)
            return "facts"

        bundle = self.research.deepen_event(
            self._event(sources=(seed,)), lambda _query: (rows, []), fetch,
            self.research.ResearchBudget(followup_queries=1, fetches=3),
        )

        self.assertEqual(fetched, [
            "https://seed.test/a?source=newsletter",
            "https://two.test/a",
            "https://three.test/a",
        ])
        self.assertEqual([source.domain for source in bundle.sources], [
            "seed.test", "two.test", "three.test",
        ])

    def test_canonical_url_keeps_identity_parameters_values_and_query_order(self):
        first, _domain = self.research._canonical_http_url(
            "https://one.test/a?source=A&from=feed&ref=home&utm_source=x&keep=1"
        )
        second, _domain = self.research._canonical_http_url(
            "https://one.test/a?source=B&from=feed&ref=home&utm_medium=x&keep=1"
        )
        self.assertEqual(
            first, "https://one.test/a?source=A&from=feed&ref=home&keep=1"
        )
        self.assertEqual(
            second, "https://one.test/a?source=B&from=feed&ref=home&keep=1"
        )
        self.assertNotEqual(first, second)

    def test_rejects_unicode_authority_but_allows_explicit_ascii_punycode(self):
        rows = [
            {"url": "https://faß.de/story?source=A", "title": "unicode host"},
            {"url": "https://xn--fa-hia.de/story?source=B", "title": "punycode host"},
        ]
        fetched = []
        bundle = self.research.deepen_event(
            self._event(), lambda _query: (rows, []),
            lambda url: fetched.append(url) or "facts",
            self.research.ResearchBudget(followup_queries=1, min_sources=1),
        )
        self.assertEqual(fetched, ["https://xn--fa-hia.de/story?source=B"])
        self.assertEqual(bundle.sources[0].domain, "xn--fa-hia.de")

    def test_rejects_unsafe_urls_without_fetching_them(self):
        rows = [
            {"url": "ftp://bad.test/a", "title": "ftp"},
            {"url": "https://user:pass@bad.test/a", "title": "userinfo"},
            {"url": "https://bad.test\\@good.test/a", "title": "slash"},
            {"url": "https:///missing-host", "title": "host"},
            {"url": "https://bad_host.test/a", "title": "invalid host"},
            {"url": "https://one.test/a", "title": "one"},
        ]
        fetched = []
        bundle = self.research.deepen_event(
            self._event(), lambda _query: (rows, []),
            lambda url: fetched.append(url) or "facts",
            self.research.ResearchBudget(followup_queries=1, min_sources=1),
        )
        self.assertEqual(fetched, ["https://one.test/a"])
        self.assertEqual(bundle.sources[0].domain, "one.test")

    def test_fetch_failure_tries_later_independent_domains_and_isolates_errors(self):
        rows = [
            {"url": "https://one.test/a", "title": "one"},
            {"url": "https://two.test/a", "title": "two"},
            {"url": "https://three.test/a", "title": "three"},
            {"url": "https://four.test/a", "title": "four"},
        ]
        fetched = []

        def fetch(url):
            fetched.append(url)
            if "one.test" in url:
                raise TimeoutError("slow")
            if "two.test" in url:
                return "正文抓取失败：upstream error"
            return f"full content from {url}"

        bundle = self.research.deepen_event(
            self._event(), lambda _query: (rows, []), fetch,
            self.research.ResearchBudget(followup_queries=1, fetches=4),
        )
        self.assertEqual(len(fetched), 4)
        self.assertEqual([source.domain for source in bundle.sources], ["three.test", "four.test"])
        self.assertTrue(all(source.fetch_status == "ok" for source in bundle.sources))

    def test_search_exception_and_malformed_result_do_not_block_other_query(self):
        calls = []

        def search_more(query):
            calls.append(query)
            if len(calls) == 1:
                raise TimeoutError("search timeout")
            return ([
                None,
                {"title": "bad", "url": ["not", "a", "url"]},
                {"title": "one", "url": "https://one.test/a"},
                {"title": "two", "url": "https://two.test/a"},
            ], [])

        bundle = self.research.deepen_event(
            self._event(), search_more, lambda url: f"facts {url}", self.research.ResearchBudget()
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual([source.domain for source in bundle.sources], ["one.test", "two.test"])

    def test_fails_closed_below_minimum_usable_independent_sources(self):
        rows = [
            {"url": "https://one.test/a", "title": "one"},
            {"url": "https://two.test/a", "title": "two"},
        ]
        bundle = self.research.deepen_event(
            self._event(), lambda _query: (rows, []),
            lambda url: "facts" if "one.test" in url else "",
            self.research.ResearchBudget(followup_queries=1),
        )
        self.assertIsNone(bundle)

    def test_hard_character_budget_fairly_preserves_every_source(self):
        rows = [
            {"url": "https://one.test/a", "title": "one", "summary": "A" * 600},
            {"url": "https://two.test/a", "title": "two", "summary": "B" * 600},
        ]
        bundle = self.research.deepen_event(
            self._event(), lambda _query: (rows, []), lambda _url: "正文" * 3000,
            self.research.ResearchBudget(followup_queries=1, max_chars=4000),
        )
        combined_length = sum(
            len(source.snippet) + len(source.full_content) for source in bundle.sources
        )
        self.assertLessEqual(combined_length, 4000)
        self.assertTrue(all(source.snippet for source in bundle.sources))
        self.assertTrue(all(len(source.full_content) >= 1000 for source in bundle.sources))
        self.assertLess(abs(
            len(bundle.sources[0].full_content) - len(bundle.sources[1].full_content)
        ), 100)

    def test_summary_payload_contains_complete_evidence_without_opinion_channel(self):
        payload = self.research.summary_payload(self._bundle())
        encoded = json.dumps(payload, ensure_ascii=False)
        for field in (
            "source_id", "provider", "query", "title", "url", "domain",
            "published_at", "snippet", "full_content", "fetch_status",
        ):
            self.assertIn(field, encoded)
        self.assertNotIn("comment", encoded)
        self.assertNotIn("opinion", encoded)

    def test_parse_summary_rejects_unknown_drop_one_domain_and_unknown_fields(self):
        bundle = self._bundle()
        valid = {
            "id": "event-1", "title": "事实标题", "summary": "事实摘要",
            "supporting_source_ids": ["S1", "S2"], "conflicts": [], "drop": False,
        }
        invalid = (
            dict(valid, id="wrong"),
            dict(valid, drop="true"),
            dict(valid, drop=None),
            dict(valid, drop=1),
            dict(valid, supporting_source_ids=["S1", "S9"]),
            dict(valid, supporting_source_ids=["S1", "S1"]),
            dict(valid, comment="主观点评"),
            dict(valid, extra="unknown"),
            dict(valid, conflicts="not-an-array"),
            dict(valid, title="A" * 25),
            dict(valid, title="Entirely English title"),
            dict(valid, summary="Entirely English factual summary"),
            dict(valid, title="川普擬升級行動"),
            dict(valid, summary="據官員透露，尚未做最終決定。"),
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assertIsNone(self.research.parse_summary(bundle, raw))
        same_domain = replace(
            bundle,
            sources=(bundle.sources[0], replace(bundle.sources[1], domain="one.test")),
        )
        self.assertIsNone(self.research.parse_summary(same_domain, valid))

    def test_parse_summary_requires_explicit_conflict_language(self):
        bundle = self._bundle()
        raw = {
            "id": "event-1", "title": "数字存在分歧", "summary": "两方给出不同数字。",
            "supporting_source_ids": ["S1", "S2"], "conflicts": ["成本数字不同"],
            "drop": False,
        }
        self.assertIsNotNone(self.research.parse_summary(bundle, raw))
        raw["summary"] = "项目公布了成本数字。"
        self.assertIsNone(self.research.parse_summary(bundle, raw))

    def test_summarize_bundle_retries_parse_failures_with_total_call_bound(self):
        bundle = self._bundle()
        valid = {
            "id": "event-1", "title": "事实标题", "summary": "事实摘要",
            "supporting_source_ids": ["S1", "S2"], "conflicts": [], "drop": False,
        }
        responses = iter((None, valid))
        calls = []
        summary = self.research.summarize_bundle(
            bundle, lambda payload: calls.append(payload) or next(responses), retries=2,
        )
        self.assertEqual(summary.title, "事实标题")
        self.assertEqual(len(calls), 2)
        with self.assertRaises(ValueError):
            self.research.summarize_bundle(bundle, lambda _payload: valid, retries=0)

    def test_parse_summary_returns_dropped_sentinel(self):
        bundle = self._bundle()
        raw = {
            "id": "event-1", "title": "丢弃", "summary": "丢弃",
            "supporting_source_ids": ["S1", "S2"], "conflicts": [], "drop": True,
        }
        result = self.research.parse_summary(bundle, raw)
        self.assertIsInstance(result, self.research.DroppedSummary)
        self.assertEqual(result.event_id, "event-1")
        self.assertIsNone(self.research.parse_summary(bundle, dict(raw, id="wrong")))
        self.assertIsNone(self.research.parse_summary(bundle, dict(raw, title="")))

    def test_summarize_bundle_does_not_retry_dropped(self):
        bundle = self._bundle()
        dropped = {
            "id": "event-1", "title": "丢弃", "summary": "丢弃",
            "supporting_source_ids": ["S1", "S2"], "conflicts": [], "drop": True,
        }
        calls = []
        result = self.research.summarize_bundle(
            bundle, lambda payload: calls.append(payload) or dropped, retries=3,
        )
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(result, self.research.DroppedSummary)

    def test_verify_summary_is_exact_and_fail_closed(self):
        bundle = self._bundle()
        summary = self.research.parse_summary(bundle, {
            "id": "event-1", "title": "事实标题", "summary": "事实摘要",
            "supporting_source_ids": ["S1", "S2"], "conflicts": [], "drop": False,
        })
        accepted = {
            "verdict": "pass", "unsupported_claims": [],
            "supporting_source_ids": ["S1", "S2"],
        }
        payloads = []
        self.assertTrue(self.research.verify_summary(
            bundle, summary, lambda payload: payloads.append(payload) or accepted, retries=1,
        ))
        encoded = json.dumps(payloads[0], ensure_ascii=False)
        self.assertNotIn("S3", encoded)
        rejected = (
            {"verdict": "fail", "unsupported_claims": [], "supporting_source_ids": ["S1"]},
            {"verdict": "unknown", "unsupported_claims": [], "supporting_source_ids": ["S1"]},
            {"verdict": "PASS", "unsupported_claims": [], "supporting_source_ids": ["S1"]},
            {"verdict": "pass", "unsupported_claims": ["unsupported"], "supporting_source_ids": ["S1"]},
            {"verdict": "pass", "unsupported_claims": [], "supporting_source_ids": ["S9"]},
            {"verdict": "pass", "unsupported_claims": [], "supporting_source_ids": []},
            {"verdict": "pass", "unsupported_claims": [], "supporting_source_ids": ["S1"]},
            {"verdict": "pass", "unsupported_claims": [], "supporting_source_ids": ["S1", "S1"]},
            {"verdict": "pass", "unsupported_claims": "none", "supporting_source_ids": ["S1"]},
            None,
        )
        for response in rejected:
            with self.subTest(response=response):
                self.assertFalse(self.research.verify_summary(
                    bundle, summary, lambda _payload, value=response: value, retries=1,
                ))

    def test_verify_summary_retries_exceptions_but_never_exceeds_budget(self):
        bundle = self._bundle()
        summary = self.research.VerifiedSummary(
            "event-1", "事实标题", "事实摘要", ("S1", "S2")
        )
        calls = []
        def verify(_payload):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("fixture")
            return {"verdict": "pass", "unsupported_claims": [], "supporting_source_ids": ["S1", "S2"]}
        self.assertTrue(self.research.verify_summary(bundle, summary, verify, retries=2))
        self.assertEqual(len(calls), 2)


class SiteKeyTests(unittest.TestCase):
    def setUp(self):
        self.research = importlib.import_module("tg_bot.report_research")

    def test_sina_variants_same(self):
        for domain in ("k.sina.cn", "k.sina.com.cn", "finance.sina.com.cn", "www.sina.cn"):
            with self.subTest(domain=domain):
                self.assertEqual(self.research.site_key(domain), "sina.com.cn")

    def test_21jingji(self):
        self.assertEqual(
            self.research.site_key("m.21jingji.com"),
            self.research.site_key("21jingji.com"),
        )

    def test_distinct(self):
        self.assertNotEqual(
            self.research.site_key("reuters.com"), self.research.site_key("bbc.com"),
        )
        self.assertEqual(self.research.site_key("news.bbc.co.uk"), "bbc.co.uk")

    def test_parse_summary_rejects_same_site(self):
        base = importlib.import_module("tg_bot.report_research")
        bundle = base.EvidenceBundle(
            "event-1", "global", "Seed", (
                base.EvidenceSource(
                    "S1", "brave", "q1", "Source one", "https://k.sina.cn/a",
                    "k.sina.cn", "2026-07-17T05:00:00+00:00", "snippet one",
                    "full facts one", "ok",
                ),
                base.EvidenceSource(
                    "S2", "serper", "q2", "Source two", "https://k.sina.com.cn/b",
                    "k.sina.com.cn", "2026-07-17T05:10:00+00:00", "snippet two",
                    "full facts two", "ok",
                ),
            ),
        )
        raw = {
            "id": "event-1", "title": "事实标题", "summary": "事实摘要",
            "supporting_source_ids": ["S1", "S2"], "conflicts": [], "drop": False,
        }
        self.assertIsNone(self.research.parse_summary(bundle, raw))

    def test_authority_raw_kept(self):
        base = importlib.import_module("tg_bot.report_research")
        bundle = base.EvidenceBundle(
            "event-1", "global", "Seed", (
                base.EvidenceSource(
                    "S1", "brave", "q1", "Source one", "https://reuters.com/a",
                    "reuters.com", "2026-07-17T05:00:00+00:00", "snippet one",
                    "full facts one", "ok",
                ),
            ),
        )
        raw = {
            "id": "event-1", "title": "事实标题", "summary": "事实摘要",
            "supporting_source_ids": ["S1"], "conflicts": [], "drop": False,
        }
        self.assertIsInstance(self.research.parse_summary(bundle, raw), self.research.VerifiedSummary)


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.daily = importlib.import_module("tg_bot.daily_report")
        self.now = self.daily.datetime(2026, 7, 12, 13, 0, tzinfo=self.daily.timezone.utc)

    def _candidate(self, title, *, domain="reuters.com", category="global", hours=2, score=0.8, url=None):
        return self.daily.NewsCandidate(
            category=category,
            title=title,
            summary=f"{title} 的事实摘要。",
            url=url or f"https://{domain}/story/{title.replace(' ', '-').lower()}",
            domain=domain,
            published_at=(self.now - self.daily.timedelta(hours=hours)).isoformat(),
            relevance=score,
            source="fixture",
        )

    def _event(self, title="Fresh event", summary="A factual summary.", *, category="global", sources=None):
        if sources is None:
            sources = (self.daily.NewsCandidate(
                category=category,
                title=title,
                summary=summary,
                url="https://example.test/story",
                domain="example.test",
                published_at=(self.now - self.daily.timedelta(hours=1)).isoformat(),
                relevance=0.9,
                source="fixture",
            ),)
        return self.daily.ReportEvent(
            event_id=f"event-{category}",
            category=category,
            title=title,
            summary=summary,
            sources=tuple(sources),
        )

    def _assert_safe_report_chunks(self, chunks):
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4000)
            self.assertEqual(chunk.count("<b>"), chunk.count("</b>"))
            self.assertEqual(chunk.count("<code>"), chunk.count("</code>"))
            self.assertEqual(chunk.count('<a href="'), chunk.count("</a>"))
            self.assertEqual(
                chunk.count("<blockquote expandable>"),
                chunk.count("</blockquote>"),
            )

    def test_tracking_parameters_do_not_change_fingerprint(self):
        left = self._candidate(
            "New AI model released",
            domain="example.com",
            url="https://example.com/story?id=1&utm_source=x",
        )
        right = self._candidate(
            "New AI model released",
            domain="other.example",
            url="https://example.com/story?id=1&utm_medium=social",
        )
        self.assertEqual(self.daily.event_fingerprint(left), self.daily.event_fingerprint(right))

    def test_similar_titles_from_independent_domains_cluster(self):
        candidates = [
            self._candidate("OpenAI releases new model for developers", domain="reuters.com"),
            self._candidate("OpenAI released a new model for developers", domain="apnews.com"),
        ]
        events = self.daily.cluster_candidates(candidates)
        self.assertEqual(len(events), 1)
        self.assertEqual({item.domain for item in events[0].sources}, {"reuters.com", "apnews.com"})

    def test_cooldown_filters_event_seen_within_fourteen_days(self):
        event = self.daily.cluster_candidates([self._candidate("Old event")])[0]
        history = {"events": {event.event_id: {"last_published": "2026-07-02T13:00:00+00:00"}}}
        selected = self.daily.select_events([event], history, now=self.now, per_category=4, cooldown_days=14)
        self.assertEqual(selected, [])

    def test_events_older_than_one_day_are_not_selected(self):
        event = self.daily.cluster_candidates([
            self._candidate("Stale event", hours=48),
        ])[0]
        selected = self.daily.select_events([event], {"events": {}}, now=self.now, per_category=4)
        self.assertEqual(selected, [])

    def test_history_title_rewrite_still_matches_cooldown(self):
        previous = self._candidate("Government announces new AI safety rule", domain="reuters.com")
        current = self._candidate("Government announces AI safety rule", domain="apnews.com")
        previous_event = self.daily.cluster_candidates([previous])[0]
        current_event = self.daily.cluster_candidates([current])[0]
        self.assertNotEqual(previous_event.event_id, current_event.event_id)
        history = {"events": {previous_event.event_id: {
            "last_published": "2026-07-10T13:00:00+00:00",
            "title": previous_event.title,
            "sources": ["reuters.com"],
        }}}
        selected = self.daily.select_events([current_event], history, now=self.now, per_category=4)
        self.assertEqual(selected, [])

    def test_official_update_after_one_day_can_reappear_as_update(self):
        candidate = self._candidate(
            "Government announces new AI safety rule",
            domain="gov.cn",
            hours=2,
            url="https://gov.cn/notice/ai-safety-v2",
        )
        event = self.daily.cluster_candidates([candidate])[0]
        history = {
            "events": {
                event.event_id: {
                    "last_published": "2026-07-10T13:00:00+00:00",
                    "title": "Government announces AI safety rule",
                    "sources": ["reuters.com"],
                }
            }
        }
        selected = self.daily.select_events([event], history, now=self.now, per_category=4, cooldown_days=14)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].status, "update")

    def test_official_numeric_change_is_an_update(self):
        candidate = self._candidate(
            "Government announces AI safety rule",
            domain="gov.cn",
            hours=2,
            url="https://gov.cn/notice/ai-safety-v3",
        )
        candidate = self.daily.NewsCandidate(
            category=candidate.category,
            title=candidate.title,
            summary="Affected 30 systems instead of 20 systems.",
            url=candidate.url,
            domain=candidate.domain,
            published_at=candidate.published_at,
            relevance=candidate.relevance,
        )
        event = self.daily.cluster_candidates([candidate])[0]
        history = {"events": {event.event_id: {
            "last_published": "2026-07-10T13:00:00+00:00",
            "title": event.title,
            "summary": "Affected 20 systems.",
            "sources": ["gov.cn"],
        }}}
        selected = self.daily.select_events([event], history, now=self.now, per_category=4)
        self.assertEqual(selected[0].status, "update")

    def test_missing_explicit_heat_is_reported_as_multi_source_attention(self):
        event = self.daily.cluster_candidates([
            self._candidate("Breaking global event", domain="reuters.com"),
            self._candidate("Breaking global event", domain="apnews.com"),
        ])[0]
        score, basis = self.daily.score_event(event, self.now)
        self.assertGreater(score, 0)
        self.assertIn("多源关注", basis)

    def test_single_source_without_heat_does_not_claim_multi_source_attention(self):
        event = self.daily.cluster_candidates([self._candidate("One source event")])[0]
        _score, basis = self.daily.score_event(event, self.now)
        self.assertNotIn("多源关注", basis)

    def test_render_daily_report_uses_requested_timezone(self):
        text = self.daily.render_daily_report([self._event()], self.now, timezone_name="UTC")
        self.assertIn("<code>2026-07-12 13:00 · 星期日</code>", text)

    def test_render_daily_report_uses_safe_html_for_headings_and_dynamic_text(self):
        sections = importlib.import_module("tg_bot.report_sections")
        title = "A < B & \"C\" 'D'"
        summary = "x > y & \"quoted\" 'text'"
        source = self.daily.NewsCandidate(
            category="global",
            title=title,
            summary=summary,
            url='https://example.test/story?a=1&b="2"',
            domain='exa<&"mple.test',
            published_at=(self.now - self.daily.timedelta(hours=1)).isoformat(),
            relevance=0.9,
            source="fixture",
        )
        text = self.daily.render_daily_report(
            [self._event(title, summary, sources=(source,))],
            self.now,
            timezone_name="UTC",
            section_specs=[sections.section_spec("global")],
        )
        self.assertIn("<b>🗞 午间日报</b>", text)
        self.assertIn("<code>2026-07-12 13:00 · 星期日</code>", text)
        self.assertIn("<b>🌍 全球要闻</b>", text)
        self.assertIn("1. <b>A &lt; B &amp; &quot;C&quot; &#x27;D&#x27;</b>", text)
        self.assertIn("x &gt; y &amp; &quot;quoted&quot; &#x27;text&#x27;", text)
        self.assertIn('href="https://example.test/story?a=1&amp;b=&quot;2&quot;"', text)
        self.assertIn(">example.test</a>", text)
        self.assertNotIn("exa&lt;&amp;&quot;mple.test", text)
        self.assertNotIn("A < B", text)

    def test_sectioned_report_omits_empty_suppressed_and_repeated_sections(self):
        sections = importlib.import_module("tg_bot.report_sections")
        text = self.daily.render_daily_report(
            [],
            self.now,
            section_specs=[sections.section_spec("global"), sections.section_spec("ai_tech")],
            preserved_sections={"global": " ", "ai_tech": ""},
            repeated_sections={"global"},
            suppressed_sections={"global", "ai_tech"},
        )
        self.assertEqual(text, "")
        for placeholder in ("今日没有", "今日暂无", "跳过重复"):
            self.assertNotIn(placeholder, text)

    def test_sectioned_report_renders_only_non_empty_current_snapshot(self):
        sections = importlib.import_module("tg_bot.report_sections")
        text = self.daily.render_daily_report(
            [],
            self.now,
            section_specs=[sections.section_spec("weather"), sections.section_spec("global")],
            preserved_sections={"weather": "🌤 天气预报\n安阳 < 25°C & clear"},
        )
        self.assertIn("<b>🌤 天气预报</b>", text)
        self.assertEqual(text.count("天气预报"), 1)
        self.assertIn("安阳 &lt; 25°C &amp; clear", text)
        self.assertNotIn("全球要闻", text)
        self.assertNotIn("今日暂无", text)

    def test_hackernews_snapshot_preserves_only_its_trusted_source_fold(self):
        sections = importlib.import_module("tg_bot.report_sections")
        text = self.daily.render_daily_report(
            [],
            self.now,
            section_specs=[sections.section_spec("hackernews")],
            preserved_sections={
                "hackernews": (
                    "🗣️ 圈子在聊\n• 安全标题 <b>不可信</b>\n"
                    "<blockquote expandable>原文链接\nhttps://example.test/a</blockquote>"
                ),
            },
        )
        self.assertIn("<blockquote expandable>原文链接", text)
        self.assertIn("https://example.test/a</blockquote>", text)
        self.assertIn("&lt;b&gt;不可信&lt;/b&gt;", text)

    def test_render_daily_report_accepts_but_ignores_legacy_event_comments(self):
        event = self._event()
        text = self.daily.render_daily_report(
            [event],
            self.now,
            event_comments={event.event_id: "└ legacy opinion"},
        )
        self.assertIn("Fresh event", text)
        self.assertNotIn("legacy opinion", text)
        self.assertNotIn("└", text)

    def test_render_daily_report_limits_each_event_to_three_independent_source_links(self):
        sections = importlib.import_module("tg_bot.report_sections")
        sources = tuple(
            self.daily.NewsCandidate(
                category="global",
                title="Fresh event",
                summary="A factual summary.",
                url=url,
                domain=domain,
                published_at=(self.now - self.daily.timedelta(hours=1)).isoformat(),
                relevance=0.9,
                source="fixture",
            )
            for domain, url in (
                ("one.test", "https://one.test/first"),
                ("one.test", "https://one.test/duplicate"),
                ("two.test", "https://two.test/story"),
                ("three.test", "https://three.test/story"),
                ("four.test", "https://four.test/story"),
            )
        )
        text = self.daily.render_daily_report(
            [self._event(sources=sources)],
            self.now,
            section_specs=[sections.section_spec("global")],
        )
        self.assertEqual(text.count('<a href="'), 3)
        self.assertIn("https://one.test/first", text)
        self.assertNotIn("https://one.test/duplicate", text)
        self.assertIn("https://two.test/story", text)
        self.assertIn("https://three.test/story", text)
        self.assertNotIn("https://four.test/story", text)
        self.assertEqual(text.count("<blockquote expandable>"), 1)

    def test_render_daily_report_derives_source_label_from_url_hostname(self):
        sections = importlib.import_module("tg_bot.report_sections")
        source = self.daily.NewsCandidate(
            category="global",
            title="Mismatched source",
            summary="summary",
            url="https://www.evil.test/story",
            domain="reuters.com",
            published_at=(self.now - self.daily.timedelta(hours=1)).isoformat(),
            relevance=0.9,
            source="fixture",
        )
        text = self.daily.render_daily_report(
            [self._event("Mismatched source", "summary", sources=(source,))],
            self.now,
            section_specs=[sections.section_spec("global")],
        )
        self.assertIn('<a href="https://www.evil.test/story">evil.test</a>', text)
        self.assertNotIn("reuters.com", text)

    def test_render_daily_report_skips_unsafe_or_hostless_source_urls(self):
        sections = importlib.import_module("tg_bot.report_sections")
        sources = tuple(
            self.daily.NewsCandidate(
                category="global",
                title="Unsafe source",
                summary="summary",
                url=url,
                domain="trusted.example",
                published_at=(self.now - self.daily.timedelta(hours=1)).isoformat(),
                relevance=0.9,
                source="fixture",
            )
            for url in (
                "javascript:alert(1)",
                "file:///tmp/report",
                "https:///missing-host",
            )
        )
        text = self.daily.render_daily_report(
            [self._event("Unsafe source", "summary", sources=sources)],
            self.now,
            section_specs=[sections.section_spec("global")],
        )
        self.assertNotIn('<a href="', text)
        self.assertNotIn("trusted.example", text)
        self.assertNotIn("<blockquote expandable>", text)

    def test_render_daily_report_rejects_ambiguous_source_authorities(self):
        sections = importlib.import_module("tg_bot.report_sections")
        unsafe_urls = (
            r"https://evil.test\@trusted.test/story",
            "https://evil.test%5c@trusted.test/story",
            "https://user:pass@trusted.test/story",
            "https://user:pa%3Ass@trusted.test/story",
            "https://user%40example.test@trusted.test/story",
            "https://trusted.test\t/story",
            "https://trusted.test /story",
        )
        for url in unsafe_urls:
            with self.subTest(url=url):
                source = self.daily.NewsCandidate(
                    category="global",
                    title="Ambiguous source",
                    summary="summary",
                    url=url,
                    domain="trusted.test",
                    published_at=(self.now - self.daily.timedelta(hours=1)).isoformat(),
                    relevance=0.9,
                    source="fixture",
                )
                text = self.daily.render_daily_report(
                    [self._event("Ambiguous source", "summary", sources=(source,))],
                    self.now,
                    section_specs=[sections.section_spec("global")],
                )
                self.assertNotIn('<a href="', text)
                self.assertNotIn("trusted.test", text)
                self.assertNotIn("<blockquote expandable>", text)

    def test_render_daily_report_rejects_non_ascii_source_authorities(self):
        sections = importlib.import_module("tg_bot.report_sections")
        unsafe_sources = (
            ("https://faß.de/x", "fass.de"),
            ("https://例子.测试/x", "xn--fsqu00a.xn--0zwm56d"),
        )
        for url, misleading_label in unsafe_sources:
            with self.subTest(url=url):
                source = self.daily.NewsCandidate(
                    category="global",
                    title="Unicode source",
                    summary="summary",
                    url=url,
                    domain="trusted.test",
                    published_at=(self.now - self.daily.timedelta(hours=1)).isoformat(),
                    relevance=0.9,
                    source="fixture",
                )
                text = self.daily.render_daily_report(
                    [self._event("Unicode source", "summary", sources=(source,))],
                    self.now,
                    section_specs=[sections.section_spec("global")],
                )
                self.assertNotIn('<a href="', text)
                self.assertNotIn(misleading_label, text)
                self.assertNotIn("<blockquote expandable>", text)

    def test_render_daily_report_allows_ascii_punycode_authority(self):
        sections = importlib.import_module("tg_bot.report_sections")
        source = self.daily.NewsCandidate(
            category="global",
            title="Punycode source",
            summary="summary",
            url="https://xn--fa-hia.de/x",
            domain="trusted.test",
            published_at=(self.now - self.daily.timedelta(hours=1)).isoformat(),
            relevance=0.9,
            source="fixture",
        )
        text = self.daily.render_daily_report(
            [self._event("Punycode source", "summary", sources=(source,))],
            self.now,
            section_specs=[sections.section_spec("global")],
        )
        self.assertIn(
            '<a href="https://xn--fa-hia.de/x">xn--fa-hia.de</a>',
            text,
        )

    def test_split_report_chunks_uses_section_boundaries_without_splitting_blockquotes(self):
        sections = importlib.import_module("tg_bot.report_sections")
        report = self.daily.render_daily_report(
            [self._event(), self._event("AI event", category="ai_tech")],
            self.now,
            section_specs=[sections.section_spec("global"), sections.section_spec("ai_tech")],
        )
        chunks = self.daily.split_report_chunks(report)
        self.assertEqual(len(chunks), 2)
        self.assertIn("<b>🗞 午间日报</b>", chunks[0])
        self.assertIn("<b>🌍 全球要闻</b>", chunks[0])
        self.assertIn("<b>🤖 AI 速报</b>", chunks[1])
        for chunk in chunks:
            self.assertEqual(chunk.count("<blockquote expandable>"), chunk.count("</blockquote>"))

        separator = "┄" * 14
        nested = (
            f"<blockquote expandable>source\n{separator}\nstill source</blockquote>\n"
            f"{separator}\n<b>Next section</b>"
        )
        nested_chunks = self.daily.split_report_chunks(nested)
        self.assertEqual(len(nested_chunks), 2)
        self.assertIn(f"source\n{separator}\nstill source", nested_chunks[0])

    def test_split_report_chunks_splits_oversized_section_at_event_boundaries(self):
        sections = importlib.import_module("tg_bot.report_sections")
        report = self.daily.render_daily_report(
            [
                self._event("Event one", "A" * 2100),
                self._event("Event two", "B" * 2100),
            ],
            self.now,
            section_specs=[sections.section_spec("global")],
        )
        chunks = self.daily.split_report_chunks(report)
        self.assertGreater(len(chunks), 1)
        self._assert_safe_report_chunks(chunks)
        event_one_chunk = next(index for index, chunk in enumerate(chunks) if "Event one" in chunk)
        event_two_chunk = next(index for index, chunk in enumerate(chunks) if "Event two" in chunk)
        self.assertNotEqual(event_one_chunk, event_two_chunk)
        self.assertEqual(sum(chunk.count("<blockquote expandable>") for chunk in chunks), 2)

    def test_split_report_chunks_safely_splits_single_oversized_event(self):
        sections = importlib.import_module("tg_bot.report_sections")
        title = "T" * 4200
        summary = "事实 & < > " * 1000
        report = self.daily.render_daily_report(
            [self._event(title, summary)],
            self.now,
            section_specs=[sections.section_spec("global")],
        )
        chunks = self.daily.split_report_chunks(report)
        self.assertGreater(len(chunks), 2)
        self._assert_safe_report_chunks(chunks)
        self.assertEqual(sum(chunk.count("T") for chunk in chunks), report.count("T"))
        self.assertEqual(sum(chunk.count("事实") for chunk in chunks), report.count("事实"))
        self.assertEqual(sum(chunk.count("<blockquote expandable>") for chunk in chunks), 1)
        source_chunk = next(chunk for chunk in chunks if "<blockquote expandable>" in chunk)
        self.assertIn('<a href="https://example.test/story">', source_chunk)
        self.assertIn("</a>", source_chunk)

    def test_split_report_chunks_degrades_oversized_source_url_safely(self):
        sections = importlib.import_module("tg_bot.report_sections")
        long_url = "https://example.test/" + "a" * 5000
        source = self.daily.NewsCandidate(
            category="global",
            title="Long URL event",
            summary="summary",
            url=long_url,
            domain="example.test",
            published_at=(self.now - self.daily.timedelta(hours=1)).isoformat(),
            relevance=0.9,
            source="fixture",
        )
        report = self.daily.render_daily_report(
            [self._event("Long URL event", "summary", sources=(source,))],
            self.now,
            section_specs=[sections.section_spec("global")],
        )
        open_links = re.findall(r'<a href="[^"]*">', report)
        self.assertTrue(all(len(tag) + len("</a>") < 4000 for tag in open_links))
        self.assertNotIn(long_url, report)
        self.assertIn("example.test", report)
        chunks = self.daily.split_report_chunks(report)
        self._assert_safe_report_chunks(chunks)

    def test_split_report_chunks_drops_oversized_atomic_open_tag_and_matching_close(self):
        report = (
            '<b>Title</b>\n<blockquote expandable><a href="https://example.test/'
            + "a" * 5000
            + '">safe.example</a></blockquote>'
        )
        result = {}

        def split():
            result["chunks"] = self.daily.split_report_chunks(report)

        worker = threading.Thread(target=split, daemon=True)
        worker.start()
        worker.join(1.0)
        self.assertFalse(worker.is_alive(), "splitter did not terminate")
        chunks = result["chunks"]
        self._assert_safe_report_chunks(chunks)
        combined = "".join(chunks)
        self.assertIn("safe.example", combined)
        self.assertNotIn('<a href="', combined)
        self.assertNotIn("</a>", combined)

    def test_selection_keeps_category_quota_and_domain_diversity(self):
        events = []
        for index in range(4):
            events.extend(self.daily.cluster_candidates([
                self._candidate(f"Global event {index}", domain="reuters.com", category="global"),
                self._candidate(f"Global event {index}", domain="apnews.com", category="global"),
            ]))
        events.extend(self.daily.cluster_candidates([
            self._candidate("China event", domain="gov.cn", category="china"),
        ]))
        selected = self.daily.select_events(events, {"events": {}}, now=self.now, per_category=2)
        self.assertEqual(sum(event.category == "global" for event in selected), 2)
        self.assertLessEqual(sum(event.sources[0].domain == "reuters.com" for event in selected), 2)

    def test_report_section_registry_keeps_legacy_and_steam_sections(self):
        sections = importlib.import_module("tg_bot.report_sections")
        ids = {item.id for item in sections.DEFAULT_REPORT_SECTIONS}
        self.assertTrue({
            "weather", "exchange", "market", "ai_tech", "proxy",
            "hackernews", "github", "steam", "cold_knowledge",
        }.issubset(ids))
        self.assertEqual(sections.section_spec("weather").kind, "snapshot")
        # steam 已改为官方中国区 API 快照板块
        self.assertEqual(sections.section_spec("steam").kind, "snapshot")
        self.assertEqual(sections.section_spec("china").kind, "event")

        def fake_collector(*_args, **_kwargs):
            return "ok"

        sections.register_section_collector("steam", fake_collector)
        self.assertIs(sections.get_section_collector("steam"), fake_collector)

    def test_report_section_registry_declares_ordered_reserve_event_lanes(self):
        sections = importlib.import_module("tg_bot.report_sections")
        reserve = [
            (item.id, item.title, item.emoji, item.query)
            for item in sections.DEFAULT_REPORT_SECTIONS
            if item.kind == "event" and item.reserve
        ]
        self.assertEqual(reserve, [
            ("security_privacy", "安全与隐私", "🛡", "网络安全 隐私 漏洞 今日 新闻"),
            ("open_source", "开源与开发工具", "🧰", "开源 开发者工具 今日 发布 更新"),
            ("science_space", "科学与太空", "🔭", "科学 太空 研究 今日 突破"),
            ("product_updates", "产品与服务更新", "📦", "软件 产品 服务 今日 重大更新"),
        ])

    def test_visible_event_sections_fill_missing_primary_lanes_with_reserves(self):
        sections = importlib.import_module("tg_bot.report_sections")
        events = [
            self._event("China", category="china"),
            self._event("Security", category="security_privacy"),
            self._event("Science", category="science_space"),
        ]
        visible = self.daily.select_visible_event_sections(
            sections.DEFAULT_REPORT_SECTIONS, events, target=3,
        )
        self.assertEqual(visible, ("china", "security_privacy", "science_space"))

    def test_visible_event_sections_never_displace_populated_primary_lanes(self):
        sections = importlib.import_module("tg_bot.report_sections")
        events = [
            self._event("China", category="china"),
            self._event("Global", category="global"),
            self._event("AI", category="ai_tech"),
            self._event("Security", category="security_privacy"),
        ]
        visible = self.daily.select_visible_event_sections(
            sections.DEFAULT_REPORT_SECTIONS, events, target=3,
        )
        self.assertEqual(visible, ("china", "global", "ai_tech"))

    def test_visible_event_sections_rank_reserves_by_their_hottest_event(self):
        sections = importlib.import_module("tg_bot.report_sections")
        events = [
            replace(
                self._event("Security", category="security_privacy"),
                heat_score=1,
            ),
            replace(
                self._event("Science", category="science_space"),
                heat_score=100,
            ),
        ]
        visible = self.daily.select_visible_event_sections(
            sections.DEFAULT_REPORT_SECTIONS, events, target=1,
        )
        self.assertEqual(visible, ("science_space",))

    def test_same_event_is_cooldown_scoped_to_each_section(self):
        first_ai = self._candidate("A new proxy tool release", category="ai_tech")
        first_proxy = self._candidate("A new proxy tool release", category="proxy")
        events = self.daily.cluster_candidates([first_ai, first_proxy])
        selected = self.daily.select_events(
            events, {"events": {}}, now=self.now, per_category=1
        )
        self.assertEqual({event.category for event in selected}, {"ai_tech", "proxy"})

    def test_sectioned_report_omits_repeated_event_and_empty_heading(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_sections", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate = module.NewsCandidate(
            category="china",
            title="China policy event",
            summary="A policy is announced.",
            url="https://news.example.cn/policy/1",
            domain="news.example.cn",
            published_at="2026-07-12T11:00:00+00:00",
            relevance=0.9,
            source="fixture",
        )
        first = module.build_report(
            [candidate], now=self.now, state={"schema_version": 1, "events": {}},
            section_specs=module.DEFAULT_REPORT_SECTIONS,
        )
        updated = module.NewsCandidate(
            category=candidate.category, title=candidate.title, summary=candidate.summary,
            url=candidate.url, domain=candidate.domain,
            published_at=(self.now + module.timedelta(days=1, hours=-2)).isoformat(),
            relevance=candidate.relevance, source=candidate.source,
        )
        self.assertIn("中国要闻", first["report_text"])
        second = module.build_report(
            [updated], now=self.now + module.timedelta(days=1), state=first["state"],
            section_specs=module.DEFAULT_REPORT_SECTIONS,
        )
        self.assertEqual(second["report_text"], "")
        self.assertNotIn("China policy event", second["report_text"])

    def test_repeated_event_does_not_restore_stale_external_section(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_repeated_external", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate = module.NewsCandidate(
            category="china", title="China policy event", summary="A policy is announced.",
            url="https://news.example.cn/policy/1", domain="news.example.cn",
            published_at="2026-07-12T11:00:00+00:00", relevance=0.9, source="fixture",
        )
        first = module.build_report(
            [candidate], now=self.now, state={"schema_version": 1, "events": {}},
            section_specs=module.DEFAULT_REPORT_SECTIONS,
        )
        legacy = "【中国要闻】\n昨日重复内容\n"
        updated = module.NewsCandidate(
            category=candidate.category, title=candidate.title, summary=candidate.summary,
            url=candidate.url, domain=candidate.domain,
            published_at=(self.now + module.timedelta(days=1, hours=-2)).isoformat(),
            relevance=candidate.relevance, source=candidate.source,
        )
        second = module.build_report(
            [updated], now=self.now + module.timedelta(days=1), state=first["state"],
            section_specs=module.DEFAULT_REPORT_SECTIONS, legacy_report_text=legacy,
        )
        self.assertEqual(second["report_text"], "")
        self.assertNotIn("昨日重复内容", second["report_text"])

    def test_stale_event_is_not_described_as_duplicate(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_stale_event", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate = module.NewsCandidate(
            category="china", title="Old China event", summary="old", url="https://news.example.cn/policy/2",
            domain="news.example.cn", published_at="2026-07-10T11:00:00+00:00", relevance=0.9,
            source="fixture",
        )
        result = module.build_report(
            [candidate], now=self.now, state={"schema_version": 1, "events": {}},
            section_specs=module.DEFAULT_REPORT_SECTIONS,
            legacy_report_text="【中国要闻】\n昨日旧活动",
        )
        self.assertEqual(result["report_text"], "")
        self.assertNotIn("跳过重复", result["report_text"])
        self.assertNotIn("昨日旧活动", result["report_text"])

    def test_build_report_does_not_restore_snapshots_from_legacy_report_text(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_external_sections", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate = module.NewsCandidate(
            category="ai_tech",
            title="Fresh AI event",
            summary="A fresh event.",
            url="https://openai.com/news/fresh",
            domain="openai.com",
            published_at="2026-07-12T11:00:00+00:00",
            relevance=0.9,
            source="fixture",
        )
        legacy = "【天气预报】\n安阳 25°C\n\n【代理圈动态】\nsing-box 更新\n"
        result = module.build_report(
            [candidate], now=self.now, state={"schema_version": 1, "events": {}},
            section_specs=module.DEFAULT_REPORT_SECTIONS, legacy_report_text=legacy,
        )
        self.assertNotIn("安阳 25°C", result["report_text"])
        self.assertNotIn("sing-box 更新", result["report_text"])

    def test_legacy_circle_heading_starts_a_new_hackernews_section(self):
        sections = importlib.import_module("tg_bot.report_sections")
        legacy = "🛠 GitHub 热榜\n仓库正文\n\n🔒 代理圈动态\n代理正文\n\n🗣️ 圈子在聊\nHN 正文"
        found = sections.split_external_sections(legacy)
        self.assertEqual(found["proxy"], "🔒 代理圈动态\n代理正文")
        self.assertEqual(found["hackernews"], "🗣️ 圈子在聊\nHN 正文")

    def test_daily_report_query_registry_covers_event_sections(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_queries", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # proxy / hackernews / github 已改为真实数据源的 snapshot 板块，不再走新闻搜索
        # steam / cold_knowledge 已改为真实 API 快照 / 编辑 AI 生成，不再走新闻搜索
        for section_id in ("china", "global", "ai_tech"):
            self.assertTrue(module._CATEGORY_QUERIES[section_id])

    def test_strict_section_history_does_not_cross_match_missing_category(self):
        event = self.daily.cluster_candidates([
            self._candidate("Proxy tool release", category="proxy", domain="example.com"),
        ])[0]
        history = {"events": {"old": {
            "last_published": "2026-07-02T13:00:00+00:00",
            "title": event.title,
            # Missing category is a malformed/legacy record.
        }}}
        selected = self.daily.select_events(
            [event], history, now=self.now, per_category=4, strict_category=True
        )
        self.assertEqual(len(selected), 1)

    def test_snapshot_collector_can_override_legacy_section(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_snapshot_adapter", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.register_section_collector("weather", lambda: "安阳当前 26°C")
        diagnostics = []
        weather_specs = [s for s in module.DEFAULT_REPORT_SECTIONS if s.id == "weather"]
        sections = module.collect_snapshot_sections(
            weather_specs,
            "【天气预报】\n昨日 20°C",
            diagnostics,
        )
        self.assertIn("安阳当前 26°C", sections["weather"])
        self.assertEqual(diagnostics, [])

    def test_snapshot_collection_does_not_resurrect_legacy_proxy_or_hn(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_snapshot_current_only", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.register_section_collector("proxy", lambda: "")
        module.register_section_collector("hackernews", lambda: "")
        snapshot_specs = [
            section for section in module.DEFAULT_REPORT_SECTIONS
            if section.id in {"proxy", "hackernews"}
        ]
        current = module.collect_snapshot_sections(
            snapshot_specs,
            "🔒 代理圈动态\n昨日版本\n\n🗣️ 圈子在聊\n昨日热帖",
            [],
        )
        self.assertEqual(current, {})

    def test_cold_knowledge_collector_is_registered_and_degrades_cleanly(self):
        """cold_knowledge 曾经在 register_builtin_snapshot_collectors 里完全漏注册，
        导致板块每天被 collect_snapshot_sections 的 `if collector is None: continue`
        静默吞掉，连诊断信息都没有。这里确认采集器已注册，且 AI 调用失败时走的是
        「记录诊断 + 跳过整块」的正常降级路径，不是继续被静默吃掉。"""
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_cold_knowledge_registered", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sections = importlib.import_module("tg_bot.report_sections")
        module.register_builtin_snapshot_collectors(True)
        self.assertIsNotNone(sections.get_section_collector("cold_knowledge"))
        with patch.object(module, "generate_cold_knowledge", return_value=None) as fake_generate:
            diagnostics = []
            cold_specs = [s for s in module.DEFAULT_REPORT_SECTIONS if s.id == "cold_knowledge"]
            sections_out = module.collect_snapshot_sections(cold_specs, "", diagnostics)
        fake_generate.assert_called_once()
        self.assertEqual(sections_out, {})
        self.assertIn("cold_knowledge: collector_empty", diagnostics)

    def test_cold_knowledge_collector_uses_generated_content_when_available(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_cold_knowledge_success", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.register_builtin_snapshot_collectors(True)
        with patch.object(module, "generate_cold_knowledge", return_value="鮟鱇鱼靠头顶的发光诱饵吸引猎物。") as fake_generate:
            diagnostics = []
            cold_specs = [s for s in module.DEFAULT_REPORT_SECTIONS if s.id == "cold_knowledge"]
            sections_out = module.collect_snapshot_sections(cold_specs, "", diagnostics)
        fake_generate.assert_called_once()
        topic_arg, avoid_arg = fake_generate.call_args[0]
        self.assertEqual(topic_arg, module.cold_topic_for())
        self.assertIsInstance(avoid_arg, list)
        self.assertIn("鮟鱇鱼靠头顶的发光诱饵吸引猎物。", sections_out["cold_knowledge"])
        self.assertEqual(diagnostics, [])

    def test_generate_cold_knowledge_returns_none_without_ai_access(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_cold_knowledge_ai_failure", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with temporary_env(**required_env()):
            reload_config()
            self.assertIsNone(module.generate_cold_knowledge("深海生物", ["深海生物·发光诱饵"]))

    def test_cold_knowledge_novelty_label_is_ledger_safe(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_cold_knowledge_label", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        storage = importlib.import_module("tg_bot.storage")
        body = "深海鮟鱇鱼利用头顶的发光诱饵吸引猎物，其光来自共生细菌，而非自身产生。"
        label = module.cold_knowledge_novelty_label("深海生物", body)
        self.assertEqual(storage.normalize_novelty_summary(label, limit=40), label)
        self.assertEqual(module.cold_knowledge_novelty_label("深海生物", ""), "深海生物")

    def test_proxy_releases_returns_empty_when_every_release_is_old(self):
        with temporary_env(**required_env()):
            native = importlib.import_module("tg_bot.tools.native")
            old_release = json.dumps({
                "tag_name": "v1.0.0",
                "published_at": "2000-01-01T00:00:00Z",
                "body": "old",
            })
            with patch("tg_bot.tools.fetch.http_get", return_value=old_release):
                self.assertEqual(native.execute_proxy_releases(days=7), "")

    def test_hn_front_page_hides_metrics_but_sorts_by_points(self):
        with temporary_env(**required_env()):
            native = importlib.import_module("tg_bot.tools.native")
            fixture = json.dumps({"hits": [
                {
                    "objectID": "1", "title": "Lower ranked headline",
                    "points": 10, "num_comments": 2,
                    "url": "https://example.test/low",
                },
                {
                    "objectID": "42", "title": "A neutral headline",
                    "points": 857, "num_comments": 512,
                    "url": "https://example.test/story",
                },
            ]})
            with patch("tg_bot.tools.fetch.http_get", return_value=fixture):
                text = native.execute_hn_front_page(limit=2)
        self.assertLess(text.index("A neutral headline"), text.index("Lower ranked headline"))
        for marker in ("热度", "讨论", "👍", "💬", "└"):
            self.assertNotIn(marker, text)

    def test_legacy_event_editor_is_removed(self):
        """事件摘要走 report_research 的证据+核查流程，不再用旧版无约束编辑器。"""
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_no_comments", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in (
            "_EVENTS_EDITOR_PROMPT", "editorialize_events",
            "_SECTIONS_EDITOR_PROMPT", "compose_sections",
        ):
            self.assertFalse(hasattr(module, name), name)
        self.assertNotIn("comment", module._SUMMARY_PROMPT.lower())
        self.assertNotIn("点评", module._SUMMARY_PROMPT)

    def test_snapshot_enrichment_forbids_fabricated_opinions(self):
        """Steam/HN 的补充说明基于真实抓取/搜索资料撰写，明确禁止编造评分、销量等数字。"""
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_snapshot_enrich", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        prompt = module._SNAPSHOT_ENRICH_PROMPT
        self.assertIn("绝不编造评分", prompt)
        self.assertIn("不要推断任何细节", prompt)

    def test_section_cooldown_controls_state_retention(self):
        sections = importlib.import_module("tg_bot.report_sections")
        steam = replace(sections.section_spec("steam"), cooldown_days=30)
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_retention", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.build_report(
            [], now=self.now, state={"schema_version": 1, "events": {
                "old": {"last_published": "2026-06-30T13:00:00+00:00"},
            }}, section_specs=[steam],
        )
        self.assertIn("old", result["state"]["events"])


class DailyReportStorageTests(unittest.TestCase):
    def _load_builder(self, name):
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "scripts", "build-daily-report.py",
        )
        spec = importlib.util.spec_from_file_location(name, script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _reload_storage(self, data_dir):
        with temporary_env(**required_env(TG_BOT_DATA_DIR=data_dir)):
            for module_name in ("tg_bot.storage", "tg_bot.config"):
                sys.modules.pop(module_name, None)
            config = importlib.import_module("tg_bot.config")
            storage = importlib.import_module("tg_bot.storage")
            return config, storage

    def test_state_round_trip_uses_schema_version(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-state-")
        try:
            _config, storage = self._reload_storage(data_dir)
            path = os.path.join(data_dir, "daily_report_state.json")
            state = {"schema_version": 1, "events": {"abc": {"heat_score": 81.2}}}
            storage.save_daily_report_state(state, path)
            self.assertEqual(storage.load_daily_report_state(path), state)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


    def test_corrupt_state_is_backed_up_and_replaced(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-state-")
        try:
            _config, storage = self._reload_storage(data_dir)
            path = os.path.join(data_dir, "daily_report_state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not-json")
            state = storage.load_daily_report_state(path)
            self.assertEqual(state, {"schema_version": 1, "events": {}})
            self.assertTrue(any(name.startswith("daily_report_state.json.corrupt.") for name in os.listdir(data_dir)))
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_build_report_returns_machine_readable_state(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        now = module.datetime(2026, 7, 12, 13, 0, tzinfo=module.timezone.utc)
        candidate = module.NewsCandidate(
            category="global",
            title="Major event today",
            summary="A verified event.",
            url="https://reuters.com/story/major-event",
            domain="reuters.com",
            published_at="2026-07-12T11:00:00+00:00",
            relevance=0.9,
            source="fixture",
        )
        result = module.build_report([candidate], now=now, state={"schema_version": 1, "events": {}})
        self.assertIn("午间日报", result["report_text"])
        self.assertIn("events", result["state"])
        self.assertEqual(len(result["selected"]), 1)

    def test_build_report_selected_and_state_include_only_visible_event_lanes(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_visible_lanes", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        now = module.datetime(2026, 7, 12, 13, 0, tzinfo=module.timezone.utc)
        categories = ("china", "global", "ai_tech", "security_privacy")
        candidates = [
            module.NewsCandidate(
                category=category,
                title=f"{category} event",
                summary="A verified event.",
                url=f"https://{category}.example.test/story",
                domain=f"{category}.example.test",
                published_at="2026-07-12T11:00:00+00:00",
                relevance=0.9,
                source="fixture",
            )
            for category in categories
        ]
        result = module.build_report(
            candidates,
            now=now,
            state={"schema_version": 1, "events": {}},
            section_specs=module.DEFAULT_REPORT_SECTIONS,
            event_section_target=2,
        )
        self.assertEqual(
            tuple(dict.fromkeys(event.category for event in result["selected"])),
            ("china", "global"),
        )
        published_ids = {event.event_id for event in result["selected"]}
        self.assertEqual(set(result["state"]["events"]), published_ids)
        self.assertEqual(
            {event.category for event in result.get("eligible_selected", [])},
            set(categories),
        )
        self.assertNotIn("AI 速报", result["report_text"])
        self.assertNotIn("安全与隐私", result["report_text"])

    def test_build_report_prunes_old_state_entries(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_prune", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        now = module.datetime(2026, 7, 12, 13, 0, tzinfo=module.timezone.utc)
        state = {"schema_version": 1, "events": {"old": {"last_published": "2026-01-01T13:00:00+00:00"}}}
        result = module.build_report([], now=now, state=state)
        self.assertNotIn("old", result["state"]["events"])

    def test_build_report_discards_invalid_state_records(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_invalid_state", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        now = module.datetime(2026, 7, 12, 13, 0, tzinfo=module.timezone.utc)
        result = module.build_report([], now=now, state={"schema_version": 1, "events": {"bad": "corrupt"}})
        self.assertNotIn("bad", result["state"]["events"])

    def test_build_report_discards_state_without_publish_timestamp(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_missing_timestamp", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        now = module.datetime(2026, 7, 12, 13, 0, tzinfo=module.timezone.utc)
        result = module.build_report([], now=now, state={"schema_version": 1, "events": {"missing": {"title": "old"}}})
        self.assertNotIn("missing", result["state"]["events"])

    def test_main_provider_failure_with_no_snapshot_keeps_previous_report(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-cli-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
                DAILY_REPORT_SECTIONS="global",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config"):
                    sys.modules.pop(module_name, None)
                script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
                spec = importlib.util.spec_from_file_location("build_daily_report_failure", script_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                report_path = os.path.join(data_dir, "today_report.txt")
                with open(report_path, "w", encoding="utf-8") as handle:
                    handle.write("previous report")
                with patch.object(module, "collect_candidates", return_value=([], ["global: provider_error:TimeoutError"])):
                    self.assertEqual(module.main([]), 0)
                with open(report_path, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "previous report")
                with open(os.path.join(data_dir, "daily_report_status.json"), encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle)["status"], "no_publishable_content")
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_dry_run_does_not_write_status_when_no_candidates(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-dry-run-")
        try:
            with temporary_env(**required_env(TG_BOT_DATA_DIR=data_dir)):
                for module_name in ("tg_bot.storage", "tg_bot.config"):
                    sys.modules.pop(module_name, None)
                script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
                spec = importlib.util.spec_from_file_location("build_daily_report_dry_run", script_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with patch.object(module, "collect_candidates", return_value=([], ["global: provider_error:TimeoutError"])):
                    self.assertEqual(module.main(["--dry-run"]), 0)
                self.assertFalse(os.path.exists(os.path.join(data_dir, "daily_report_status.json")))
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_main_provider_failure_still_publishes_current_snapshot(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-provider-snapshot-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
                DAILY_REPORT_SECTIONS="weather,global",
                DAILY_REPORT_PUSH="false",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_provider_snapshot")
                snapshots = {"weather": "🌤 天气预报\n安阳晴 25°C"}
                with patch.object(module, "collect_candidates", return_value=([], ["global: provider_error:TimeoutError"])), \
                     patch.object(module, "collect_snapshot_sections", return_value=snapshots):
                    self.assertEqual(module.main([]), 0)
                with open(os.path.join(data_dir, "today_report.txt"), encoding="utf-8") as handle:
                    report = handle.read()
                self.assertIn("安阳晴 25°C", report)
                with open(os.path.join(data_dir, "daily_report_status.json"), encoding="utf-8") as handle:
                    status = json.load(handle)
                self.assertEqual(status["status"], "fresh")
                self.assertIn("global: provider_error:TimeoutError", status["diagnostics"])
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_snapshot_translation_failure_keeps_original_instead_of_dropping_section(self):
        """翻译彻底失败或仍带专有名词（仓库名/版本号）都不该整块删除板块——
        GitHub 热榜曾因为仓库名合法保留英文，被 _needs_translation 误判"没翻译完"
        整块 pop 掉，日报里直接消失。现在必须保留内容（能翻译则用译文，否则用原文）。"""
        module = self._load_builder("build_daily_report_snapshot_translation_closed")
        english_hn = "🗣️ 圈子在聊\n  • This English headline contains enough descriptive words to require translation"
        repo_snapshot = "🛠 开源热榜\n  • a/b v1.2.3"

        # 主翻译 + 逐行兜底都失败：保留原文，不删除 key
        with patch.object(module, "_translate_section_text", return_value=None), \
             patch.object(module, "_translate_section_lines_fallback", return_value=None):
            result = module.localize_snapshot_sections({
                "hackernews": english_hn,
                "github": repo_snapshot,
            })
        self.assertEqual(result["hackernews"], english_hn)
        self.assertEqual(result["github"], repo_snapshot)

        # 译文里仍含专有名词、被 _needs_translation 判定"还需要翻译"：
        # 这是仓库名/版本号合法保留英文的正常情况，必须使用这份译文而不是删除
        translated_but_has_proper_nouns = "🛠 开源热榜\n  • a/b v1.2.3 一个示例仓库的更新说明文字足够长"
        with patch.object(module, "_translate_section_text", return_value=translated_but_has_proper_nouns):
            result = module.localize_snapshot_sections({"github": repo_snapshot})
        self.assertIn("github", result)
        self.assertEqual(result["github"], translated_but_has_proper_nouns)

    def test_empty_event_candidates_still_render_snapshot_sections(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-empty-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config"):
                    sys.modules.pop(module_name, None)
                script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
                spec = importlib.util.spec_from_file_location("build_daily_report_empty", script_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.register_section_collector("weather", lambda: "安阳当前 26°C")
                with patch.object(module, "collect_candidates", return_value=([], ["global: no_candidates"])):
                    self.assertEqual(module.main([]), 0)
                with open(os.path.join(data_dir, "daily_report_status.json"), encoding="utf-8") as handle:
                    status = json.load(handle)
                self.assertEqual(status["status"], "fresh")
                with open(os.path.join(data_dir, "today_report.txt"), encoding="utf-8") as handle:
                    report = handle.read()
                self.assertIn("天气预报", report)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


class FetchSecurityTests(unittest.TestCase):
    def setUp(self):
        self.fetch = importlib.import_module("tg_bot.tools.fetch")

    def test_fetch_url_rejects_private_and_non_http_targets(self):
        self.assertFalse(self.fetch.validate_fetch_url("http://127.0.0.1:8080/secret"))
        self.assertFalse(self.fetch.validate_fetch_url("http://169.254.169.254/latest/meta-data"))
        self.assertFalse(self.fetch.validate_fetch_url("http://100.64.0.1/shared"))
        self.assertFalse(self.fetch.validate_fetch_url("https://example.com:0/article"))
        self.assertFalse(self.fetch.validate_fetch_url("file:///etc/passwd"))
        self.assertFalse(self.fetch.validate_fetch_url("http://localhost/admin"))

    def test_fetch_url_rejects_hostname_resolving_to_private_address(self):
        private_info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))]
        with patch.object(self.fetch.socket, "getaddrinfo", return_value=private_info):
            self.assertFalse(self.fetch.validate_fetch_url("https://example.test/article"))

    def test_fetch_content_does_not_call_provider_for_unsafe_url(self):
        with patch("tg_bot.tools.search._tavily_request") as tavily:
            result = self.fetch.execute_fetch_content("http://127.0.0.1:8000/internal")
        tavily.assert_not_called()
        self.assertIn("URL 不安全", result)

    def test_fetch_content_uses_local_extractor_before_remote_provider(self):
        raw = "<html><body>" + ("安全正文 " * 100) + "</body></html>"
        with patch.object(self.fetch, "validate_fetch_url", return_value=True), \
             patch.object(self.fetch, "_safe_local_fetch", return_value=raw), \
             patch("tg_bot.tools.search._tavily_request") as tavily:
            result = self.fetch.execute_fetch_content("https://example.test/article")
        tavily.assert_not_called()
        self.assertIn("安全正文", result)


class BotUtilsTests(unittest.TestCase):
    def setUp(self):
        with temporary_env(**required_env()):
            for module_name in ("tg_bot.bot_utils", "tg_bot.config"):
                sys.modules.pop(module_name, None)
            self.bot_utils = importlib.import_module("tg_bot.bot_utils")

    def test_send_returns_delivery_status(self):
        with patch.object(self.bot_utils, "http_post", return_value={"ok": True}):
            self.assertTrue(self.bot_utils.send(1, "hello"))
        with patch.object(self.bot_utils, "http_post", return_value={"ok": False}):
            self.assertFalse(self.bot_utils.send(1, "hello"))

    def test_send_report_html_sends_complete_non_empty_sections_without_reprocessing_html(self):
        sent = []
        with patch.object(
            self.bot_utils,
            "_send_chunk",
            side_effect=lambda chat_id, body: sent.append((chat_id, body)) or len(sent) == 1,
        ), patch.object(self.bot_utils.time, "sleep") as sleep:
            delivered = self.bot_utils.send_report_html(
                "1",
                ["<b>A</b>\n正文", "", "   ", "<b>B &amp; C</b>\n正文"],
            )
        self.assertFalse(delivered)
        self.assertEqual(sent, [
            ("1", "<b>A</b>\n正文"),
            ("1", "<b>B &amp; C</b>\n正文"),
        ])
        sleep.assert_called_once_with(0.3)


class DailyReportCollectionTests(unittest.TestCase):
    def _load_builder(self, name):
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "scripts", "build-daily-report.py",
        )
        spec = importlib.util.spec_from_file_location(name, script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_research_selected_events_runs_bounded_evidence_summary_and_verify(self):
        module = self._load_builder("build_daily_report_verified_orchestration")
        daily = importlib.import_module("tg_bot.daily_report")
        research = importlib.import_module("tg_bot.report_research")
        seed = module.NewsCandidate(
            category="global", title="Seed", summary="seed facts",
            url="https://seed.test/a", domain="seed.test",
            published_at="2026-07-17T04:00:00+00:00", relevance=0.9, source="seed",
        )
        event = daily.ReportEvent(
            "e1", "global", "Seed", "seed facts", (seed,), heat_score=91.0,
        )
        queries, fetched, summary_payloads, verify_payloads = [], [], [], []

        def search_more(query):
            queries.append(query)
            index = len(queries)
            return ([{
                "title": f"source {index}", "summary": f"facts {index}",
                "url": f"https://source{index}.test/story", "source": "fixture",
            }], [])

        def fetch(url):
            fetched.append(url)
            return f"full evidence from {url}"

        def summarize(payload):
            summary_payloads.append(payload)
            return {
                "id": "e1", "title": "已核查标题", "summary": "已核查事实摘要",
                "supporting_source_ids": ["S1", "S2"], "conflicts": [], "drop": False,
            }

        def verify(payload):
            verify_payloads.append(payload)
            return {
                "verdict": "pass", "unsupported_claims": [],
                "supporting_source_ids": ["S1", "S2"],
            }

        diagnostics = []
        verified = module.research_selected_events(
            [event], research.ResearchBudget(), search_more, fetch, summarize, verify,
            evidence_top_k=3, summary_retries=2, verify_retries=1,
            diagnostics=diagnostics,
        )
        self.assertEqual(len(queries), 2)
        self.assertLessEqual(len(fetched), 3)
        self.assertEqual(len(summary_payloads), 1)
        self.assertEqual(len(verify_payloads), 1)
        self.assertEqual([item.title for item in verified], ["已核查标题"])
        self.assertEqual([item.summary for item in verified], ["已核查事实摘要"])
        self.assertEqual({source.domain for source in verified[0].sources}, {"seed.test", "source1.test"})
        self.assertIn("global:e1:verified", diagnostics)

    def test_research_selected_events_drops_individual_failures_and_continues(self):
        module = self._load_builder("build_daily_report_verified_failure_isolation")
        daily = importlib.import_module("tg_bot.daily_report")
        research = importlib.import_module("tg_bot.report_research")
        events = [
            daily.ReportEvent(
                event_id, "global", event_id, "facts", (), heat_score=heat,
            )
            for event_id, heat in (("bad", 100.0), ("good", 90.0))
        ]
        diagnostics = []
        def search_more(query):
            event_id = "bad" if query == "bad" else "good"
            return ([
                {"title": "one", "url": f"https://one-{event_id}.test/a"},
                {"title": "two", "url": f"https://two-{event_id}.test/a"},
            ], [])
        def summarize(payload):
            if payload["id"] == "bad":
                return None
            return {
                "id": "good", "title": "已核查事件", "summary": "已核查事实摘要",
                "supporting_source_ids": ["S1", "S2"], "conflicts": [], "drop": False,
            }
        verified = module.research_selected_events(
            events, research.ResearchBudget(followup_queries=1), search_more,
            lambda _url: "full facts", summarize,
            lambda _payload: {"verdict": "pass", "unsupported_claims": [], "supporting_source_ids": ["S1", "S2"]},
            evidence_top_k=2, summary_retries=1, verify_retries=1,
            diagnostics=diagnostics,
        )
        self.assertEqual([event.event_id for event in verified], ["good"])
        self.assertIn("global:bad:summary_failed", diagnostics)

    def test_research_selected_events_skips_dropped_quietly(self):
        module = self._load_builder("build_daily_report_dropped_quietly")
        daily = importlib.import_module("tg_bot.daily_report")
        research = importlib.import_module("tg_bot.report_research")
        events = [
            daily.ReportEvent(
                event_id, "global", event_id, "facts", (), heat_score=heat,
            )
            for event_id, heat in (("junk", 100.0), ("good", 90.0))
        ]
        diagnostics, verify_payloads = [], []
        def search_more(query):
            event_id = "junk" if query == "junk" else "good"
            return ([
                {"title": "one", "url": f"https://one-{event_id}.test/a"},
                {"title": "two", "url": f"https://two-{event_id}.test/a"},
            ], [])
        def summarize(payload):
            if payload["id"] == "junk":
                return {
                    "id": "junk", "title": "丢弃", "summary": "丢弃",
                    "supporting_source_ids": ["S1", "S2"], "conflicts": [], "drop": True,
                }
            return {
                "id": "good", "title": "已核查事件", "summary": "已核查事实摘要",
                "supporting_source_ids": ["S1", "S2"], "conflicts": [], "drop": False,
            }
        def verify(payload):
            verify_payloads.append(payload)
            return {"verdict": "pass", "unsupported_claims": [], "supporting_source_ids": ["S1", "S2"]}
        verified = module.research_selected_events(
            events, research.ResearchBudget(followup_queries=1), search_more,
            lambda _url: "full facts", summarize, verify,
            evidence_top_k=2, summary_retries=1, verify_retries=1,
            diagnostics=diagnostics,
        )
        self.assertEqual([event.event_id for event in verified], ["good"])
        self.assertEqual([payload["summary"]["id"] for payload in verify_payloads], ["good"])
        self.assertIn("global:junk:dropped", diagnostics)
        self.assertFalse(any("summary_failed" in item for item in diagnostics))

    def test_summary_and_verify_calls_use_separate_rotating_key_pools(self):
        module = self._load_builder("build_daily_report_model_pools")
        captured = []
        response = {
            "choices": [{"message": {"content": json.dumps({"ok": True})}}]
        }
        with temporary_env(**required_env(
            DEEPSEEK_KEY_0="write-0", DEEPSEEK_KEY_1="write-1",
            DEEPSEEK_VERIFY_KEY_0="verify-0", DEEPSEEK_VERIFY_KEY_1="verify-1",
        )):
            config = reload_config()
            with patch("tg_bot.tools.fetch.http_post", side_effect=lambda *args, **kwargs: captured.append((args, kwargs)) or response):
                self.assertEqual(module._summary_call({"id": "e1", "sources": []}), {"ok": True})
                self.assertEqual(module._verify_call({"summary": {}, "sources": []}), {"ok": True})
        auth = [entry[1]["headers"]["Authorization"] for entry in captured]
        self.assertEqual(auth, ["Bearer write-1", "Bearer verify-1"])
        summary_prompt = captured[0][0][1]["messages"][0]["content"]
        verify_prompt = captured[1][0][1]["messages"][0]["content"]
        self.assertIn("supporting_source_ids", summary_prompt)
        self.assertIn("unsupported_claims", verify_prompt)
        self.assertNotIn("comment", summary_prompt.lower())
        self.assertNotIn("点评", summary_prompt)

    def test_main_publishes_verified_html_with_daily_sender_and_commits_after_success(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-verified-send-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_PUSH="true",
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
                DAILY_REPORT_SECTIONS="global",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config", "tg_bot.bot_utils"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_verified_send")
                candidate = module.NewsCandidate(
                    category="global", title="Draft headline", summary="draft",
                    url="https://seed.test/a", domain="seed.test",
                    published_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                    relevance=0.9, source="fixture",
                )

                def verified(events, *_args, **_kwargs):
                    return [replace(
                        events[0], title="Verified <headline>", summary="facts & figures",
                        sources=(
                            candidate,
                            replace(candidate, url="https://second.test/b", domain="second.test"),
                        ),
                    )]

                sent_chunks = []
                with patch.object(module, "collect_candidates", return_value=([candidate], [])), \
                     patch.object(module, "research_selected_events", side_effect=verified), \
                     patch("tg_bot.bot_utils.send_report_html", side_effect=lambda chat, chunks: sent_chunks.extend(chunks) or True) as report_send, \
                     patch("tg_bot.bot_utils.send", side_effect=AssertionError("ordinary sender called")) as ordinary_send:
                    self.assertEqual(module.main([]), 0)
                report_send.assert_called_once()
                ordinary_send.assert_not_called()
                html_text = "\n".join(sent_chunks)
                self.assertIn("<b>Verified &lt;headline&gt;</b>", html_text)
                self.assertIn("facts &amp; figures", html_text)
                self.assertIn("seed.test", html_text)
                self.assertIn("second.test", html_text)
                for marker in ("└", "👍", "💬"):
                    self.assertNotIn(marker, html_text)
                with open(os.path.join(data_dir, "daily_report_state.json"), encoding="utf-8") as handle:
                    state = json.load(handle)
                self.assertEqual(len(state["events"]), 1)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_collect_candidates_preserves_requested_order_when_workers_finish_out_of_order(self):
        module = self._load_builder("build_daily_report_parallel_order")
        delays = {"china": 0.04, "global": 0.01, "ai_tech": 0.0}
        queries = {category: module._CATEGORY_QUERIES[category] for category in delays}

        def fake_search(query):
            category = next(item for item, expected in queries.items() if expected == query)
            time.sleep(delays[category])
            return ([{
                "title": f"{category} headline",
                "summary": "summary",
                "url": f"https://example.com/{category}",
                "source": "fixture",
            }], [])

        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            with patch.object(
                search, "execute_news_candidates_all", side_effect=fake_search
            ), patch.object(
                search, "execute_news_candidates",
                side_effect=AssertionError("legacy search must not serve daily reports"),
            ) as legacy_search:
                candidates, diagnostics = module.collect_candidates(("china", "global", "ai_tech"))
        self.assertEqual([item.category for item in candidates], ["china", "global", "ai_tech"])
        self.assertEqual(diagnostics, [])
        legacy_search.assert_not_called()

    def test_collect_candidates_isolates_provider_exception(self):
        module = self._load_builder("build_daily_report_parallel_errors")
        queries = {category: module._CATEGORY_QUERIES[category] for category in ("china", "global")}

        def fake_search(query):
            if query == queries["global"]:
                raise TimeoutError("fixture")
            return ([{
                "title": "stable headline",
                "summary": "summary",
                "url": "https://example.com/stable",
                "source": "fixture",
            }], [])

        with temporary_env(**required_env()):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            with patch.object(search, "execute_news_candidates_all", side_effect=fake_search):
                candidates, diagnostics = module.collect_candidates(("china", "global"))
        self.assertEqual([item.category for item in candidates], ["china"])
        self.assertEqual(diagnostics, ["global: provider_error:TimeoutError"])

    def test_report_content_hash_ignores_only_html_header_timestamp(self):
        module = self._load_builder("build_daily_report_hash")
        candidate = module.NewsCandidate(
            category="global",
            title="Stable headline",
            summary="Stable summary.",
            url="https://example.com/stable",
            domain="example.com",
            published_at="2026-07-12T12:00:00+00:00",
            relevance=0.9,
            source="fixture",
        )
        specs = module.resolve_sections(("global",))
        first = module.build_report(
            [candidate],
            now=module.datetime(2026, 7, 12, 13, 0, tzinfo=module.timezone.utc),
            state={"schema_version": 1, "events": {}},
            section_specs=specs,
        )
        second = module.build_report(
            [candidate],
            now=module.datetime(2026, 7, 12, 13, 5, tzinfo=module.timezone.utc),
            state={"schema_version": 1, "events": {}},
            section_specs=specs,
        )
        first_lines = first["report_text"].splitlines()
        second_lines = second["report_text"].splitlines()
        self.assertNotEqual(first_lines[1], second_lines[1])
        self.assertEqual(first_lines[:1] + first_lines[2:], second_lines[:1] + second_lines[2:])
        self.assertEqual(module._report_content_hash(first), module._report_content_hash(second))

        body_code_first = dict(first, report_text=first["report_text"] + "\n<code>body-a</code>")
        body_code_second = dict(first, report_text=first["report_text"] + "\n<code>body-b</code>")
        self.assertNotEqual(
            module._report_content_hash(body_code_first),
            module._report_content_hash(body_code_second),
        )

    def test_report_content_hash_changes_with_event_title_or_summary(self):
        module = self._load_builder("build_daily_report_hash_event_content")
        now = module.datetime(2026, 7, 12, 13, tzinfo=module.timezone.utc)
        specs = module.resolve_sections(("global",))

        def report(title, summary):
            candidate = module.NewsCandidate(
                category="global",
                title=title,
                summary=summary,
                url="https://example.com/stable",
                domain="example.com",
                published_at="2026-07-12T12:00:00+00:00",
                relevance=0.9,
                source="fixture",
            )
            return module.build_report(
                [candidate],
                now=now,
                state={"schema_version": 1, "events": {}},
                section_specs=specs,
            )

        baseline = report("Stable headline", "Stable summary.")
        title_changed = report("Changed headline", "Stable summary.")
        summary_changed = report("Stable headline", "Changed summary.")
        self.assertNotEqual(
            module._report_content_hash(baseline),
            module._report_content_hash(title_changed),
        )
        self.assertNotEqual(
            module._report_content_hash(baseline),
            module._report_content_hash(summary_changed),
        )

    def test_push_is_idempotent_for_unchanged_nonempty_report(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-push-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_PUSH="true",
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
                DAILY_REPORT_SECTIONS="global",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config", "tg_bot.bot_utils"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_push")
                candidate = module.NewsCandidate(
                    category="global",
                    title="Stable headline",
                    summary="summary",
                    url="https://example.com/stable",
                    domain="example.com",
                    published_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                    relevance=0.9,
                    source="fixture",
                )
                empty_state = {"schema_version": 1, "events": {}}
                with patch.object(module, "collect_candidates", return_value=([candidate], [])), \
                     patch.object(module, "research_selected_events", side_effect=lambda events, *_a, **_k: list(events)), \
                     patch("tg_bot.storage.load_daily_report_state", return_value=empty_state), \
                     patch("tg_bot.bot_utils.send_report_html", return_value=True) as send:
                    self.assertEqual(module.main([]), 0)
                    self.assertEqual(module.main([]), 0)
                self.assertEqual(send.call_count, 1)
                with open(os.path.join(data_dir, "daily_report_status.json"), encoding="utf-8") as handle:
                    status = json.load(handle)
                self.assertEqual(status["push"]["status"], "skipped_unchanged")
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_push_failure_retries_same_event_before_committing_cooldown_state(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-push-retry-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_PUSH="true",
                DAILY_REPORT_SECTIONS="global",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config", "tg_bot.bot_utils"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_push_retry")
                candidate = module.NewsCandidate(
                    category="global",
                    title="Stable headline",
                    summary="summary",
                    url="https://example.com/stable",
                    domain="example.com",
                    published_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                    relevance=0.9,
                    source="fixture",
                )
                with patch.object(module, "collect_candidates", return_value=([candidate], [])), \
                     patch.object(module, "research_selected_events", side_effect=lambda events, *_a, **_k: list(events)), \
                     patch("tg_bot.bot_utils.send_report_html", side_effect=[False, True]) as send:
                    self.assertEqual(module.main([]), 1)
                    self.assertEqual(module.main([]), 0)
                    self.assertEqual(module.main([]), 0)
                self.assertEqual(send.call_count, 2)
                with open(os.path.join(data_dir, "daily_report_status.json"), encoding="utf-8") as handle:
                    status = json.load(handle)
                self.assertEqual(status["status"], "fresh")
                self.assertIn(status["push"]["status"], {"sent", "skipped_unchanged"})
                self.assertEqual(status["event_count"], 1)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_main_applies_event_section_target_to_report_json_and_cooldown_state(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-visible-lanes-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
                DAILY_REPORT_PUSH="false",
                DAILY_REPORT_SECTIONS="china,security_privacy",
                DAILY_REPORT_EVENT_SECTION_TARGET="1",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config", "tg_bot.bot_utils"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_main_visible_lanes")
                candidates = [
                    module.NewsCandidate(
                        category=category,
                        title=f"{category} headline",
                        summary="summary",
                        url=f"https://{category}.example.test/story",
                        domain=f"{category}.example.test",
                        published_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                        relevance=0.9,
                        source="fixture",
                    )
                    for category in ("china", "security_privacy")
                ]
                with patch.object(module, "collect_candidates", return_value=(candidates, [])), \
                     patch.object(module, "research_selected_events", side_effect=lambda events, *_a, **_k: list(events)):
                    self.assertEqual(module.main([]), 0)
                with open(os.path.join(data_dir, "daily_report.json"), encoding="utf-8") as handle:
                    report_json = json.load(handle)
                self.assertEqual(
                    [event["category"] for event in report_json["events"]],
                    ["china"],
                )
                self.assertFalse(os.path.exists(os.path.join(data_dir, "daily_report_state.json")))
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_main_fills_dropped_primary_lane_from_eligible_reserve(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-reserve-after-edit-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
                DAILY_REPORT_PUSH="false",
                DAILY_REPORT_SECTIONS="china,security_privacy",
                DAILY_REPORT_EVENT_SECTION_TARGET="1",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config", "tg_bot.bot_utils"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_reserve_after_edit")
                candidates = [
                    module.NewsCandidate(
                        category=category,
                        title=f"{category} headline",
                        summary="summary",
                        url=f"https://{category}.example.test/story",
                        domain=f"{category}.example.test",
                        published_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                        relevance=0.9,
                        source="fixture",
                    )
                    for category in ("china", "security_privacy")
                ]

                def verify_only_reserve(selected, *_args, **_kwargs):
                    return [event for event in selected if event.category == "security_privacy"]

                with patch.object(module, "collect_candidates", return_value=(candidates, [])), \
                     patch.object(module, "research_selected_events", side_effect=verify_only_reserve):
                    self.assertEqual(module.main([]), 0)
                with open(os.path.join(data_dir, "daily_report.json"), encoding="utf-8") as handle:
                    report_json = json.load(handle)
                with open(os.path.join(data_dir, "today_report.txt"), encoding="utf-8") as handle:
                    report_text = handle.read()
                self.assertEqual(
                    [event["category"] for event in report_json["events"]],
                    ["security_privacy"],
                )
                self.assertNotIn("eligible_selected", report_json)
                self.assertFalse(os.path.exists(os.path.join(data_dir, "daily_report_state.json")))
                self.assertIn("安全与隐私", report_text)
                self.assertNotIn("中国要闻", report_text)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_research_pipeline_failure_does_not_publish_unverified_draft(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-editorial-transaction-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
                DAILY_REPORT_PUSH="false",
                DAILY_REPORT_SECTIONS="global",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config", "tg_bot.bot_utils"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_editorial_transaction")
                candidate = module.NewsCandidate(
                    category="global",
                    title="Original headline",
                    summary="summary",
                    url="https://example.test/transaction",
                    domain="example.test",
                    published_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                    relevance=0.9,
                    source="fixture",
                )

                with patch.object(module, "collect_candidates", return_value=([candidate], [])), \
                     patch.object(module, "research_selected_events", side_effect=RuntimeError("fixture")):
                    self.assertEqual(module.main([]), 0)
                self.assertFalse(os.path.exists(os.path.join(data_dir, "today_report.txt")))
                self.assertFalse(os.path.exists(os.path.join(data_dir, "daily_report.json")))
                self.assertFalse(os.path.exists(os.path.join(data_dir, "daily_report_state.json")))
                with open(os.path.join(data_dir, "daily_report_status.json"), encoding="utf-8") as handle:
                    status = json.load(handle)
                self.assertEqual(status["status"], "no_publishable_content")
                self.assertIn("research_pipeline_error:RuntimeError", status["diagnostics"])
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_main_empty_report_only_writes_no_publishable_content_status(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-no-content-")
        try:
            report_path = os.path.join(data_dir, "today_report.txt")
            json_path = os.path.join(data_dir, "daily_report.json")
            state_path = os.path.join(data_dir, "daily_report_state.json")
            previous_json = {"schema_version": 1, "marker": "keep-json"}
            previous_state = {
                "schema_version": 1,
                "events": {
                    "previous": {
                        "last_published": datetime.now(timezone.utc).isoformat(),
                        "category": "global",
                        "title": "Previous event",
                    },
                },
            }
            with open(report_path, "w", encoding="utf-8") as handle:
                handle.write("previous report")
            with open(json_path, "w", encoding="utf-8") as handle:
                json.dump(previous_json, handle)
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump(previous_state, handle)

            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
                DAILY_REPORT_PUSH="true",
                DAILY_REPORT_SECTIONS="global",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config", "tg_bot.bot_utils"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_no_content")
                with patch.object(module, "collect_candidates", return_value=([], ["global: no_candidates"])), \
                     patch("tg_bot.bot_utils.send_report_html", return_value=True) as send:
                    self.assertEqual(module.main([]), 0)
                send.assert_not_called()

            with open(report_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "previous report")
            with open(json_path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), previous_json)
            with open(state_path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), previous_state)
            with open(os.path.join(data_dir, "daily_report_status.json"), encoding="utf-8") as handle:
                status = json.load(handle)
            self.assertEqual(status["status"], "no_publishable_content")
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_main_empty_report_does_not_overwrite_concurrent_status(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-empty-race-")
        try:
            concurrent_status = {
                "schema_version": 1,
                "status": "fresh",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "content_sha256": "concurrent-success",
                "event_count": 2,
                "push": {
                    "enabled": False,
                    "status": "disabled",
                    "marker": "keep-concurrent-metadata",
                },
            }
            status_path = os.path.join(data_dir, "daily_report_status.json")
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
                DAILY_REPORT_PUSH="false",
                DAILY_REPORT_SECTIONS="global",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config", "tg_bot.bot_utils"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_empty_race")

                def publish_concurrently(_events, *_args, **_kwargs):
                    with open(status_path, "w", encoding="utf-8") as handle:
                        json.dump(concurrent_status, handle)
                    return []

                with patch.object(module, "collect_candidates", return_value=([], [])), \
                     patch.object(module, "research_selected_events", side_effect=publish_concurrently):
                    self.assertEqual(module.main([]), 0)
                with open(status_path, encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle), concurrent_status)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_same_day_sent_status_survives_empty_cooldown_rerun(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-empty-after-sent-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
                DAILY_REPORT_PUSH="true",
                DAILY_REPORT_SECTIONS="global",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config", "tg_bot.bot_utils"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_empty_after_sent")
                status_path = os.path.join(data_dir, "daily_report_status.json")
                candidate = module.NewsCandidate(
                    category="global",
                    title="Stable headline",
                    summary="summary",
                    url="https://example.com/stable",
                    domain="example.com",
                    published_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                    relevance=0.9,
                    source="fixture",
                )
                with patch.object(module, "collect_candidates", return_value=([candidate], [])), \
                     patch.object(module, "research_selected_events", side_effect=lambda events, *_a, **_k: list(events)), \
                     patch("tg_bot.bot_utils.send_report_html", return_value=True) as send:
                    self.assertEqual(module.main([]), 0)
                    with open(status_path, encoding="utf-8") as handle:
                        sent_status = json.load(handle)
                    self.assertEqual(module.main([]), 0)
                self.assertEqual(send.call_count, 1)
                with open(status_path, encoding="utf-8") as handle:
                    final_status = json.load(handle)
                self.assertEqual(final_status, sent_status)
                self.assertEqual(final_status["push"]["status"], "sent")
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_verified_rerender_uses_no_comment_channel(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-editorial-compat-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
                DAILY_REPORT_PUSH="false",
                DAILY_REPORT_SECTIONS="global",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config", "tg_bot.bot_utils"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_editorial_compat")
                candidate = module.NewsCandidate(
                    category="global",
                    title="Original headline",
                    summary="summary",
                    url="https://example.com/editorial-compat",
                    domain="example.com",
                    published_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                    relevance=0.9,
                    source="fixture",
                )

                def fake_research(selected, *_args, **_kwargs):
                    return [replace(selected[0], title="Verified headline")]

                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch.object(module, "collect_candidates", return_value=([candidate], [])), \
                     patch.object(module, "research_selected_events", side_effect=fake_research), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(module.main(["--dry-run"]), 0)
                self.assertIn("Verified headline", stdout.getvalue())
                self.assertNotIn("└", stdout.getvalue())
                self.assertNotIn("TypeError", stderr.getvalue())
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


class DeploymentDocsTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

    def test_daily_report_deployment_examples_document_timer_and_state(self):
        service_path = os.path.join(self.root, "deploy", "tg-bot-daily-report.service.example")
        timer_path = os.path.join(self.root, "deploy", "tg-bot-daily-report.timer.example")
        with open(service_path, encoding="utf-8") as handle:
            service = handle.read()
        with open(timer_path, encoding="utf-8") as handle:
            timer = handle.read()
        self.assertIn("build-daily-report.py", service)
        self.assertIn("DAILY_REPORT_STATE_FILE", service)
        self.assertIn("DAILY_REPORT_PUSH", service)
        self.assertIn("Environment=PYTHONPATH=/opt/tg-bot-search-assistant", service)
        self.assertIn("OnCalendar=*-*-* 13:00:00 Asia/Shanghai", timer)
        self.assertIn("Persistent=true", timer)

    def test_env_and_readmes_document_report_controls(self):
        for relative in (".env.example", "README.md", os.path.join("tg_bot", "README.md")):
            with open(os.path.join(self.root, relative), encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("DAILY_REPORT_COOLDOWN_DAYS", text)
            self.assertIn("DAILY_REPORT_PUSH", text)
            self.assertIn("DAILY_REPORT_MAX_WORKERS", text)
            self.assertIn("daily_report_state.json", text)
            self.assertIn("daily_report_status.json", text)


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
    def test_direct_scheme_source_exempt_from_short_body_filter(self):
        """direct:// 来源（天气/汇率等本地直连工具）正文可以很短，
        不适用「少于 200 字符即空页」的网页启发式过滤；普通 http 短正文仍应被过滤。"""
        direct_source = Source(
            id="D001",
            url="direct://check_weather",
            domain="direct://check_weather",
            title="天气",
            snippet="",
            full_content=("北京今天晴，气温18到27度，东南风3级，空气质量优。" * 5)[:100],
            tool="check_weather",
        )
        short_source = Source(
            id="R001",
            url="https://example.com/short",
            domain="example.com",
            title="无关网页",
            snippet=("这是一段过短的普通网页正文，" * 5)[:50],
            tool="web_search",
        )
        # 先确认构造的正文长度符合用例设计（direct 100 字符，普通 http 50 字符）。
        self.assertEqual(len(direct_source.full_content), 100)
        self.assertEqual(len(short_source.snippet), 50)

        req = curate(
            [direct_source, short_source],
            user_query="今天北京天气怎么样",
            keywords=["天气"],
        )
        kept_ids = {s.id for s in req.sources}
        self.assertIn("D001", kept_ids)
        self.assertNotIn("R001", kept_ids)

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


class GatherParallelToolCallTests(unittest.TestCase):
    """gather_ai 同一轮模型并列发起多个工具调用时（pipeline/gather.py ~612-718 行），
    并发执行后必须按原始 tool_calls 顺序合并回 msgs/meta，且不产生重复来源。"""

    def test_same_round_parallel_tool_calls_merge_in_original_order(self):
        tool_calls = [
            {"id": "call_1", "type": "function", "function": {
                "name": "web_search", "arguments": json.dumps({"query": "苹果发布会"})}},
            {"id": "call_2", "type": "function", "function": {
                "name": "web_search", "arguments": json.dumps({"query": "苹果股价"})}},
            {"id": "call_3", "type": "function", "function": {
                "name": "web_search", "arguments": json.dumps({"query": "苹果新品"})}},
        ]
        round1_response = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "",
                    "tool_calls": tool_calls,
                },
            }]
        }
        round2_response = {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps({
                        "sufficient": True,
                        "reason": "三次搜索已覆盖问题",
                        "reason_code": "coverage_complete",
                        "suggested_length": "short",
                    }),
                    "reasoning_content": "",
                },
            }]
        }
        responses = [round1_response, round2_response]
        captured_payloads = []

        def fake_http_post(url, payload, headers=None, timeout=None):
            captured_payloads.append(payload)
            return responses[len(captured_payloads) - 1]

        def fake_execute_gather_tool(fn, args, ctx):
            # 每个 query 返回可区分的结果，并把新条目写回本次调用专属的
            # local ctx（gather_ai 并发执行时每个工具调用都有独立的 ctx）。
            query = args.get("query", "")
            entry = {
                "id": f"S_{query}",
                "url": f"https://example.com/{query}",
                "domain": "example.com",
                "title": f"title-{query}",
                "snippet": f"snippet-{query}",
                "full_content": "",
                "tool": fn,
                "query": query,
            }
            ctx.source_index.append(entry)
            ctx.url_to_entry[entry["url"]] = entry
            return f"result-for-{query}"

        with temporary_env(**required_env()):
            from tg_bot.pipeline.gather import gather_ai
            with patch("tg_bot.pipeline.gather.http_post", side_effect=fake_http_post), \
                 patch("tg_bot.pipeline.gather.execute_gather_tool",
                       side_effect=fake_execute_gather_tool):
                source_index, meta = gather_ai(
                    "苹果最新动态", ["苹果"],
                    chat_id=None,
                    evidence_audit_enabled=False,
                )

        # 只应发生两轮 API 调用：一轮并列工具调用 + 一轮采集完成 JSON。
        self.assertEqual(len(captured_payloads), 2)

        # ① 第二轮请求里 3 条 tool 回复的 tool_call_id 顺序需与原始 tool_calls 顺序一致。
        round2_msgs = captured_payloads[1]["messages"]
        tool_reply_ids = [m["tool_call_id"] for m in round2_msgs if m.get("role") == "tool"]
        self.assertEqual(tool_reply_ids, ["call_1", "call_2", "call_3"])

        # ② meta["tool_calls_summary"] 恰好 3 条且顺序正确。
        self.assertEqual(
            meta["tool_calls_summary"],
            ["web_search(苹果发布会)", "web_search(苹果股价)", "web_search(苹果新品)"],
        )

        # ③ source_index 无重复 id。
        self.assertEqual(len(source_index), 3)
        ids = [s["id"] for s in source_index]
        self.assertEqual(len(ids), len(set(ids)))

        self.assertTrue(meta["sufficient"])
        self.assertEqual(meta["gather_reason_code"], "coverage_complete")


class GatherFallbackTests(unittest.TestCase):
    def test_parse_gather_completion(self):
        parsed = parse_gather_completion('说明 {"sufficient": false, "reason": "素材不足", "suggested_length": "short"}')
        self.assertFalse(parsed["sufficient"])
        self.assertEqual(parsed["reason"], "素材不足")
        self.assertEqual(parsed["suggested_length"], "short")

    def test_finalize_round_limit(self):
        sources, meta = finalize_round_limit([{"id": "R001"}], {})
        # 采集轮次耗尽属于"未充分"完成，fail-closed 语义下 sufficient 为 False
        self.assertFalse(meta["sufficient"])
        self.assertEqual(meta["gather_reason_code"], "quota_exhausted")
        self.assertEqual(meta["source_index"], sources)


if __name__ == "__main__":
    unittest.main()
