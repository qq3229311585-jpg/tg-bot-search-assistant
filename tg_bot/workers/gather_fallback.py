#!/usr/bin/env python3
"""Small gather completion/fallback helpers."""
from __future__ import annotations

import json
import re


_COMPLETION_REASON_CODES = {
    "direct_api_complete",
    "coverage_complete",
    "missing_requirements",
    "conflicting_evidence",
    "no_full_text",
    "no_novelty",
    "quota_exhausted",
    "invalid_audit",
}
_SUFFICIENT_REASON_CODES = {"direct_api_complete", "coverage_complete"}
_INSUFFICIENT_REASON_CODES = _COMPLETION_REASON_CODES - _SUFFICIENT_REASON_CODES - {"invalid_audit"}


def _completion_is_consistent(sufficient: object, reason_code: str) -> bool:
    return (
        sufficient is True and reason_code in _SUFFICIENT_REASON_CODES
    ) or (
        sufficient is False and reason_code in _INSUFFICIENT_REASON_CODES
    )


def parse_gather_completion(raw_out: str) -> dict:
    """Parse gather AI's final sufficiency JSON with conservative defaults."""
    parsed = {
        "sufficient": False,
        "reason": "",
        "reason_code": "invalid_audit",
        "suggested_length": "",
    }
    try:
        m_json = re.search(r"\{.*\}", raw_out or "", re.DOTALL)
        if not m_json:
            return parsed
        data = json.loads(m_json.group(0))
        if not isinstance(data.get("sufficient"), bool):
            return parsed
        parsed["sufficient"] = data["sufficient"]
        parsed["reason"] = str(data.get("reason", ""))
        code = str(data.get("reason_code", ""))
        parsed["reason_code"] = code if code in _COMPLETION_REASON_CODES else "invalid_audit"
        parsed["suggested_length"] = str(data.get("suggested_length", ""))
        if not _completion_is_consistent(parsed["sufficient"], parsed["reason_code"]):
            parsed["sufficient"] = False
            parsed["reason_code"] = "invalid_audit"
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return parsed


def finalize_gather_sources(source_index: list[dict], meta: dict, completion: dict) -> tuple[list[dict], dict]:
    """Attach final gather metadata and return source list + meta."""
    meta["source_index"] = source_index
    meta["gather_reason"] = completion.get("reason", "")
    reason_code = str(completion.get("reason_code", "invalid_audit"))
    if _completion_is_consistent(completion.get("sufficient"), reason_code):
        meta["gather_reason_code"] = reason_code
        meta["sufficient"] = completion["sufficient"]
    else:
        meta["gather_reason_code"] = "invalid_audit"
        meta["sufficient"] = False
    meta["suggested_length"] = completion.get("suggested_length", "")
    return source_index, meta


def finalize_round_limit(source_index: list[dict], meta: dict) -> tuple[list[dict], dict]:
    """Return collected sources when gather reaches its round limit."""
    meta["source_index"] = source_index
    meta["sufficient"] = False
    meta["gather_reason_code"] = "quota_exhausted"
    meta["gather_reason"] = f"采集轮次耗尽，共收集到 {len(source_index)} 条素材"
    return source_index, meta
