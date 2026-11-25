"""Tests for the cost/speed optimizations and the new feature commands."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from tg_bot.tests.test_core_units import required_env, temporary_env

with temporary_env(**required_env()):
    import tg_bot.config  # noqa: F401  (config needs env at import time)

from tg_bot.agents.local_check import local_fact_check
from tg_bot.core.contracts import PipelineConfig, Source
from tg_bot.features.store import BJ


class _DataDirCase(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="tg-bot-feature-")
        self._patches = [
            patch("tg_bot.config.DATA_DIR", self.data_dir),
            patch("tg_bot.config.DAILY_LOGS_DIR", os.path.join(self.data_dir, "daily_logs")),
            patch.dict(os.environ, {"TG_BOT_DATA_DIR": self.data_dir}),
        ]
        for p in self._patches:
            p.start()
        self.sent = []

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def send(self, chat_id, text):
        self.sent.append(text)
        return True


def _src(body, sid="R1"):
    return Source(id=sid, url="https://example.com/" + sid, domain="example.com",
                  title="t", snippet=body, full_content=body, tool="web_search")


class UsageLedgerTests(_DataDirCase):
    def test_records_and_summarizes_by_stage(self):
        from tg_bot.usage import llm_stage, load_usage, record_llm_usage, summarize_usage
        resp = {"usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                          "prompt_cache_hit_tokens": 600, "prompt_cache_miss_tokens": 400}}
        with llm_stage("critic"):
            record_llm_usage({"model": "m"}, resp)
        with llm_stage("writer"):
            record_llm_usage({"model": "m"}, resp)
            record_llm_usage({"model": "m"}, resp)
        rows = load_usage(1)
        self.assertEqual(len(rows), 3)
        summary = summarize_usage(rows)
        self.assertEqual(summary["total"]["calls"], 3)
        self.assertEqual(list(summary["by_stage"])[0], "writer")
        self.assertGreater(summary["total"]["cost"], 0)

    def test_ignores_responses_without_usage(self):
        from tg_bot.usage import load_usage, record_llm_usage
        record_llm_usage({}, {"choices": []})
        self.assertEqual(load_usage(1), [])

    def test_http_post_hook_records_deepseek_usage(self):
        import io
        from tg_bot.tools import fetch
        from tg_bot.usage import load_usage

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        body = json.dumps({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        with patch.object(fetch, "urlopen", return_value=_Resp(body)):
            fetch.http_post("https://api.deepseek.com/chat/completions", {"model": "m"})
        self.assertEqual(len(load_usage(1)), 1)


class CostCommandTests(_DataDirCase):
    def test_report_and_budget_alert_once_per_day(self):
        from tg_bot.features import cost
        from tg_bot.usage import llm_stage, record_llm_usage
        self.assertIn("还没有模型调用记录", cost.render_cost_report())
        with llm_stage("disambig"):
            record_llm_usage({}, {"usage": {"prompt_tokens": 2_000_000, "completion_tokens": 0}})
        report = cost.render_cost_report()
        self.assertIn("意图判断", report)
        self.assertIn("平均每轮", report)
        cost.handle_cost(1, "/cost budget 1", self.send)
        self.assertIn("每日预算设为", self.sent[-1])
        self.assertIn("超过每日预算", cost.check_budget_alert())
        self.assertEqual(cost.check_budget_alert(), "")


class LocalCheckTests(unittest.TestCase):
    def test_passes_when_numbers_are_in_cited_source(self):
        result = local_fact_check("公司 2024 年营收 1,200 亿元[来源1]。", [_src("2024年营收1200亿元")])
        self.assertTrue(result.ok, result.issues)

    def test_flags_missing_number_and_bad_reference(self):
        result = local_fact_check("营收 999 亿元[来源1]。另一个说法[来源5]。", [_src("营收 1200 亿元")])
        self.assertFalse(result.ok)
        reasons = " ".join(i["reason"] for i in result.issues)
        self.assertIn("999", reasons)
        self.assertIn("不存在的来源编号", reasons)

    def test_number_from_other_source_is_soft(self):
        result = local_fact_check("增长 35%[来源1]。", [_src("增长很快"), _src("同比增长35%", "R2")])
        self.assertEqual(result.issues[0]["severity"], "SOFT")


class RuleSummaryTests(unittest.TestCase):
    def test_rule_summary_keeps_whole_sentences(self):
        from tg_bot.pipeline.gather import rule_summary
        text = "🌤 安阳今天晴，最高 12 度[来源1]。明天降温。" + "后面还有很多很多内容。" * 10
        out = rule_summary(text)
        self.assertTrue(out.startswith("安阳今天晴"))
        self.assertNotIn("[来源", out)
        self.assertLessEqual(len(out), 60)

    def test_context_summary_does_not_call_model_by_default(self):
        from tg_bot.pipeline import gather
        with patch.object(gather, "http_post") as post:
            self.assertTrue(gather.summarize_for_context("短回复。"))
        post.assert_not_called()


class SmartCriticPipelineTests(unittest.TestCase):
    source = {"id": "R1", "url": "https://example.com/a", "domain": "example.com", "title": "a",
              "snippet": "2024年营收1200亿元", "full_content": "2024年营收1200亿元", "tool": "web_search"}

    def _run(self, reply, user_text="公司营收多少", **kwargs):
        from tg_bot.core.pipeline import run_search_pipeline
        meta = {"rounds": [], "tool_calls_summary": [], "tool_results": [], "fetched_pages": [],
                "failed_urls": [], "facts_json": {}}
        cfg = PipelineConfig(query_fixer=True, curator=False, critic=True, patcher=True)
        with temporary_env(**required_env()), \
                patch("tg_bot.pipeline.gather.gather_ai", return_value=([dict(self.source)], meta)), \
                patch("tg_bot.agents.writer.write", return_value=(reply, "")), \
                patch("tg_bot.agents.query_fixer.fix_query") as fix, \
                patch("tg_bot.agents.critic.critique") as critique:
            from tg_bot.core.contracts import CriticReport
            critique.return_value = CriticReport(verdict="pass")
            out = run_search_pipeline(user_text, ["营收"], config=cfg, query_variants=["营收 2024"], **kwargs)
        return out, fix, critique

    def test_local_pass_skips_model_critic_and_query_fixer(self):
        (reply, status, meta), fix, critique = self._run("2024 年营收 1200 亿元[来源1]。")
        self.assertEqual(status, "local_pass")
        critique.assert_not_called()
        fix.assert_not_called()
        self.assertEqual(meta["query_variants"], ["营收 2024"])

    def test_suspicious_draft_is_deferred_when_allowed(self):
        (reply, status, meta), _fix, critique = self._run("营收 999 亿元[来源1]。", allow_deferred_critic=True)
        self.assertEqual(status, "deferred")
        critique.assert_not_called()
        self.assertIn("deferred_critic", meta)

    def test_suspicious_draft_runs_model_critic_when_not_deferred(self):
        (_reply, status, _meta), _fix, critique = self._run("营收 999 亿元[来源1]。")
        self.assertEqual(status, "pass")
        critique.assert_called_once()

    def test_high_risk_always_uses_model_critic(self):
        (_reply, status, _meta), _fix, critique = self._run(
            "2024 年营收 1200 亿元[来源1]。", user_text="这只股票的财报营收多少")
        self.assertEqual(status, "pass")
        critique.assert_called_once()


class RemindTests(_DataDirCase):
    now = datetime(2026, 11, 25, 10, 0, tzinfo=BJ)

    def test_parses_common_phrases(self):
        from tg_bot.features.remind import parse_reminder
        cases = {
            "30分钟后 喝水": (datetime(2026, 11, 25, 10, 30, tzinfo=BJ), "喝水", False),
            "两小时后收衣服": (datetime(2026, 11, 25, 12, 0, tzinfo=BJ), "收衣服", False),
            "明天9点 交报告": (datetime(2026, 11, 26, 9, 0, tzinfo=BJ), "交报告", False),
            "下午3点半 取快递": (datetime(2026, 11, 25, 15, 30, tzinfo=BJ), "取快递", False),
            "15:45 开会": (datetime(2026, 11, 25, 15, 45, tzinfo=BJ), "开会", False),
            "每天8点 吃药": (datetime(2026, 11, 26, 8, 0, tzinfo=BJ), "吃药", True),
            "后天早上八点二十分 体检": (datetime(2026, 11, 27, 8, 20, tzinfo=BJ), "体检", False),
            "9点 散步": (datetime(2026, 11, 25, 21, 0, tzinfo=BJ), "散步", False),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_reminder(text, self.now), expected, text)
        self.assertIsNone(parse_reminder("随便什么时候", self.now))

    def test_due_reminders_fire_and_daily_ones_roll_forward(self):
        from tg_bot.features import remind
        remind.add_reminder("30分钟后 喝水", self.now)
        remind.add_reminder("每天10点半 站起来", self.now)
        self.assertEqual(remind.pop_due(self.now), [])
        later = self.now + timedelta(minutes=31)
        fired = remind.pop_due(later)
        self.assertEqual(len(fired), 2)
        self.assertIn("站起来", remind.list_reminders())
        self.assertNotIn("喝水", remind.list_reminders())


class WatchTests(_DataDirCase):
    def test_baseline_then_only_new_items_are_pushed(self):
        from tg_bot.features import watch
        now = datetime(2026, 11, 25, 10, 0, tzinfo=BJ)
        batches = [
            [{"title": "英伟达发布新一代芯片 - 新浪", "url": "https://a.com/1", "summary": "s"}],
            [{"title": "英伟达发布新一代芯片 - 网易", "url": "https://b.com/9", "summary": "s"},
             {"title": "新进展 B", "url": "https://a.com/2?utm=x", "summary": "新内容"}],
        ]
        search = lambda q: (batches.pop(0), [])
        self.assertIn("开始关注", watch.add_watch("英伟达"))
        self.assertIn("已经在关注", watch.add_watch("英伟达"))
        self.assertEqual(watch.check_watches(search, now=now), [])  # baseline
        self.assertEqual(watch.check_watches(search, now=now + timedelta(hours=1)), [])  # not due
        msgs = watch.check_watches(search, now=now + timedelta(hours=13))
        self.assertEqual(len(msgs), 1)
        self.assertIn("新进展 B", msgs[0])
        self.assertNotIn("新一代芯片", msgs[0])
        self.assertIn("已推送 1 条", watch.list_watches())
        self.assertIn("已取消", watch.remove_watch(1))


class LinkDigestTests(_DataDirCase):
    def test_digest_uses_cache_on_repeat(self):
        from tg_bot.features import link_digest
        calls = []
        fetch = lambda url: "[正文来源：x]\n\n" + "正文内容" * 100
        chat = lambda *a, **k: calls.append(1) or "一句话：测试\n- 要点"
        first = link_digest.digest("https://example.com/a", fetch=fetch, chat=chat)
        second = link_digest.digest("https://example.com/a", fetch=fetch, chat=chat)
        self.assertIn("链接速读", first)
        self.assertIn("缓存", second)
        self.assertEqual(len(calls), 1)

    def test_fetch_failure_is_reported_without_model_call(self):
        from tg_bot.features import link_digest
        out = link_digest.digest("https://example.com/b", fetch=lambda u: "正文抓取失败（URL 不安全）",
                                 chat=lambda *a, **k: self.fail("should not call model"))
        self.assertIn("抓不下来", out)

    def test_link_detection(self):
        from tg_bot.features.link_digest import extract_url, is_link_message
        self.assertTrue(is_link_message("https://example.com/x"))
        self.assertFalse(is_link_message("看看 https://example.com/x"))
        self.assertEqual(extract_url("/tldr https://e.com"), "https://e.com")


class FunAndDispatchTests(_DataDirCase):
    def test_fun_fact_does_not_repeat_until_exhausted(self):
        from tg_bot.features import fun
        seen = {fun.pick_fact() for _ in range(len(fun.FUN_FACTS))}
        self.assertEqual(len(seen), len(fun.FUN_FACTS))

    def test_quiz_hides_answers_in_spoilers(self):
        from tg_bot.features import fun
        payload = json.dumps({"questions": [{"q": "Q1", "options": ["A. x", "B. y"], "answer": "A", "why": "w"}]})
        out = fun.build_quiz("日报" * 100, chat=lambda *a, **k: payload)
        self.assertIn("<tg-spoiler>A · w</tg-spoiler>", out)
        self.assertIn("还没生成", fun.build_quiz(""))

    def test_dispatch_routes_commands_and_leaves_chat_alone(self):
        import tg_bot.features as features
        self.assertTrue(features.dispatch(1, "/features", self.send))
        self.assertIn("/watch", self.sent[-1])
        self.assertTrue(features.dispatch(1, "/remind list", self.send))
        self.assertFalse(features.dispatch(1, "今天天气怎么样", self.send))
        self.assertFalse(features.dispatch(1, "/funny", self.send))

    def test_weekly_lite_summarizes_logs(self):
        from tg_bot.features import weekly
        from tg_bot.features.store import now_bj
        os.makedirs(os.path.join(self.data_dir, "daily_logs"), exist_ok=True)
        with open(os.path.join(self.data_dir, "daily_logs", now_bj().strftime("%Y-%m-%d") + ".jsonl"), "w",
                  encoding="utf-8") as handle:
            for _ in range(3):
                handle.write(json.dumps({"role": "user", "content": "英伟达财报怎么样"}, ensure_ascii=False) + "\n")
        out = weekly.build_weekly(lite=True)
        self.assertIn("一共聊了 3 轮", out)
        self.assertIn("英伟达", out)


class DailyReportSearchCacheTests(_DataDirCase):
    def test_same_query_reuses_cached_results(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                            "scripts", "build-daily-report.py")
        spec = importlib.util.spec_from_file_location("build_daily_report_cache", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []

        def search(query):
            calls.append(query)
            return [{"title": "t", "url": "https://e.com/1"}], []

        first = module._cached_news_search("q", search)
        second = module._cached_news_search("q", search)
        self.assertEqual(first[0], second[0])
        self.assertEqual(calls, ["q"])
        with patch.dict(os.environ, {"DAILY_REPORT_SEARCH_CACHE_HOURS": "0"}):
            module._cached_news_search("q", search)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
