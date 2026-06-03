#!/usr/bin/env python3
"""Build a minimal facts_json artifact from source_index entries."""
from __future__ import annotations

import re

from tg_bot.workers.source_utils import compact_excerpt


def _clean_body_for_fact_excerpt(text: str) -> str:
    text = re.sub(r"^\[正文来源：[^\]]+\]\s*", "", text or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\[[^\]]{0,40}\]\(javascript:[^)]+\)", "", text, flags=re.I)
    text = re.sub(r"\[[^\]]{0,40}\]\(data:[^)]+\)", "", text, flags=re.I)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", text)
    return text.strip()


def build_minimal_facts_json(source_index: list[dict] | None) -> dict:
    """Create entry-level facts for audit/source follow-up without old fact_list."""
    facts = []
    for i, src in enumerate(source_index or [], start=1):
        body = _clean_body_for_fact_excerpt(src.get("full_content") or src.get("snippet") or "")
        query = " ".join([
            src.get("query") or "",
            src.get("title") or "",
        ]).strip()
        facts.append({
            "fact_id": f"F{i:03d}",
            "source_id": src.get("id") or f"S{i:03d}",
            "title": src.get("title") or "",
            "domain": src.get("domain") or "",
            "url": src.get("url") or "",
            "excerpt": compact_excerpt(body, query, 520),
            "tool": src.get("tool") or "",
        })
    return {
        "schema_version": 1,
        "fact_count": len(facts),
        "facts": facts,
    }
