#!/usr/bin/env python3
"""User-visible reply cleanup helpers."""
from __future__ import annotations

import re

_SOURCE_MARK_RE = re.compile(r"\s*(?:\[来源\d+\]|【来源\d+】)")


def clean_reply_for_user(reply: str) -> str:
    """Remove internal source markers from ordinary user-facing replies."""
    text = _SOURCE_MARK_RE.sub("", reply or "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── A10：核查结论的用户可见说明 ────────────────────────────────────────────────
# 统一定义在一处，方便改文案；level 由 core.pipeline.run_search_pipeline 计算，
# "verified" 不加任何说明（核查干净通过，不需要额外提醒）。
VERIFICATION_NOTES: dict[str, str] = {
    "verified": "",
    "verified_with_removals": "（已删去 {removed_count} 处无法与资料核对的内容）",
    "rule_only": "（事实核查服务暂不可用，本条仅做了数字与来源的自动比对）",
    "partial": "（部分表述未能完全核实，请谨慎参考）",
    "insufficient": "（资料不完整，未确认的部分已在文中说明）",
}


def append_verification_note(reply: str, verification: dict | None) -> str:
    """在 clean_reply_for_user 之后追加一行核查说明（与 AI-no references 同风格）。"""
    verification = verification or {}
    level = str(verification.get("level") or "").strip()
    template = VERIFICATION_NOTES.get(level, "")
    if not template:
        return reply
    note = template.format(
        removed_count=verification.get("removed_count", 0),
        rule_issue_count=verification.get("rule_issue_count", 0),
        llm_issue_count=verification.get("llm_issue_count", 0),
    )
    reply = (reply or "").rstrip()
    if not reply or note in reply:
        return reply
    return reply + "\n\n" + note
