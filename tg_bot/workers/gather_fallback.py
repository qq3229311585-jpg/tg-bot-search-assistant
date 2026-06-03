#!/usr/bin/env python3
"""Small gather completion/fallback helpers."""
from __future__ import annotations

import json
import re


def parse_gather_completion(raw_out: str) -> dict:
    """Parse gather AI's final sufficiency JSON with conservative defaults."""
    parsed = {
        "sufficient": True,
        "reason": "",
        "suggested_length": "",
    }
    try:
        m_json = re.search(r"\{.*\}", raw_out or "", re.DOTALL)
        if not m_json:
            return parsed
        data = json.loads(m_json.group(0))
        parsed["sufficient"] = bool(data.get("sufficient", True))
        parsed["reason"] = str(data.get("reason", ""))
        parsed["suggested_length"] = str(data.get("suggested_length", ""))
    except Exception:
        pass
    return parsed


def finalize_gather_sources(source_index: list[dict], meta: dict, completion: dict) -> tuple[list[dict], dict]:
    """Attach final gather metadata and return source list + meta."""
    meta["source_index"] = source_index
    meta["sufficient"] = bool(completion.get("sufficient", True))
    meta["gather_reason"] = completion.get("reason", "")
    meta["suggested_length"] = completion.get("suggested_length", "")
    return source_index, meta


def finalize_round_limit(source_index: list[dict], meta: dict) -> tuple[list[dict], dict]:
    """Return collected sources when gather reaches its round limit."""
    meta["source_index"] = source_index
    meta["sufficient"] = bool(source_index)
    meta["gather_reason"] = f"采集轮次耗尽，共收集到 {len(source_index)} 条素材"
    return source_index, meta
