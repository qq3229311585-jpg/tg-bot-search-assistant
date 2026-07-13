#!/usr/bin/env python3
import contextlib
from dataclasses import replace
import http.client
import importlib
import json
import os
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


class SearchProviderTests(unittest.TestCase):
    def test_serper_without_key_is_explicitly_unavailable(self):
        with temporary_env(**required_env(TAVILY_KEY_0=None, SERPER_KEY_0=None)):
            reload_config()
            sys.modules.pop("tg_bot.tools.search", None)
            search = importlib.import_module("tg_bot.tools.search")
            self.assertEqual(search._execute_serper("test", "general"), "Serper 未配置")

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


class ReplyIntegrationTests(unittest.TestCase):
    def test_writer_prompt_declares_stable_sections(self):
        writer = importlib.import_module("tg_bot.agents.writer")
        for heading in ("结论", "关键依据", "下一步", "来源"):
            self.assertIn(heading, writer._SYS_WRITER)

    def test_today_report_pack_declares_report_mode(self):
        with temporary_env(**required_env()):
            for module_name in ("tg_bot.evidence", "tg_bot.storage", "tg_bot.config"):
                sys.modules.pop(module_name, None)
            evidence = importlib.import_module("tg_bot.evidence")
            pack = evidence.build_today_report_pack(
                "今天午报有什么", report_text="AI 新闻\n来源：example.com"
            )
        self.assertEqual(pack["reply_mode"], "report")


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
        text = self.daily.render_daily_report([], self.now, timezone_name="UTC")
        self.assertIn("2026-07-12 13:00", text)

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
        self.assertEqual(sections.section_spec("steam").kind, "event")

        def fake_collector(*_args, **_kwargs):
            return "ok"

        sections.register_section_collector("steam", fake_collector)
        self.assertIs(sections.get_section_collector("steam"), fake_collector)

    def test_same_event_is_cooldown_scoped_to_each_section(self):
        first_ai = self._candidate("A new proxy tool release", category="ai_tech")
        first_proxy = self._candidate("A new proxy tool release", category="proxy")
        events = self.daily.cluster_candidates([first_ai, first_proxy])
        selected = self.daily.select_events(
            events, {"events": {}}, now=self.now, per_category=1
        )
        self.assertEqual({event.category for event in selected}, {"ai_tech", "proxy"})

    def test_sectioned_report_skips_repeated_event_but_keeps_heading(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_sections", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate = module.NewsCandidate(
            category="steam",
            title="Steam discount event",
            summary="A game is discounted.",
            url="https://store.steampowered.com/app/1",
            domain="steampowered.com",
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
        self.assertIn("Steam", first["report_text"])
        second = module.build_report(
            [updated], now=self.now + module.timedelta(days=1), state=first["state"],
            section_specs=module.DEFAULT_REPORT_SECTIONS,
        )
        self.assertIn("Steam", second["report_text"])
        self.assertIn("跳过重复", second["report_text"])
        self.assertNotIn("1. Steam discount event", second["report_text"])

    def test_repeated_event_does_not_restore_stale_external_section(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_repeated_external", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate = module.NewsCandidate(
            category="steam", title="Steam discount event", summary="A game is discounted.",
            url="https://store.steampowered.com/app/1", domain="steampowered.com",
            published_at="2026-07-12T11:00:00+00:00", relevance=0.9, source="fixture",
        )
        first = module.build_report(
            [candidate], now=self.now, state={"schema_version": 1, "events": {}},
            section_specs=module.DEFAULT_REPORT_SECTIONS,
        )
        legacy = "【Steam 优惠】\n昨日重复内容\n"
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
        self.assertIn("跳过重复", second["report_text"])
        self.assertNotIn("昨日重复内容", second["report_text"])

    def test_stale_event_is_not_described_as_duplicate(self):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_stale_event", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate = module.NewsCandidate(
            category="steam", title="Old Steam event", summary="old", url="https://store.steampowered.com/app/2",
            domain="steampowered.com", published_at="2026-07-10T11:00:00+00:00", relevance=0.9,
            source="fixture",
        )
        result = module.build_report(
            [candidate], now=self.now, state={"schema_version": 1, "events": {}},
            section_specs=module.DEFAULT_REPORT_SECTIONS,
            legacy_report_text="【Steam 降价优惠】\n昨日旧活动",
        )
        self.assertIn("今日暂无可验证的新内容", result["report_text"])
        self.assertNotIn("跳过重复", result["report_text"])
        self.assertNotIn("昨日旧活动", result["report_text"])

    def test_external_sections_are_preserved_when_no_new_candidates(self):
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
        self.assertIn("安阳 25°C", result["report_text"])
        self.assertIn("sing-box 更新", result["report_text"])

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
        for section_id in ("ai_tech", "proxy", "hackernews", "github", "steam", "cold_knowledge"):
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
        sections = module.collect_snapshot_sections(
            module.DEFAULT_REPORT_SECTIONS,
            "【天气预报】\n昨日 20°C",
            diagnostics,
        )
        self.assertIn("安阳当前 26°C", sections["weather"])
        self.assertEqual(diagnostics, [])

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
        self.assertIn("今日热点日报", result["report_text"])
        self.assertIn("events", result["state"])
        self.assertEqual(len(result["selected"]), 1)

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

    def test_main_provider_failure_keeps_previous_report(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-cli-")
        try:
            with temporary_env(**required_env(TG_BOT_DATA_DIR=data_dir)):
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
                    self.assertEqual(module.main([]), 1)
                with open(report_path, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "previous report")
                with open(os.path.join(data_dir, "daily_report_status.json"), encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle)["status"], "stale_previous")
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
                    self.assertEqual(module.main(["--dry-run"]), 1)
                self.assertFalse(os.path.exists(os.path.join(data_dir, "daily_report_status.json")))
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

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
                with patch.object(module, "collect_candidates", return_value=([], ["global: no_candidates"])):
                    self.assertEqual(module.main([]), 0)
                with open(os.path.join(data_dir, "daily_report_status.json"), encoding="utf-8") as handle:
                    status = json.load(handle)
                self.assertEqual(status["status"], "fresh")
                with open(os.path.join(data_dir, "today_report.txt"), encoding="utf-8") as handle:
                    report = handle.read()
                self.assertIn("【天气预报】", report)
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
    def test_send_returns_delivery_status(self):
        bot_utils = importlib.import_module("tg_bot.bot_utils")
        with patch.object(bot_utils, "http_post", return_value={"ok": True}):
            self.assertTrue(bot_utils.send(1, "hello"))
        with patch.object(bot_utils, "http_post", return_value={"ok": False}):
            self.assertFalse(bot_utils.send(1, "hello"))


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

        with patch("tg_bot.tools.search.execute_news_candidates", side_effect=fake_search):
            candidates, diagnostics = module.collect_candidates(("china", "global", "ai_tech"))
        self.assertEqual([item.category for item in candidates], ["china", "global", "ai_tech"])
        self.assertEqual(diagnostics, [])

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

        with patch("tg_bot.tools.search.execute_news_candidates", side_effect=fake_search):
            candidates, diagnostics = module.collect_candidates(("china", "global"))
        self.assertEqual([item.category for item in candidates], ["china"])
        self.assertEqual(diagnostics, ["global: provider_error:TimeoutError"])

    def test_report_content_hash_ignores_generation_timestamp(self):
        module = self._load_builder("build_daily_report_hash")
        first = module.build_report([], now=module.datetime(2026, 7, 12, 13, tzinfo=module.timezone.utc))
        second = dict(first, generated_at="2026-07-13T13:00:00+00:00")
        second["report_text"] = first["report_text"].replace("2026-07-12 21:00", "2026-07-13 21:00", 1)
        self.assertEqual(module._report_content_hash(first), module._report_content_hash(second))

    def test_push_is_idempotent_for_unchanged_report(self):
        data_dir = tempfile.mkdtemp(prefix="tg-bot-report-push-")
        try:
            with temporary_env(**required_env(
                TG_BOT_DATA_DIR=data_dir,
                DAILY_REPORT_PUSH="true",
                DAILY_REPORT_NATIVE_SNAPSHOTS="false",
            )):
                for module_name in ("tg_bot.storage", "tg_bot.config", "tg_bot.bot_utils"):
                    sys.modules.pop(module_name, None)
                module = self._load_builder("build_daily_report_push")
                with patch.object(module, "collect_candidates", return_value=([], [])), \
                     patch("tg_bot.bot_utils.send", return_value=True) as send:
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
                    published_at="2026-07-14T10:00:00+00:00",
                    relevance=0.9,
                    source="fixture",
                )
                with patch.object(module, "collect_candidates", return_value=([candidate], [])), \
                     patch("tg_bot.bot_utils.send", side_effect=[False, True]) as send:
                    self.assertEqual(module.main([]), 1)
                    self.assertEqual(module.main([]), 0)
                    self.assertEqual(module.main([]), 0)
                self.assertEqual(send.call_count, 2)
                with open(os.path.join(data_dir, "daily_report_status.json"), encoding="utf-8") as handle:
                    status = json.load(handle)
                self.assertEqual(status["push"]["status"], "skipped_unchanged")
                self.assertEqual(status["event_count"], 1)
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
