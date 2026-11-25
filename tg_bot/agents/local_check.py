#!/usr/bin/env python3
"""agents/local_check.py — 规则核查（零成本、毫秒级）

模型核查（Critic）每次要把全部素材再读一遍，是搜索路径里最贵也最慢的环节之一。
实际上 Writer 最常见的硬错误是两类，用规则就能抓到：

1. 引用了不存在的来源编号（[来源9] 但只有 5 条素材）
2. 句子里的数字（年份、金额、百分比、数量）在素材里根本找不到

规则核查通过 → 普通问题直接放行，不再调用模型核查；
规则核查发现可疑 → 再交给模型 Critic 精查（高风险问题无论如何都走模型核查）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_CITE_RE = re.compile(r"\[来源(\d+)\]")
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?\n])")
_NUM_RE = re.compile(r"\d+(?:[.,，]\d+)*")


@dataclass
class LocalCheckResult:
    ok: bool
    issues: list[dict] = field(default_factory=list)
    checked_sentences: int = 0
    checked_numbers: int = 0

    @property
    def summary(self) -> str:
        if self.ok:
            return f"规则核查通过（{self.checked_sentences} 句 / {self.checked_numbers} 个数字）"
        return "；".join(i["reason"] for i in self.issues[:3])


def _norm_number(token: str) -> str:
    return token.replace(",", "").replace("，", "")


def _numbers_in(text: str) -> set[str]:
    return {_norm_number(t) for t in _NUM_RE.findall(text or "")}


def local_fact_check(reply: str, sources: list) -> LocalCheckResult:
    """对 Writer 草稿做规则核查。``sources`` 是 Source 列表（有 ``body`` 属性）。"""
    reply = reply or ""
    bodies = [getattr(s, "body", "") or "" for s in (sources or [])]
    all_numbers = set()
    per_source_numbers = []
    for body in bodies:
        nums = _numbers_in(body)
        per_source_numbers.append(nums)
        all_numbers |= nums

    issues: list[dict] = []
    checked_sentences = 0
    checked_numbers = 0
    citations_total = 0

    for raw in _SENT_SPLIT_RE.split(reply):
        sentence = raw.strip()
        if not sentence:
            continue
        refs = [int(n) for n in _CITE_RE.findall(sentence)]
        if not refs:
            continue
        citations_total += len(refs)
        checked_sentences += 1
        clean = _CITE_RE.sub("", sentence)

        bad_refs = [n for n in refs if n < 1 or n > len(bodies) or not bodies[n - 1].strip()]
        if bad_refs:
            issues.append({
                "sentence": clean[:120],
                "source_ref": f"来源{bad_refs[0]}",
                "severity": "HARD",
                "reason": f"引用了不存在的来源编号 {bad_refs}",
            })
            continue

        cited_numbers = set()
        for n in refs:
            cited_numbers |= per_source_numbers[n - 1]
        for token in _NUM_RE.findall(clean):
            value = _norm_number(token)
            # 单个数字（如“第1”“3 点”）太常见，误报多于价值，跳过
            if len(value.replace(".", "")) < 2:
                continue
            checked_numbers += 1
            if value in cited_numbers:
                continue
            severity = "SOFT" if value in all_numbers else "HARD"
            issues.append({
                "sentence": clean[:120],
                "source_ref": f"来源{refs[0]}",
                "severity": severity,
                "reason": (
                    f"数字 {token} 不在所引来源中，但出现在其他来源" if severity == "SOFT"
                    else f"数字 {token} 在所有素材里都找不到"
                ),
            })

    if bodies and any(b.strip() for b in bodies) and citations_total == 0 and len(reply) > 120:
        issues.append({
            "sentence": "",
            "source_ref": "",
            "severity": "SOFT",
            "reason": "正文没有任何来源标注，无法用规则核查",
        })

    return LocalCheckResult(
        ok=not issues,
        issues=issues,
        checked_sentences=checked_sentences,
        checked_numbers=checked_numbers,
    )
