#!/usr/bin/env python3
"""agents/critic.py — 审核 AI（只判断，不修改）

职责（只做这一件事）：
  输入：草稿文本 + sources
  输出：CriticReport（pass / patch / rewrite / unknown + 问题列表）

不做的事：
  - 不重写文本（那是 Writer 的事）
  - 不执行 patch（那是 Patcher 的事）

核查两条腿走路：
  1. claim_check.check_claims 先做一遍确定性规则核查（引用越界、数字有没有来源、
     来源标注对不对、来源段有没有编造链接）——这些代码能算清楚，不问模型。
  2. 模型再做一遍语义级核查（措辞是否夸大、时效是否过期、素材是否矛盾等）。
  两边的问题合并去重，**最终 verdict 只由代码根据合并后的 issues 计算**，
  模型自己给的 verdict 只记日志，不参与决策。
"""
from __future__ import annotations
import json
import logging
import re
from datetime import datetime, timezone, timedelta

from tg_bot.core.contracts import CriticReport, Issue, Source
from tg_bot.workers.source_utils import format_sources_for_writer
from tg_bot.workers import claim_check

log = logging.getLogger(__name__)

_SYS_CRITIC = """\
你是一个事实核查员。

输入：
- 用户原始问题
- 编号的原文素材（[来源N]）
- 写作AI的草稿（其中每句具体事实后标注了 [来源N]）

任务：
- 标注了 [来源N] 的句子：对照所标来源核查数字/日期/结论是否一致。
- 没标 [来源N] 但包含具体事实（数字、日期、人名、机构、事件、排名、因果结论）的句子：
  对照全部素材核查，找不到支撑 → HARD。
- 纯过渡句、结论复述、礼貌语：跳过，不检查。

输出严格为 JSON：
{
  "verdict": "pass" 或 "patch" 或 "rewrite"，
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
（注：verdict 字段仅供参考记录用，实际是否 pass/patch/rewrite 由系统按 issues 数量和严重度用固定规则计算，不采用你给的 verdict。）

判断标准：
- SOFT：信息大体正确，但措辞夸大/缩小/换了说法，原文能找到近似依据
- HARD：素材里完全找不到该事实，或与素材明确矛盾（数字错、时间错等）
- 无问题的句子：完全不要出现在 issues 里

额外的 HARD 判断（时效性）：
- 草稿用"今天/最新/目前/刚刚"描述某件事，但所引素材的发布日期明显早于今天（超过7天），
  或素材根本没有日期却声称是"今天"发生的 → HARD，reason 写明时效不符。

额外的 SOFT 判断（素材矛盾）：
- 多条素材对同一件事给出不同说法，草稿只采用了其中一方且没有说明分歧 → SOFT，
  suggested_fix 要求写明"素材X说A，素材Y说B"两种说法都列出。

审核门槛（严格执行）：
- 素材支持同一个信息的多种表达 → PASS，不要因为措辞不同就 SOFT
- 直接 API 工具（天气/日历/流量）的结果本身就是权威，无需来源验证
"""


_HARD_REASON_RE = re.compile(r"(找不到|无来源|不存在|矛盾|编造|捏造|伪造|不符|冲突|无法核实)")


def _bj_today_line() -> str:
    now = datetime.now(timezone(timedelta(hours=8)))
    return f"【当前日期】今天是 {now.strftime('%Y年%m月%d日')}（北京时间）"


def _merge_issues(rule_issues: list[Issue], llm_issues: list[Issue]) -> list[Issue]:
    """按句子归一化去重合并，严重度取较高者，reason 保留两边。"""
    merged: list[Issue] = []
    used_llm = [False] * len(llm_issues)
    rule_norms = [claim_check.normalize_sentence(i.sentence) for i in rule_issues]
    llm_norms = [claim_check.normalize_sentence(i.sentence) for i in llm_issues]

    def _same(a: str, b: str) -> bool:
        if not a or not b:
            return False
        if a == b:
            return True
        return (a in b or b in a) and min(len(a), len(b)) >= 6

    for ri, rissue in enumerate(rule_issues):
        match_idx = None
        for li, lissue in enumerate(llm_issues):
            if used_llm[li]:
                continue
            if _same(rule_norms[ri], llm_norms[li]):
                match_idx = li
                break
        if match_idx is None:
            merged.append(rissue)
            continue
        used_llm[match_idx] = True
        lissue = llm_issues[match_idx]
        severity = "HARD" if "HARD" in (rissue.severity, lissue.severity) else "SOFT"
        reason = rissue.reason
        if lissue.reason and lissue.reason not in reason:
            reason = f"{reason}；模型另指出：{lissue.reason}"
        merged.append(Issue(
            sentence=rissue.sentence,
            source_ref=rissue.source_ref or lissue.source_ref,
            severity=severity,
            reason=reason,
            suggested_fix=rissue.suggested_fix or lissue.suggested_fix,
            evidence_excerpt=rissue.evidence_excerpt or lissue.evidence_excerpt,
            origin="rule",
        ))

    for li, lissue in enumerate(llm_issues):
        if not used_llm[li]:
            merged.append(lissue)

    return merged


def critique(
    reply: str,
    sources: list[Source],
    user_query: str = "",
    attempt: int = 0,
) -> CriticReport:
    """
    对 reply 进行事实核查。
    返回 CriticReport（verdict 由代码根据合并后的 issues 计算，不采用模型自报的 verdict）。
    """
    import tg_bot.config as _cfg
    from tg_bot.tools.fetch import http_post
    from tg_bot.storage import save_verifier_thinking

    if not (reply or "").strip():
        return CriticReport(verdict="rewrite", issues=[])

    rule_issues = claim_check.check_claims(reply, sources, user_query)
    rule_issue_count = len(rule_issues)

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
                    {"role": "system", "content": _SYS_CRITIC + "\n\n" + _bj_today_line()},
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

    def _fallback_report(kind: str, reasoning: str) -> CriticReport:
        """模型失败/JSON 修复也失败：有规则问题就按规则问题定 verdict，没有就 unknown。"""
        if rule_issues:
            verdict = claim_check.compute_verdict(rule_issues)
            log.warning(f"Critic {kind}，但规则核查发现 {rule_issue_count} 个问题，按规则结果继续")
            save_verifier_thinking(user_query, f"format_error({kind})_rule_fallback", reasoning, attempt)
            return CriticReport(
                verdict=verdict, issues=list(rule_issues),
                llm_failed=True, rule_issue_count=rule_issue_count,
            )
        log.warning(f"Critic {kind}，JSON 修复失败且无规则问题，转为 unknown，不触发整篇重写")
        save_verifier_thinking(user_query, f"format_error({kind})", reasoning, attempt)
        return CriticReport(verdict="unknown", issues=[], llm_failed=True, rule_issue_count=0)

    def _parse(raw: str, reasoning: str, repaired: bool = False) -> CriticReport:
        json_text = _extract_json_text(raw)
        if not json_text:
            if not repaired:
                fixed = _repair_json(raw)
                if fixed:
                    log.info("Critic 未返回 JSON，已通过 JSON 修复器恢复")
                    return _parse(fixed, reasoning, repaired=True)
            return _fallback_report("未返回 JSON", reasoning)
        try:
            data = json.loads(json_text)
        except Exception:
            if not repaired:
                fixed = _repair_json(raw)
                if fixed:
                    log.info("Critic JSON 解析失败，已通过 JSON 修复器恢复")
                    return _parse(fixed, reasoning, repaired=True)
            return _fallback_report("JSON 解析失败", reasoning)

        raw_issues = data.get("issues") or []
        llm_issues = []
        for item in raw_issues:
            sev = str(item.get("severity", "")).upper()
            reason = str(item.get("reason", "")).strip()
            if sev not in ("SOFT", "HARD"):
                # severity 缺失但 reason 明确说找不到/矛盾/编造 → 按 HARD 处理；否则当 SOFT
                sev = "HARD" if _HARD_REASON_RE.search(reason) else "SOFT"
            llm_issues.append(Issue(
                sentence=str(item.get("sentence", "")).strip(),
                source_ref=str(item.get("source_ref", "")).strip(),
                severity=sev,
                reason=reason,
                suggested_fix=str(item.get("suggested_fix", "")).strip(),
                evidence_excerpt=str(item.get("evidence_excerpt", "")).strip(),
                origin="llm",
            ))

        model_verdict = str(data.get("verdict", "pass")).lower()  # 仅记日志，不参与决策

        merged = _merge_issues(rule_issues, llm_issues)
        verdict = claim_check.compute_verdict(merged)

        if merged:
            log.warning(
                f"❌ Critic 发现 {len(merged)} 个问题（规则={rule_issue_count} 模型={len(llm_issues)} "
                f"verdict={verdict}，模型自报={model_verdict}）"
            )
            save_verifier_thinking(user_query, verdict, reasoning, attempt)
        else:
            log.info(f"✅ Critic 审核通过（attempt={attempt}）")
            save_verifier_thinking(user_query, "pass", reasoning, attempt)

        return CriticReport(
            verdict=verdict, issues=merged,
            llm_failed=False, rule_issue_count=rule_issue_count,
        )

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
            log.warning(f"Critic 重试也失败: {e2}")
        return _fallback_report("模型调用失败", f"critic_retry_failed: {e}")
