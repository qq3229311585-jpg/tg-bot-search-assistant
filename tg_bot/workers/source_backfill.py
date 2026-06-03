#!/usr/bin/env python3
"""Source index completion helpers for gather pipelines."""
from __future__ import annotations

from collections.abc import Callable, Iterable

from tg_bot.workers.gather_tools import parse_search_entries
from tg_bot.workers.source_utils import extract_wiki_title


_LATEST_ONLY_TOOLS = {
    "check_weather", "vps_traffic", "github_trending", "check_api_balance",
    "calendar_query", "calendar_add",
}


def _direct_api_entry(tool: str, query: str, snippet: str, next_rid: Callable[[], str]) -> dict:
    return {
        "id": f"AUTO_{tool}_{next_rid()}",
        "tool": tool,
        "query": query,
        "title": (query or tool)[:80],
        "url": "",
        "domain": tool,
        "snippet": snippet[:600],
        "full_content": snippet,
    }


def build_entries_from_tool_result(
    result: dict,
    next_rid: Callable[[], str],
    direct_api_tools: Iterable[str],
) -> list[dict]:
    """Convert one meta.tool_results item into source_index entries."""
    tool = result.get("tool", "")
    query = result.get("query", "")[:40]
    snippet = result.get("snippet", "") or ""
    if not snippet:
        return []

    if tool in ("web_search", "serper_search"):
        return parse_search_entries(
            result=snippet,
            query=query,
            tool=tool,
            next_rid=lambda: f"AUTO_{tool}_{next_rid()}",
        )
    if tool == "wikipedia_lookup":
        return [{
            "id": f"AUTO_{tool}_{next_rid()}",
            "tool": tool,
            "query": query,
            "title": extract_wiki_title(snippet, query or tool),
            "url": f"https://en.wikipedia.org/wiki/{(query or tool).replace(' ', '_')}",
            "domain": "wikipedia.org",
            "snippet": snippet[:600],
            "full_content": snippet,
        }]
    if tool in set(direct_api_tools):
        return [_direct_api_entry(tool, query, snippet, next_rid)]
    return []


def dedupe_source_index(source_index: list[dict]) -> list[dict]:
    """Deduplicate source entries while keeping the latest direct API entry."""
    seen_tools: dict[str, int] = {}
    seen_keys: set[str] = set()
    deduped: list[dict] = []

    for i, entry in enumerate(source_index):
        tool = entry.get("tool", "")
        if tool in _LATEST_ONLY_TOOLS or entry.get("domain", "").startswith("direct://"):
            seen_tools[tool or entry.get("domain", "")] = i

    for i, entry in enumerate(source_index):
        tool = entry.get("tool", "")
        tool_key = tool or entry.get("domain", "")
        if tool_key in seen_tools and seen_tools[tool_key] != i:
            continue
        key = entry.get("url") or (entry.get("snippet") or "")[:60]
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        deduped.append(entry)

    return deduped


def complete_source_index(
    source_index: list[dict],
    tool_results: list[dict],
    next_rid: Callable[[], str],
    direct_api_tools: Iterable[str],
) -> list[dict]:
    """Backfill missing entries from tool_results and return a deduped index."""
    completed = list(source_index or [])
    for result in tool_results or []:
        completed.extend(build_entries_from_tool_result(result, next_rid, direct_api_tools))

    if not completed and tool_results:
        for result in tool_results:
            snippet = (result.get("snippet") or "").strip()
            if not snippet:
                continue
            tool = result.get("tool", "")
            query = result.get("query", "")[:40]
            completed.append({
                "id": f"AUTO_RAW_{tool}_{next_rid()}",
                "tool": tool,
                "query": query,
                "title": (query or tool or "工具结果")[:80],
                "url": "",
                "domain": tool or "tool_results",
                "snippet": snippet[:600],
                "full_content": snippet,
            })

    return dedupe_source_index(completed)
