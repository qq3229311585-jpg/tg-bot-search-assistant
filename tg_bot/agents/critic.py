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


def critique(
    reply: str,
    sources: list[Source],
    user_query: str = "",
    attempt: int = 0,
) -> CriticReport:
    """
    对 reply 进行事实核查。
    返回 CriticReport（verdict 决定后续动作）。
    """
    import tg_bot.config as _cfg
    from tg_bot.tools.fetch import http_post
    from tg_bot.storage import save_verifier_thinking

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
            "https://api.deepseek.com/chat/completions",
            {
                "model": "deepseek-v4-flash",
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
                "https://api.deepseek.com/chat/completions",
                {
                    "model": "deepseek-v4-flash",
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

    def _format_error_report(kind: str, reasoning: str) -> CriticReport:
        log.warning(f"Critic {kind}，JSON 修复失败，转为 unknown，不触发整篇重写")
        save_verifier_thinking(user_query, f"format_error({kind})", reasoning, attempt)
        return CriticReport(verdict="unknown", issues=[])

    def _parse(raw: str, reasoning: str, repaired: bool = False) -> CriticReport:
        json_text = _extract_json_text(raw)
        if not json_text:
            if not repaired:
                fixed = _repair_json(raw)
                if fixed:
                    log.info("Critic 未返回 JSON，已通过 JSON 修复器恢复")
                    return _parse(fixed, reasoning, repaired=True)
            return _format_error_report("未返回 JSON", reasoning)
        try:
            data = json.loads(json_text)
        except Exception:
            if not repaired:
                fixed = _repair_json(raw)
                if fixed:
                    log.info("Critic JSON 解析失败，已通过 JSON 修复器恢复")
                    return _parse(fixed, reasoning, repaired=True)
            return _format_error_report("JSON 解析失败", reasoning)

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
            save_verifier_thinking(user_query, verdict, reasoning, attempt)
        else:
            log.info(f"✅ Critic 审核通过（attempt={attempt}）")
            save_verifier_thinking(user_query, "pass", reasoning, attempt)

        return CriticReport(verdict=verdict, issues=issues)

    try:
        resp = _call()
        if not resp or not resp.get("choices"):
            raise ValueError("空响应")
        msg = resp["choices"][0]["message"]
        reasoning = (msg.get("reasoning_content") or "").strip()
        raw = (msg.get("content") or "").strip()
        return _parse(raw, reasoning)

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
                    (msg2.get("reasoning_content") or "").strip(),
                )
        except Exception as e2:
            log.warning(f"Critic 重试也失败: {e2}，返回 unknown")
        save_verifier_thinking(user_query, "unknown", f"critic_retry_failed: {e}", attempt)
        return CriticReport(verdict="unknown")
