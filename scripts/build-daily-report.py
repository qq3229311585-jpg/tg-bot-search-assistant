#!/usr/bin/env python3
"""Build the daily news report without starting Telegram polling."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import re
import sys
from typing import Iterable


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tg_bot.daily_report import (  # noqa: E402
    NewsCandidate,
    cluster_candidates,
    normalize_candidate,
    render_daily_report,
    select_events,
)


_CATEGORY_QUERIES = {
    "china": "中国 今日 重大新闻",
    "global": "全球 今日 重大新闻",
    "ai_tech": "AI 人工智能 技术 今日 新闻",
}


def _parse_search_output(result: str, category: str, source: str) -> list[NewsCandidate]:
    """Parse the plain-text format returned by existing search providers."""
    candidates = []
    for block in re.split(r"\n\s*\n", result or ""):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        title = lines[0].lstrip("•-").strip()
        url_index = next((index for index, line in enumerate(lines) if line.startswith("http")), None)
        if url_index is None or not title:
            continue
        url = lines[url_index].split()[0]
        summary = " ".join(lines[1:url_index]).strip()
        date_match = re.search(r"\[(\d{4}[^\]]*)\]", title)
        published = date_match.group(1) if date_match else None
        title = re.sub(r"\s*\[[^\]]+\]\s*$", "", title).strip()
        candidates.append(normalize_candidate({
            "title": title,
            "summary": summary,
            "url": url,
            "published_date": published,
        }, category, source))
    return candidates


def collect_candidates(categories: Iterable[str] | None = None):
    """Collect news candidates and return ``(candidates, diagnostics)``."""
    from tg_bot.tools.search import execute_news_candidates

    candidates = []
    diagnostics = []
    for category in categories or _CATEGORY_QUERIES:
        query = _CATEGORY_QUERIES.get(category, f"{category} 今日 新闻")
        try:
            raw_items, provider_diagnostics = execute_news_candidates(query)
            found = [
                normalize_candidate(item, category, item.get("source", "news"))
                for item in raw_items
            ]
            if found:
                candidates.extend(found)
            diagnostics.extend(f"{category}: {item}" for item in provider_diagnostics)
            if not found and not provider_diagnostics:
                diagnostics.append(f"{category}: no_candidates")
        except Exception as exc:
            diagnostics.append(f"{category}: provider_error:{type(exc).__name__}")
    return candidates, diagnostics


def _serialize_event(event):
    return {
        "event_id": event.event_id,
        "category": event.category,
        "title": event.title,
        "summary": event.summary,
        "status": event.status,
        "heat_score": event.heat_score,
        "heat_basis": list(event.heat_basis),
        "sources": [
            {
                "title": item.title,
                "summary": item.summary,
                "url": item.url,
                "domain": item.domain,
                "published_at": item.published_at,
                "relevance": item.relevance,
                "explicit_heat": item.explicit_heat,
                "source": item.source,
            }
            for item in event.sources
        ],
    }


def _parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _prune_state(state, now, retention_days):
    cutoff = now.astimezone(timezone.utc) - timedelta(days=max(1, int(retention_days)))
    kept = {}
    for event_id, record in state.get("events", {}).items():
        last = _parse_timestamp((record or {}).get("last_published"))
        if last is None or last.astimezone(timezone.utc) >= cutoff:
            kept[event_id] = record
    state["events"] = kept


def build_report(candidates, *, now=None, state=None, per_category=4, cooldown_days=14, timezone_name="Asia/Shanghai"):
    """Cluster, select, render, and return a serializable report result."""
    now = now or datetime.now(timezone.utc)
    current_state = dict(state or {})
    current_state["schema_version"] = 1
    current_state["events"] = dict(current_state.get("events") or {})
    _prune_state(current_state, now, max(cooldown_days, 14))
    events = cluster_candidates(candidates)
    selected = select_events(
        events,
        current_state,
        now=now,
        per_category=per_category,
        cooldown_days=cooldown_days,
    )
    for event in selected:
        previous = current_state["events"].get(event.event_id, {})
        current_state["events"][event.event_id] = {
            "last_published": now.isoformat(),
            "first_seen": previous.get("first_seen", now.isoformat()),
            "category": event.category,
            "title": event.title,
            "summary": event.summary,
            "sources": sorted({item.domain for item in event.sources if item.domain}),
            "heat_score": event.heat_score,
        }
    return {
        "generated_at": now.isoformat(),
        "selected": selected,
        "events": events,
        "state": current_state,
        "report_text": render_daily_report(selected, now, timezone_name=timezone_name),
        "diagnostics": [],
    }


def write_report_files(result, *, report_file, json_file, state_file, status_file=None):
    """Atomically write TXT/JSON/state after a successful collection."""
    from tg_bot.file_io import atomic_write_json, atomic_write_text
    from tg_bot.storage import save_daily_report_state

    payload = {
        "schema_version": 1,
        "generated_at": result["generated_at"],
        "events": [_serialize_event(event) for event in result["selected"]],
        "diagnostics": list(result.get("diagnostics") or []),
    }
    atomic_write_json(json_file, payload)
    atomic_write_text(report_file, result["report_text"])
    save_daily_report_state(result["state"], state_file)
    if status_file:
        atomic_write_json(status_file, {
            "schema_version": 1,
            "status": "fresh",
            "generated_at": result["generated_at"],
            "event_count": len(result["selected"]),
            "diagnostics": list(result.get("diagnostics") or []),
        })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the report without writing runtime files")
    args = parser.parse_args(argv)

    import importlib
    config = importlib.import_module("tg_bot.config")
    load_daily_report_state = importlib.import_module("tg_bot.storage").load_daily_report_state

    candidates, diagnostics = collect_candidates(config.DAILY_REPORT_CATEGORIES)
    if not candidates:
        from tg_bot.file_io import atomic_write_json
        for item in diagnostics:
            print(f"ERROR {item}", file=sys.stderr)
        try:
            atomic_write_json(config.DAILY_REPORT_STATUS_FILE, {
                "schema_version": 1,
                "status": "stale_previous",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "diagnostics": diagnostics,
            })
        except Exception as exc:
            print(f"ERROR daily_report_status_write: {type(exc).__name__}", file=sys.stderr)
        print("ERROR daily_report_no_candidates: previous report was kept", file=sys.stderr)
        return 1

    result = build_report(
        candidates,
        state=load_daily_report_state(config.DAILY_REPORT_STATE_FILE),
        per_category=config.DAILY_REPORT_ITEMS_PER_CATEGORY,
        cooldown_days=config.DAILY_REPORT_COOLDOWN_DAYS,
        timezone_name=config.DAILY_REPORT_TIMEZONE,
    )
    result["diagnostics"] = diagnostics
    if args.dry_run:
        print(result["report_text"])
        return 0
    write_report_files(
        result,
        report_file=config.REPORT_FILE,
        json_file=config.DAILY_REPORT_JSON_FILE,
        state_file=config.DAILY_REPORT_STATE_FILE,
        status_file=config.DAILY_REPORT_STATUS_FILE,
    )
    for item in diagnostics:
        print(f"WARNING {item}")
    print(f"OK daily report generated; events={len(result['selected'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
