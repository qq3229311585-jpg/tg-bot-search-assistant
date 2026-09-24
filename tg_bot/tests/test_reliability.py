#!/usr/bin/env python3
"""tg_bot/tests/test_reliability.py

覆盖"未验证内容可靠性修复方案"里的关键行为（A/B 部分）。
不联网：所有 http_post / fast_chat / gather_ai 调用都被 mock 掉。
"""
from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest.mock import patch

from tg_bot.tests.test_core_units import required_env, temporary_env

from tg_bot.core.contracts import CriticReport, Issue, PipelineConfig, Source, WriteRequest
from tg_bot.workers import claim_check
from tg_bot.workers.claim_check import (
    Fact,
    check_claims,
    compute_verdict,
    deterministic_patch,
    extract_hard_facts,
    fact_supported,
    finalize_reply,
    normalize_sentence,
    remove_sentences,
    split_sentences,
    validate_patch,
)
from tg_bot.workers.gather_tools import parse_date_tag, parse_search_entries
from tg_bot.workers.source_utils import deduplicate, format_sources_for_writer, is_nav_or_empty


def _src(id_="1", url="https://a.com/x", domain="a.com", title="标题", body="",
         tool="web_search", published_at=""):
    return Source(
        id=id_, url=url, domain=domain, title=title, snippet=body,
        full_content="", tool=tool, query="", published_at=published_at,
    )


# ══════════════════════════════════════════════════════════════════════════
# D1: claim_check 基础抽取/核查
# ══════════════════════════════════════════════════════════════════════════
class ClaimCheckExtractionTests(unittest.TestCase):
    def test_extracts_thousands_separator(self):
        sent = split_sentences("销量达到1,234件。")[0]
        facts = extract_hard_facts(sent)
        self.assertEqual(facts[0].value, 1234.0)
        self.assertEqual(facts[0].kind, "number")

    def test_extracts_fullwidth_digits(self):
        sent = split_sentences("销量达到１，２３４件。")[0]
        facts = extract_hard_facts(sent)
        self.assertEqual(facts[0].value, 1234.0)

    def test_extracts_percent(self):
        sent = split_sentences("占比12.5%。")[0]
        facts = extract_hard_facts(sent)
        self.assertEqual(facts[0].kind, "percent")
        self.assertEqual(facts[0].value, 12.5)

    def test_extracts_chinese_unit_number(self):
        sent = split_sentences("用户数突破3万。")[0]
        facts = extract_hard_facts(sent)
        self.assertEqual(facts[0].value, 30000.0)

        sent2 = split_sentences("规模达到1.2亿。")[0]
        facts2 = extract_hard_facts(sent2)
        self.assertEqual(facts2[0].value, 120000000.0)

    def test_extracts_dates_and_years(self):
        sent = split_sentences("这是2024年的事。")[0]
        facts = extract_hard_facts(sent)
        self.assertEqual(facts[0].kind, "year")
        self.assertEqual(facts[0].value, 2024.0)

        sent2 = split_sentences("日期是2024-03-05。")[0]
        facts2 = extract_hard_facts(sent2)
        self.assertEqual(facts2[0].kind, "date")

    def test_ignores_list_prefix_numbers(self):
        sent = split_sentences("1. 用户破了3万。")[0]
        facts = extract_hard_facts(sent)
        # 不应该把行首的 "1." 当成事实数字
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].raw, "3万")

        sent2 = split_sentences("① 第一条内容有100万用户。")[0]
        facts2 = extract_hard_facts(sent2)
        self.assertEqual(len(facts2), 1)
        self.assertEqual(facts2[0].raw, "100万")

    def test_fuzzy_tolerance_for_vague_words(self):
        sent = split_sentences("约3.001万元。")[0]
        fact = extract_hard_facts(sent)[0]
        self.assertTrue(fact_supported(fact, "销售额达到3万元", fuzzy=True))
        self.assertFalse(fact_supported(fact, "销售额达到3万元", fuzzy=False))

    def test_years_never_get_fuzzy_tolerance(self):
        sent = split_sentences("约2025年发布。")[0]
        fact = extract_hard_facts(sent)[0]
        self.assertFalse(fact_supported(fact, "本产品2024年发布", fuzzy=True))


class ClaimCheckIssueTests(unittest.TestCase):
    def setUp(self):
        self.s1 = _src("1", "https://a.com/x", "a.com", "t1", "2024年，销售额达到3万元，占比12%。")
        self.s2 = _src("2", "https://b.com/y", "b.com", "t2", "另一篇报道说销售额约3.05万元。")

    def test_ref_out_of_bounds_is_hard(self):
        reply = "今年营收创新高[来源5]。"
        issues = check_claims(reply, [self.s1, self.s2])
        self.assertTrue(any(i.severity == "HARD" and "不存在的来源" in i.reason for i in issues))

    def test_unlabeled_number_found_in_sources_is_soft(self):
        reply = "去年增长了12%。"
        issues = check_claims(reply, [self.s1, self.s2])
        soft = [i for i in issues if i.severity == "SOFT"]
        self.assertTrue(any("缺少来源标注" in i.reason for i in soft))
        self.assertTrue(any("来源1" in i.suggested_fix for i in soft))

    def test_unlabeled_number_not_found_anywhere_is_hard(self):
        reply = "今年新增了500人。"
        issues = check_claims(reply, [self.s1, self.s2])
        self.assertTrue(any(i.severity == "HARD" and "无来源支撑" in i.reason for i in issues))

    def test_mislabeled_source_is_soft_with_correct_target(self):
        reply = "另有报道称销售额约3.05万元[来源1]。"
        issues = check_claims(reply, [self.s1, self.s2])
        self.assertTrue(any(
            i.severity == "SOFT" and "应为来源2" in i.reason for i in issues
        ))

    def test_forged_source_block_url_is_hard(self):
        reply = (
            "结论：销售额达到3万元[来源1]。\n\n"
            "来源\n"
            "- t1（a.com）\n"
            "- fake site（fake-not-real.com）\n"
        )
        issues = check_claims(reply, [self.s1, self.s2])
        self.assertTrue(any(
            i.severity == "HARD" and "伪造来源" in i.reason for i in issues
        ))

    def test_supported_cited_number_has_no_issue(self):
        reply = "去年增长了12%[来源1]。"
        issues = check_claims(reply, [self.s1, self.s2])
        self.assertEqual(issues, [])

    def test_direct_api_source_numbers_checked_same_way(self):
        api_src = _src("1", "", "vps_traffic", "VPS 流量", "本月流量共消耗120GB。", tool="vps_traffic")
        reply = "本月流量消耗了120GB[来源1]。"
        self.assertEqual(check_claims(reply, [api_src]), [])
        reply_bad = "本月流量消耗了999GB[来源1]。"
        issues = check_claims(reply_bad, [api_src])
        self.assertTrue(any(i.severity == "HARD" for i in issues))


# ══════════════════════════════════════════════════════════════════════════
# D2: compute_verdict
# ══════════════════════════════════════════════════════════════════════════
class ComputeVerdictTests(unittest.TestCase):
    def test_no_issues_is_pass(self):
        self.assertEqual(compute_verdict([]), "pass")

    def test_two_hard_is_rewrite(self):
        issues = [Issue("a", "", "HARD", "r"), Issue("b", "", "HARD", "r")]
        self.assertEqual(compute_verdict(issues), "rewrite")

    def test_five_total_is_rewrite(self):
        issues = [Issue(f"s{i}", "", "SOFT", "r") for i in range(5)]
        self.assertEqual(compute_verdict(issues), "rewrite")

    def test_one_hard_few_soft_is_patch(self):
        issues = [Issue("a", "", "HARD", "r"), Issue("b", "", "SOFT", "r")]
        self.assertEqual(compute_verdict(issues), "patch")

    def test_verdict_is_computed_by_code_not_model(self):
        # 模型说 pass，但规则查出一个 HARD：最终结果不能是 pass。
        rule_issues = [Issue("坏句子", "", "HARD", "无来源支撑的数字/日期", origin="rule")]
        self.assertNotEqual(compute_verdict(rule_issues), "pass")


# ══════════════════════════════════════════════════════════════════════════
# D3: Critic 模型失败时的行为
# ══════════════════════════════════════════════════════════════════════════
class CriticFailureTests(unittest.TestCase):
    def test_llm_failure_with_rule_issues_is_not_unknown(self):
        from tg_bot.agents.critic import critique

        src = _src("1", "https://a.com", "a.com", "t", "素材里完全没有这个数字。")
        reply = "今年增长了500%。"  # 无标注、素材里也找不到 -> 规则会产生 HARD

        with temporary_env(**required_env()):
            with patch("tg_bot.tools.fetch.http_post", side_effect=RuntimeError("network down")):
                report = critique(reply, [src], "增长多少", 0)

        self.assertNotEqual(report.verdict, "unknown")
        self.assertTrue(report.llm_failed)
        self.assertGreater(report.rule_issue_count, 0)

    def test_llm_failure_without_rule_issues_is_unknown(self):
        from tg_bot.agents.critic import critique

        src = _src("1", "https://a.com", "a.com", "t", "这是一段没有数字的说明性文字。")
        reply = "这个话题目前资料未明确说明细节。"  # 无硬事实 -> 规则不会报问题

        with temporary_env(**required_env()):
            with patch("tg_bot.tools.fetch.http_post", side_effect=RuntimeError("network down")):
                report = critique(reply, [src], "介绍一下", 0)

        self.assertEqual(report.verdict, "unknown")
        self.assertTrue(report.llm_failed)
        self.assertEqual(report.rule_issue_count, 0)

    def test_model_pass_but_rule_hard_does_not_pass(self):
        from tg_bot.agents.critic import critique

        src = _src("1", "https://a.com", "a.com", "t", "素材原文没有任何这个数字。")
        reply = "今年营收暴涨了800%。"

        fake_resp = {
            "choices": [{"message": {"content": '{"verdict":"pass","issues":[]}', "reasoning_content": ""}}]
        }
        with temporary_env(**required_env()):
            with patch("tg_bot.tools.fetch.http_post", return_value=fake_resp):
                report = critique(reply, [src], "问题", 0)

        self.assertNotEqual(report.verdict, "pass")
        self.assertTrue(any(i.severity == "HARD" for i in report.issues))


# ══════════════════════════════════════════════════════════════════════════
# D4: validate_patch / deterministic_patch
# ══════════════════════════════════════════════════════════════════════════
class PatchValidationTests(unittest.TestCase):
    def setUp(self):
        self.s1 = _src("1", "https://a.com", "a.com", "t1", "2024年销售额达到3万元。")
        self.orig = "结论：销售额达到3万元[来源1]。今年新增了500人。"
        self.issues = check_claims(self.orig, [self.s1])

    def test_over_deletion_triggers_deterministic_fallback(self):
        ok, reason = validate_patch(self.orig, "不知道。", self.issues, [self.s1])
        self.assertFalse(ok)
        self.assertIn("40%", reason)

    def test_unflagged_sentence_altered_triggers_fallback(self):
        # 把没被点名的句子也改写了
        bad_patch = "结论：营收达到3万元[来源1]。"
        ok, reason = validate_patch(self.orig, bad_patch, self.issues, [self.s1])
        self.assertFalse(ok)

    def test_new_hard_issue_triggers_fallback(self):
        bad_patch = "结论：销售额达到3万元[来源1]。今年增长了800%。"
        ok, reason = validate_patch(self.orig, bad_patch, self.issues, [self.s1])
        self.assertFalse(ok)
        self.assertIn("HARD", reason)

    def test_valid_patch_passes(self):
        good_patch = "结论：销售额达到3万元[来源1]。"
        ok, reason = validate_patch(self.orig, good_patch, self.issues, [self.s1])
        self.assertTrue(ok, reason)

    def test_deterministic_patch_removes_hard_and_fixes_ref(self):
        result = deterministic_patch(self.orig, self.issues, [self.s1])
        self.assertNotIn("500人", result)
        self.assertIn("[来源1]", result)

    def test_deterministic_patch_corrects_mislabeled_ref(self):
        s2 = _src("2", "https://b.com", "b.com", "t2", "销售额约3.05万元。")
        reply = "销售额约3.05万元[来源1]。"
        issues = check_claims(reply, [self.s1, s2])
        result = deterministic_patch(reply, issues, [self.s1, s2])
        self.assertIn("[来源2]", result)

    def test_patcher_falls_back_to_deterministic_on_model_failure(self):
        from tg_bot.agents.patcher import patch

        report = CriticReport(verdict="patch", issues=self.issues)
        with temporary_env(**required_env()):
            with patch_http_post_fail():
                result, mode = patch(self.orig, report, [self.s1], "问题")
        self.assertEqual(mode, "deterministic")
        self.assertNotIn("500人", result)

    def test_patcher_falls_back_when_model_output_is_out_of_bounds(self):
        from tg_bot.agents.patcher import patch

        report = CriticReport(verdict="patch", issues=self.issues)
        fake_resp = {"choices": [{"message": {"content": "不知道。"}}]}
        with temporary_env(**required_env()):
            with patch_module_http_post(return_value=fake_resp):
                result, mode = patch(self.orig, report, [self.s1], "问题")
        self.assertEqual(mode, "deterministic")


def patch_http_post_fail():
    return patch("tg_bot.tools.fetch.http_post", side_effect=RuntimeError("down"))


def patch_module_http_post(return_value):
    return patch("tg_bot.tools.fetch.http_post", return_value=return_value)


# ══════════════════════════════════════════════════════════════════════════
# D5: finalize_reply
# ══════════════════════════════════════════════════════════════════════════
class FinalizeReplyTests(unittest.TestCase):
    def test_removes_remaining_hard_sentences(self):
        s1 = _src("1", "https://a.com", "a.com", "t", "2024年销售额达到3万元。")
        reply = "销售额达到3万元[来源1]。今年新增了500人。"
        final_text, info = finalize_reply(reply, [s1])
        self.assertNotIn("500人", final_text)
        self.assertEqual(info["removed_count"], 1)

    def test_emptied_reply_uses_fallback(self):
        s1 = _src("1", "https://a.com", "a.com", "t", "这是一段完全不相关的说明文字。")
        reply = "今年新增了500人。"
        final_text, info = finalize_reply(reply, [s1])
        self.assertTrue(info["used_fallback"])
        self.assertIn("换个问法", final_text)

    def test_short_valid_reply_is_not_replaced(self):
        # 本来就很短但没有问题的回复不应该被兜底文案顶替
        s1 = _src("1", "https://a.com", "a.com", "t", "北京是中国的首都。")
        reply = "北京"
        final_text, info = finalize_reply(reply, [s1])
        self.assertEqual(final_text, "北京")
        self.assertFalse(info["used_fallback"])


class VerificationLevelTests(unittest.TestCase):
    def _run(self, sources, write_reply, critic_report, evidence_sufficient=True):
        from tg_bot.core.pipeline import run_search_pipeline

        gather_meta = {
            "rounds": [], "tool_calls_summary": [], "tool_results": [],
            "fetched_pages": [], "failed_urls": [],
            "sufficient": evidence_sufficient, "gather_reason": "" if evidence_sufficient else "缺口",
        }
        source_dicts = [
            {"id": s.id, "url": s.url, "domain": s.domain, "title": s.title,
             "snippet": s.snippet, "full_content": s.full_content, "tool": s.tool}
            for s in sources
        ]
        cfg = PipelineConfig(query_fixer=False, curator=True, critic=True, patcher=True)
        with temporary_env(**required_env(PIPELINE_SUPPLEMENT_GATHER="false")):
            with patch("tg_bot.pipeline.gather.gather_ai", return_value=(source_dicts, gather_meta)):
                with patch("tg_bot.agents.writer.write", return_value=(write_reply, "")):
                    with patch("tg_bot.agents.critic.critique", return_value=critic_report):
                        return run_search_pipeline("问题", ["关键词"], config=cfg)

    def test_level_verified(self):
        s1 = _src("1", "https://a.com", "a.com", "t", "2024年营收3万元。" * 5)
        reply, verify_status, meta = self._run([s1], "营收3万元[来源1]。", CriticReport(verdict="pass"))
        self.assertEqual(meta["verification"]["level"], "verified")

    def test_level_rule_only_when_llm_failed(self):
        s1 = _src("1", "https://a.com", "a.com", "t", "2024年营收3万元。" * 5)
        report = CriticReport(verdict="pass", issues=[], llm_failed=True, rule_issue_count=0)
        reply, verify_status, meta = self._run([s1], "营收3万元[来源1]。", report)
        self.assertEqual(meta["verification"]["level"], "rule_only")

    def test_level_insufficient_when_evidence_gap(self):
        s1 = _src("1", "https://a.com", "a.com", "t", "2024年营收3万元。" * 5)
        reply, verify_status, meta = self._run(
            [s1], "营收3万元[来源1]。", CriticReport(verdict="pass"),
            evidence_sufficient=False,
        )
        self.assertEqual(meta["verification"]["level"], "insufficient")


# ══════════════════════════════════════════════════════════════════════════
# D6: 长度控制——Critic 之后不再调用模型压缩
# ══════════════════════════════════════════════════════════════════════════
class LengthControlNoModelAfterCriticTests(unittest.TestCase):
    def test_no_model_compression_after_patch(self):
        from tg_bot.core.pipeline import run_search_pipeline

        s1 = _src("1", "https://a.com", "a.com", "t", "素材内容。" * 50)
        long_reply = "结论。" * 200  # 超长，触发短字数裁剪逻辑（目标字数很小时）
        gather_meta = {
            "rounds": [], "tool_calls_summary": [], "tool_results": [],
            "fetched_pages": [], "failed_urls": [], "sufficient": True, "gather_reason": "",
        }
        source_dicts = [{"id": "1", "url": s1.url, "domain": s1.domain, "title": s1.title,
                          "snippet": s1.snippet, "full_content": "", "tool": "web_search"}]
        issue = Issue("结论。", "", "SOFT", "缺少来源标注", suggested_fix="")
        cfg = PipelineConfig(query_fixer=False, curator=False, critic=True, patcher=True, max_rewrites=1)

        with temporary_env(**required_env(PIPELINE_SUPPLEMENT_GATHER="false")):
            with patch("tg_bot.pipeline.gather.gather_ai", return_value=(source_dicts, gather_meta)):
                with patch("tg_bot.agents.writer.write", return_value=(long_reply, "")):
                    with patch("tg_bot.agents.critic.critique",
                               return_value=CriticReport(verdict="patch", issues=[issue])):
                        with patch("tg_bot.agents.patcher.patch",
                                   return_value=(long_reply, "model")) as mock_patch:
                            with patch("tg_bot.pipeline.gather.fast_chat") as mock_fast_chat:
                                run_search_pipeline(
                                    "写一个80字的介绍", ["关键词"], config=cfg,
                                )
                # fast_chat（模型压缩）在 Critic/Patcher 之后不应该被调用
                mock_fast_chat.assert_not_called()
                mock_patch.assert_called()


# ══════════════════════════════════════════════════════════════════════════
# D7: parse_search_entries 保留 published_at；format_sources_for_writer 输出日期
# ══════════════════════════════════════════════════════════════════════════
class SourceDateThreadingTests(unittest.TestCase):
    def test_parse_date_tag_handles_iso_and_relative(self):
        self.assertEqual(parse_date_tag("2026-07-14"), "2026-07-14")
        relative = parse_date_tag("3 days ago")
        self.assertRegex(relative, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(parse_date_tag(""), "")

    def test_parse_search_entries_keeps_published_at_and_cleans_title(self):
        result = (
            "• Tornado Safety [2026-07-14]\n"
            "Shelter in a basement.\n"
            "https://www.ready.gov/tornadoes"
        )
        seq = iter(["R001"])
        entries = parse_search_entries(
            result=result, query="", tool="web_search", next_rid=lambda: next(seq)
        )
        self.assertEqual(entries[0]["title"], "Tornado Safety")
        self.assertEqual(entries[0]["published_at"], "2026-07-14")

    def test_format_sources_for_writer_shows_date_and_continuous_numbering(self):
        sources = [
            _src("1", "https://a.com", "a.com", "t1", "正文一" * 30, published_at="2026-07-14"),
            _src("2", "https://b.com", "b.com", "t2", "正文二" * 30, published_at=""),
        ]
        text = format_sources_for_writer(sources)
        self.assertIn("[来源1]", text)
        self.assertIn("发布于 2026-07-14", text)
        self.assertIn("[来源2]", text)
        # 第二条没有日期，不应该出现"发布于"字样紧跟在来源2标题后
        idx2 = text.index("[来源2]")
        line2_end = text.index("\n", idx2)
        self.assertNotIn("发布于", text[idx2:line2_end])


# ══════════════════════════════════════════════════════════════════════════
# D8: is_nav_or_empty 按工具区分门槛；deduplicate 优先全文
# ══════════════════════════════════════════════════════════════════════════
class SourceFilterTests(unittest.TestCase):
    def test_snippet_tool_has_lower_threshold(self):
        short_snippet = "这是一段" * 12  # 48 字，> 40 但 < 200
        self.assertFalse(is_nav_or_empty("标题", short_snippet, "web_search"))
        self.assertTrue(is_nav_or_empty("标题", short_snippet, "fetch_content"))

    def test_direct_api_tool_never_filtered_by_length(self):
        self.assertFalse(is_nav_or_empty("VPS流量", "12GB", "vps_traffic"))

    def test_empty_body_always_filtered(self):
        self.assertTrue(is_nav_or_empty("标题", "", "vps_traffic"))

    def test_deduplicate_prefers_longer_full_content(self):
        short = _src("1", "https://a.com/x", "a.com", "t", "摘要")
        long = _src("2", "https://a.com/x", "a.com", "t", "完整正文" * 100)
        result = deduplicate([short, long])
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].body), len(long.body))


# ══════════════════════════════════════════════════════════════════════════
# D9: 素材不足 -> 补搜一次
# ══════════════════════════════════════════════════════════════════════════
class SupplementGatherTests(unittest.TestCase):
    def test_insufficient_triggers_one_supplement_call(self):
        from tg_bot.core.pipeline import run_search_pipeline

        s1 = {"id": "1", "url": "https://a.com", "domain": "a.com", "title": "t",
              "snippet": "不够", "full_content": "内容不足以支撑" * 30, "tool": "web_search"}
        meta_insufficient = {
            "rounds": [], "tool_calls_summary": ["web_search(问题)"], "tool_results": [],
            "fetched_pages": [], "failed_urls": [], "sufficient": False, "gather_reason": "缺关键数据",
        }
        meta_sufficient = {
            "rounds": [], "tool_calls_summary": ["web_search(问题2)"], "tool_results": [],
            "fetched_pages": [], "failed_urls": [], "sufficient": True, "gather_reason": "",
        }
        calls = []

        def fake_gather(*args, **kwargs):
            calls.append(kwargs.get("retry_hint"))
            if len(calls) == 1:
                return [s1], dict(meta_insufficient)
            return [s1], dict(meta_sufficient)

        cfg = PipelineConfig(query_fixer=False, critic=False, patcher=False)
        with temporary_env(**required_env(PIPELINE_SUPPLEMENT_GATHER="true")):
            with patch("tg_bot.pipeline.gather.gather_ai", side_effect=fake_gather):
                with patch("tg_bot.agents.writer.write", return_value=("整理后的答案[来源1]。", "")):
                    run_search_pipeline("问题", ["关键词"], config=cfg)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls, [False, True])

    def test_still_insufficient_after_supplement_marks_write_request(self):
        from tg_bot.core.pipeline import run_search_pipeline

        s1 = {"id": "1", "url": "https://a.com", "domain": "a.com", "title": "t",
              "snippet": "不够", "full_content": "内容不足以支撑" * 30, "tool": "web_search"}
        meta_insufficient = {
            "rounds": [], "tool_calls_summary": [], "tool_results": [],
            "fetched_pages": [], "failed_urls": [], "sufficient": False, "gather_reason": "缺关键数据",
        }
        captured = {}

        def fake_write(req):
            captured["evidence_sufficient"] = req.evidence_sufficient
            captured["evidence_gaps"] = req.evidence_gaps
            return "整理后的答案[来源1]。", ""

        cfg = PipelineConfig(query_fixer=False, critic=False, patcher=False)
        with temporary_env(**required_env(PIPELINE_SUPPLEMENT_GATHER="true")):
            with patch("tg_bot.pipeline.gather.gather_ai", return_value=([s1], meta_insufficient)):
                with patch("tg_bot.agents.writer.write", side_effect=fake_write):
                    run_search_pipeline("问题", ["关键词"], config=cfg)

        self.assertFalse(captured["evidence_sufficient"])
        self.assertEqual(captured["evidence_gaps"], "缺关键数据")


# ══════════════════════════════════════════════════════════════════════════
# D10: disambig 启发式兜底 / lstrip 修复 / suggested_length
# ══════════════════════════════════════════════════════════════════════════
class DisambigFallbackTests(unittest.TestCase):
    def test_json_failure_falls_back_to_heuristic_social(self):
        with temporary_env(**required_env()):
            for name in list(sys.modules):
                if name.startswith("tg_bot.pipeline.disambig") or name == "tg_bot.pipeline":
                    sys.modules.pop(name, None)
            from tg_bot.pipeline.disambig import _pre_check

            with patch(
                "tg_bot.pipeline.disambig.http_post",
                return_value={"choices": [{"message": {"content": "不是JSON的胡言乱语"}}]},
            ):
                result = _pre_check("谢谢！")

        self.assertTrue(result.get("heuristic"))
        self.assertEqual(result["query_type"], "闲聊")
        self.assertFalse(result["needs_search"])
        self.assertIn("suggested_length", result)

    def test_response_failure_falls_back_to_heuristic_search(self):
        with temporary_env(**required_env()):
            for name in list(sys.modules):
                if name.startswith("tg_bot.pipeline.disambig") or name == "tg_bot.pipeline":
                    sys.modules.pop(name, None)
            from tg_bot.pipeline.disambig import _pre_check

            with patch("tg_bot.pipeline.disambig.http_post", return_value=None):
                result = _pre_check("今天美股行情怎么样")

        self.assertTrue(result.get("heuristic"))
        self.assertTrue(result["needs_search"])

    def test_retry_prefix_strip_does_not_eat_extra_characters(self):
        with temporary_env(**required_env()):
            for name in list(sys.modules):
                if name.startswith("tg_bot.pipeline.disambig") or name == "tg_bot.pipeline":
                    sys.modules.pop(name, None)
            from tg_bot.pipeline.disambig import _pre_check

            fake_toollog = [{
                "pre_searched": "缓存:witness kit,witty facts",
                "model_tools": [],
                "user": "上次那个问题",
                "reply_preview": "",
            }]
            with patch("tg_bot.storage.load_toollog", return_value=fake_toollog):
                result = _pre_check("再查一下")

        # 旧 bug：lstrip("缓存:") 会按字符集删除，"witness" 开头的 w/i/t 也会被吃掉。
        self.assertEqual(result["prev_searches"][0], "witness kit")


# ══════════════════════════════════════════════════════════════════════════
# D11: 上下文继承收紧
# ══════════════════════════════════════════════════════════════════════════
class ContextInheritanceTests(unittest.TestCase):
    @staticmethod
    def _load():
        with temporary_env(**required_env()):
            detailed_log_stub = types.ModuleType("tg_bot.detailed_log")
            detailed_log_stub.save_detailed_log = lambda **kwargs: None
            with patch.dict(sys.modules, {"tg_bot.detailed_log": detailed_log_stub}):
                sys.modules.pop("tg_bot.bot", None)
                return importlib.import_module("tg_bot.bot")

    def test_thanks_does_not_inherit_search(self):
        bot = self._load()
        pre = {"speech_act": "social_chat", "topic_continuity": "none"}
        route_info = {"category": "闲聊"}
        self.assertFalse(bot._should_inherit_search_context("谢谢", pre, route_info))

    def test_ok_does_not_inherit_search(self):
        bot = self._load()
        pre = {"speech_act": "", "topic_continuity": "none"}
        route_info = {"category": "其他"}
        self.assertFalse(bot._should_inherit_search_context("好的", pre, route_info))

    def test_why_inherits_search(self):
        bot = self._load()
        pre = {"speech_act": "", "topic_continuity": "none"}
        route_info = {"category": "其他"}
        self.assertTrue(bot._should_inherit_search_context("为什么", pre, route_info))

    def test_continue_same_topic_inherits(self):
        bot = self._load()
        pre = {"speech_act": "", "topic_continuity": "continue_same_topic"}
        route_info = {"category": "其他"}
        # 用一句不是"社交短句"的追问，避免被 _SOCIAL_RE 的兜底规则拦下来
        self.assertTrue(bot._should_inherit_search_context("那具体是什么价格", pre, route_info))


# ══════════════════════════════════════════════════════════════════════════
# D12: 快速路径——历史不重复、系统提示不含工具/把握分
# ══════════════════════════════════════════════════════════════════════════
class FastPathPromptTests(unittest.TestCase):
    def test_fast_prompt_has_no_tool_flow_or_confidence_score(self):
        with temporary_env(**required_env()):
            detailed_log_stub = types.ModuleType("tg_bot.detailed_log")
            detailed_log_stub.save_detailed_log = lambda **kwargs: None
            with patch.dict(sys.modules, {"tg_bot.detailed_log": detailed_log_stub}):
                sys.modules.pop("tg_bot.bot", None)
                bot = importlib.import_module("tg_bot.bot")

        prompt = bot._build_fast_system_prompt(["你是中文助手。"])
        self.assertNotIn("wikipedia_lookup", prompt)
        self.assertNotIn("把握分", prompt)
        self.assertNotIn("web_search", prompt)
        self.assertIn("没有调用任何搜索工具", prompt)


# ══════════════════════════════════════════════════════════════════════════
# D13: 本地证据 lane 不调用 gather_ai
# ══════════════════════════════════════════════════════════════════════════
class LocalEvidenceLaneTests(unittest.TestCase):
    def test_local_sources_skip_gather_ai(self):
        from tg_bot.core.pipeline import run_search_pipeline

        local_sources = [{
            "id": "LOCAL_VPS_TRAFFIC", "tool": "vps_traffic", "query": "流量",
            "title": "VPS 流量状态", "url": "local://vps_traffic", "domain": "local://vps_traffic",
            "snippet": "本月流量120GB", "full_content": "本月流量共消耗120GB。",
        }]
        cfg = PipelineConfig(critic=False, patcher=False)
        with temporary_env(**required_env()):
            with patch("tg_bot.pipeline.gather.gather_ai") as mock_gather:
                with patch("tg_bot.agents.writer.write",
                           return_value=("本月流量120GB[来源1]。", "")):
                    reply, verify_status, meta = run_search_pipeline(
                        "查一下流量", [], config=cfg, local_sources=local_sources,
                    )
        mock_gather.assert_not_called()
        self.assertEqual(len(meta["source_index"]), 1)
        self.assertEqual(meta["source_index"][0]["tool"], "vps_traffic")


# ══════════════════════════════════════════════════════════════════════════
# D14: Writer 请求——温度环境变量、当前日期、history 不作为 messages
# ══════════════════════════════════════════════════════════════════════════
class WriterRequestTests(unittest.TestCase):
    def test_default_temperature_is_0_3(self):
        from tg_bot.agents.writer import _writer_temperature
        with temporary_env(WRITER_TEMPERATURE=None):
            self.assertEqual(_writer_temperature(), 0.3)

    def test_temperature_from_env_var(self):
        from tg_bot.agents.writer import _writer_temperature
        with temporary_env(WRITER_TEMPERATURE="0.55"):
            self.assertEqual(_writer_temperature(), 0.55)

    def test_user_content_includes_current_date_and_no_history_messages(self):
        from tg_bot.agents.writer import write

        src = _src("1", "https://a.com", "a.com", "t", "正文内容。" * 30)
        req = WriteRequest(
            user_query="问题",
            sources=[src],
            target_words=(80, 200),
            history_context=[
                {"role": "user", "content": "上一轮问题"},
                {"role": "assistant", "content": "上一轮回答"},
            ],
        )

        captured = {}

        def fake_post(url, payload, headers=None, timeout=None):
            captured["payload"] = payload
            return {"choices": [{"message": {"content": "正文[来源1]。", "reasoning_content": ""}}]}

        with temporary_env(**required_env()):
            with patch("tg_bot.tools.fetch.http_post", side_effect=fake_post):
                write(req)

        messages = captured["payload"]["messages"]
        # 只有 system + 一条 user，history_context 不能被当成独立的 messages 塞进去
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        user_content = messages[1]["content"]
        self.assertIn("当前日期", user_content)
        self.assertIn("对话背景", user_content)
        self.assertIn("上一轮问题", user_content)
        self.assertIn("不能作为事实来源", user_content)


class ReviewRegressionTests(unittest.TestCase):
    def test_thousands_separator_before_cn_unit_is_supported(self):
        src = Source(id="1", url="https://a.com/x", domain="a.com", title="t", snippet="",
                     full_content="该公司2024年营收为1,234亿元，同比增长12.5%。" * 3)
        issues = claim_check.check_claims("营收1234亿元[来源1]。\n营收为1,234亿元[来源1]。", [src], "")
        self.assertEqual(issues, [])

    def test_legacy_write_ai_drops_memory_role_from_messages(self):
        captured = {}

        def fake_post(url, payload, headers=None, timeout=None):
            captured["payload"] = payload
            return {"choices": [{"message": {"content": "正文", "reasoning_content": ""}}]}

        history = [
            {"role": "memory", "content": "长期记忆"},
            {"role": "user", "content": "上一问", "ts": "x"},
            {"role": "assistant", "content": "上一答", "ts": "x"},
        ]
        with temporary_env(**required_env()):
            from tg_bot.pipeline import write as legacy_write
            with patch.object(legacy_write, "http_post", side_effect=fake_post):
                legacy_write.write_ai("问题", "", history_context=history, source_index=[])
        messages = captured["payload"]["messages"]
        self.assertNotIn("memory", {m["role"] for m in messages})
        self.assertTrue(all(set(m) <= {"role", "content"} for m in messages))


if __name__ == "__main__":
    unittest.main()
