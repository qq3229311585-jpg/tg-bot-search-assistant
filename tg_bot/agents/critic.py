#!/usr/bin/env python3
"""agents/critic.py — 审核 AI（只判断，不修改）

职责（只做这一件事）：
  输入：草稿文本 + sources
  输出：CriticReport（pass / patch / rewrite / unknown + 问题列表）

不做的事：
  - 不重写文本（那是 Writer 的事）
  - 不执行 patch（那是 Patcher 的事）

审核哲学：
  - 默认 PASS，只有找到明确错误才报告
  - SOFT = 措辞与原文不完全一致，但信息没错 → Patcher 改写
  - HARD = 数字/事实与原文矛盾，或完全无来源 → Patcher 删除
  - 不要因为"可能更好"就退回；只因为"明确错了"才退回
"""
from __future__ import annotations
import json
import logging
import re
from dataclasses import replace

from tg_bot.core.contracts import CriticReport, Issue, Source
from tg_bot.workers.source_utils import format_sources_for_writer

log = logging.getLogger(__name__)

_SYS_CRITIC = """\
你是一个事实核查员。

输入：
- 用户原始问题
- 编号的原文素材（[来源N]）
- 写作AI的草稿（其中每句具体事实后标注了 [来源N]）

任务：逐句检查标注了 [来源N] 的内容，确认是否真实出现在对应素材里。

输出严格为 JSON：
{
  "verdict": "pass" 或 "patch" 或 "rewrite",
  "issues": [
    {
      "sentence": "被检查的原句（完整引用）",
      "source_ref": "来源N",
      "severity": "SOFT" 或 "HARD",
      "reason": "一句话说明问题",
      "suggested_fix": "如何修改（SOFT时必填）",
      "evidence_excerpt": "原文中对应的片段（最多100字）"
    }
  ]
}

判断标准：
- SOFT：信息大体正确，但措辞夸大/缩小/换了说法，原文能找到近似依据
- HARD：素材里完全找不到该事实，或与素材明确矛盾（数字错、时间错等）
- 无问题的句子：完全不要出现在 issues 里

verdict 规则：
- issues 全空 → "pass"
- 有 SOFT 且 HARD 数量 ≤ 1 → "patch"（Patcher 做最小改动）
- HARD 数量 ≥ 2 或严重偏离 → "rewrite"（Writer 重写）

审核门槛（严格执行）：
- 没有 [来源N] 标注的句子一律跳过（那是过渡句）
- 素材支持同一个信息的多种表达 → PASS，不要因为措辞不同就 SOFT
- 直接 API 工具（天气/日历/流量）的结果本身就是权威，无需来源验证
"""


def _critique_once(
    reply: str,
    sources: list[Source],
    user_query: str = "",
    attempt: int = 0,
    *,
    shard_label: str = "",
) -> CriticReport:
    """
    对 reply 进行事实核查。
    返回 CriticReport（verdict 决定后续动作）。
    """
    import tg_bot.config as _cfg
    from tg_bot.tools.fetch import http_post
    from tg_bot.storage import save_verifier_thinking

    audit_identity = f"critic_audit[{shard_label or 'single'}]"

    if not (reply or "").strip():
        return CriticReport(verdict="rewrite", issues=[])

    sources_text = format_sources_for_writer(sources, max_body=2500)
    user_msg = (
        f"【用户原始问题】\n{user_query}\n\n"
        f"【原文素材】\n{sources_text}\n\n"
        f"【待审草稿】\n{reply}"
    )

    def _call():
        return http_post(
            f"{_cfg.DEEPSEEK_API_BASE}/chat/completions",
            {
                "model": _cfg.DEEPSEEK_MODEL_VERIFY,
                "messages": [
                    {"role": "system", "content": _SYS_CRITIC},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 3000,
                "temperature": 0.1,
                "thinking": {"type": "enabled", "budget_tokens": 800},
            },
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_VERIFY_KEY}"},
            timeout=60,
        )

    def _extract_json_text(raw: str) -> str:
        raw = (raw or "").strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        return m.group(0) if m else ""

    def _repair_json(raw: str) -> str:
        """Critic 输出非 JSON 时，只修格式，不重新核查。"""
        if not (raw or "").strip():
            return ""
        try:
            resp = http_post(
                f"{_cfg.DEEPSEEK_API_BASE}/chat/completions",
                {
                    "model": _cfg.DEEPSEEK_MODEL_VERIFY,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是 JSON 修复器。把用户给出的审核结果改成严格 JSON，"
                                "只能输出 JSON 对象，不要输出解释。格式必须是："
                                "{\"verdict\":\"pass|patch|rewrite\",\"issues\":[]}。"
                                "不要新增事实判断；如果原文没有明确问题，verdict=pass, issues=[]。"
                            ),
                        },
                        {"role": "user", "content": raw[:6000]},
                    ],
                    "max_tokens": 1800,
                    "temperature": 0,
                    "thinking": {"type": "disabled"},
                },
                headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_VERIFY_KEY}"},
                timeout=30,
            )
            if resp and resp.get("choices"):
                return _extract_json_text(
                    (resp["choices"][0]["message"].get("content") or "").strip()
                )
        except Exception as e:
            log.warning(f"Critic JSON 修复调用失败: {e}")
        return ""

    def _audit_summary(verdict: str, issues: list[Issue] | None = None, error_code: str = "") -> str:
        refs = sorted({issue.source_ref for issue in (issues or []) if issue.source_ref})
        parts = [f"verdict={verdict}", f"issues={len(issues or [])}"]
        if refs:
            parts.append("refs=" + ",".join(refs[:8]))
        if error_code:
            parts.append("error=" + error_code)
        if shard_label:
            parts.append("shard=" + shard_label)
        return ";".join(parts)

    def _format_error_report(kind: str) -> CriticReport:
        log.warning(f"Critic {kind}，JSON 修复失败，转为 unknown，不触发整篇重写")
        error_code = {
            "未返回 JSON": "format_missing_json",
            "JSON 解析失败": "format_invalid_json",
        }.get(kind, "format_error")
        save_verifier_thinking(
            audit_identity,
            f"format_error({kind})",
            _audit_summary("unknown", error_code=error_code),
            attempt,
        )
        return CriticReport(verdict="unknown", issues=[])

    def _parse(raw: str, repaired: bool = False) -> CriticReport:
        json_text = _extract_json_text(raw)
        if not json_text:
            if not repaired:
                fixed = _repair_json(raw)
                if fixed:
                    log.info("Critic 未返回 JSON，已通过 JSON 修复器恢复")
                    return _parse(fixed, repaired=True)
            return _format_error_report("未返回 JSON")
        try:
            data = json.loads(json_text)
        except Exception:
            if not repaired:
                fixed = _repair_json(raw)
                if fixed:
                    log.info("Critic JSON 解析失败，已通过 JSON 修复器恢复")
                    return _parse(fixed, repaired=True)
            return _format_error_report("JSON 解析失败")

        raw_issues = data.get("issues") or []
        issues = []
        for item in raw_issues:
            sev = str(item.get("severity", "")).upper()
            if sev not in ("SOFT", "HARD"):
                continue
            issues.append(Issue(
                sentence=str(item.get("sentence", "")).strip(),
                source_ref=str(item.get("source_ref", "")).strip(),
                severity=sev,
                reason=str(item.get("reason", "")).strip(),
                suggested_fix=str(item.get("suggested_fix", "")).strip(),
                evidence_excerpt=str(item.get("evidence_excerpt", "")).strip(),
            ))

        verdict = str(data.get("verdict", "pass")).lower()
        if verdict not in ("pass", "patch", "rewrite", "unknown"):
            verdict = "pass" if not issues else "patch"

        if issues:
            log.warning(f"❌ Critic 发现 {len(issues)} 个问题（verdict={verdict}）")
            save_verifier_thinking(audit_identity, verdict, _audit_summary(verdict, issues), attempt)
        else:
            log.info(f"✅ Critic 审核通过（attempt={attempt}）")
            save_verifier_thinking(audit_identity, "pass", _audit_summary("pass"), attempt)

        return CriticReport(verdict=verdict, issues=issues)

    try:
        resp = _call()
        if not resp or not resp.get("choices"):
            raise ValueError("空响应")
        msg = resp["choices"][0]["message"]
        raw = (msg.get("content") or "").strip()
        return _parse(raw)

    except Exception as e:
        log.warning(f"Critic 第1次失败: {e}，切换 key 重试")
        from tg_bot.config import _next_verify_key
        _next_verify_key()
        try:
            resp2 = _call()
            if resp2 and resp2.get("choices"):
                msg2 = resp2["choices"][0]["message"]
                return _parse(
                    (msg2.get("content") or "").strip(),
                )
        except Exception as e2:
            log.warning(f"Critic 重试也失败: {e2}，返回 unknown")
        save_verifier_thinking(
            audit_identity,
            "unknown",
            _audit_summary("unknown", error_code="retry_failed"),
            attempt,
        )
        return CriticReport(verdict="unknown")


def _single_review_report(report: CriticReport) -> CriticReport:
    """Attach additive audit fields without changing one-shot verdict semantics."""
    return replace(
        report,
        mode="single",
        chunks_total=1,
        chunks_passed=1 if report.verdict == "pass" else 0,
        chunks_unknown=1 if report.verdict == "unknown" else 0,
    )


def critique(
    reply: str,
    sources: list[Source],
    user_query: str = "",
    attempt: int = 0,
    *,
    shard_threshold: int = 1200,
    max_chunks: int = 6,
    chunk_evidence_chars: int = 6000,
    progress_cb=None,
    call_once=None,
) -> CriticReport:
    """Review short drafts once and long drafts in citation-scoped shards."""
    from tg_bot.review_shards import (
        aggregate_critic_reports,
        preflight_shard_issues,
        route_shard_sources,
        should_shard_review,
        split_review_text,
    )

    if not (reply or "").strip():
        return _single_review_report(CriticReport(verdict="rewrite", issues=[]))

    source_text_length = len(format_sources_for_writer(sources, max_body=2500))
    review_once = call_once or _critique_once
    if not should_shard_review(reply, source_text_length, shard_threshold):
        return _single_review_report(
            review_once(reply, sources, user_query=user_query, attempt=attempt)
        )

    shards = split_review_text(reply, threshold=shard_threshold, max_chunks=max_chunks)
    if len(shards) <= 1 and len(reply or "") <= shard_threshold and source_text_length <= 8000:
        return _single_review_report(
            review_once(reply, sources, user_query=user_query, attempt=attempt)
        )

    total = len(shards)

    def _notify(status: str, detail: str) -> None:
        if not progress_cb:
            return
        try:
            progress_cb("review_shard", status, detail)
        except Exception as exc:
            log.debug("review shard progress callback failed (non-fatal): %s", exc)

    def _review_one_shard(shard) -> CriticReport:
        routed_sources, routing_issues = route_shard_sources(
            shard, sources, evidence_budget=chunk_evidence_chars,
        )
        preflight_issues = (*preflight_shard_issues(shard), *routing_issues)
        if preflight_issues:
            # Invalid citations or missing routed evidence are code-verified HARD
            # failures; never ask the model to invent a judgement for that shard.
            return CriticReport(verdict="patch", issues=list(preflight_issues))
        return review_once(
            shard.text,
            routed_sources,
            user_query=user_query,
            attempt=attempt,
            shard_label=f"{shard.index}/{total}",
        )

    # 每段核查互相独立（各自路由证据、各自一次模型调用），并发执行；
    # 结果按段序合并，聚合结论与串行版本一致。进度回调按实际完成数上报。
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _notify("start", f"0/{total}")
    reports: list[CriticReport | None] = [None] * total
    completed = 0
    with ThreadPoolExecutor(
        max_workers=min(4, total), thread_name_prefix="review-shard"
    ) as pool:
        futures = {pool.submit(_review_one_shard, shard): i for i, shard in enumerate(shards)}
        for future in as_completed(futures):
            reports[futures[future]] = future.result()
            completed += 1
            _notify("done", f"{completed}/{total}")

    return aggregate_critic_reports([report for report in reports if report is not None])
