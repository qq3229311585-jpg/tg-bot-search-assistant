#!/usr/bin/env python3
"""Build the daily news report without starting Telegram polling."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import os
import re
import sys
from typing import Iterable, Mapping


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
from tg_bot.report_sections import (  # noqa: E402
    DEFAULT_REPORT_SECTIONS,
    EVENT_SECTION_IDS,
    get_section_collector,
    register_section_collector,
    resolve_sections,
    split_external_sections,
)


_CATEGORY_QUERIES = {
    section.id: section.query
    for section in DEFAULT_REPORT_SECTIONS
    if section.kind == "event" and section.query
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
    requested = tuple(categories or EVENT_SECTION_IDS)
    for category in requested:
        # Snapshot lanes are intentionally not sent through a news provider;
        # they are supplied by native/external collectors and preserved below.
        if category not in _CATEGORY_QUERIES:
            diagnostics.append(f"{category}: snapshot_or_no_news_collector")
            continue
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


def _has_provider_failure(diagnostics: Iterable[str]) -> bool:
    """Return true only for failures that make a fresh event report unsafe."""
    return any(
        "provider_error" in str(item) or "unavailable" in str(item)
        for item in diagnostics
    )


def _has_fresh_event(events, now):
    """Mirror the report freshness window to explain cooldown placeholders."""
    for event in events:
        published = [
            _parse_timestamp(item.published_at)
            for item in event.sources
            if item.published_at
        ]
        if not published:
            return True
        latest = max(published)
        age_hours = (now.astimezone(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds() / 3600
        if age_hours <= 24:
            return True
    return False


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
        if not isinstance(record, Mapping):
            continue
        last = _parse_timestamp(record.get("last_published"))
        if last is not None and last.astimezone(timezone.utc) >= cutoff:
            kept[event_id] = record
    state["events"] = kept


def load_legacy_report(path):
    """Read the previous report for external/legacy section preservation."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except (FileNotFoundError, OSError):
        return ""


def register_builtin_snapshot_collectors(enabled=True):
    """Install lazy native adapters without importing/networking at module load."""
    if not enabled:
        return

    def weather_collector():
        from tg_bot.tools.native import execute_weather
        return execute_weather()

    register_section_collector("weather", weather_collector)


def collect_snapshot_sections(section_specs, legacy_report_text, diagnostics=None):
    """Merge registered snapshot collectors over legacy section text."""
    sections = split_external_sections(legacy_report_text, section_specs)
    diagnostics = diagnostics if diagnostics is not None else []
    for section in section_specs:
        if section.kind != "snapshot":
            continue
        collector = get_section_collector(section.id)
        if collector is None:
            continue
        try:
            raw = str(collector() or "").strip()
        except Exception as exc:
            diagnostics.append(f"{section.id}: collector_error:{type(exc).__name__}")
            continue
        if not raw or raw.startswith("天气查询失败"):
            diagnostics.append(f"{section.id}: collector_empty")
            continue
        sections[section.id] = f"【{section.title}】\n{raw}"
    return sections


def _record_selected_events(current_state, selected, now):
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


def build_report(
    candidates,
    *,
    now=None,
    state=None,
    per_category=None,
    cooldown_days=None,
    timezone_name="Asia/Shanghai",
    section_specs=None,
    legacy_report_text="",
    preserved_sections=None,
):
    """Cluster, select, render, and return a serializable report result.

    ``section_specs`` enables the full report registry. Leaving it unset keeps
    the original three-category API and output contract for older callers.
    """
    now = now or datetime.now(timezone.utc)
    default_per_category = 4 if per_category is None else int(per_category)
    default_cooldown_days = 14 if cooldown_days is None else int(cooldown_days)
    resolved_specs = None
    if section_specs is not None:
        resolved_specs = resolve_sections(getattr(item, "id", item) for item in section_specs)
    retention_days = default_cooldown_days
    if resolved_specs:
        retention_days = max(
            retention_days,
            *(int(section.cooldown_days) for section in resolved_specs if section.kind == "event"),
        )
    current_state = dict(state or {})
    current_state["schema_version"] = 1
    current_state["events"] = dict(current_state.get("events") or {})
    _prune_state(current_state, now, max(retention_days, 14))

    if section_specs is not None:
        specs = resolved_specs
        selected = []
        repeated_sections = set()
        suppressed_sections = set()
        all_events = []
        for section in specs:
            if section.kind != "event":
                continue
            section_candidates = [item for item in candidates if item.category == section.id]
            events = cluster_candidates(section_candidates)
            all_events.extend(events)
            chosen = select_events(
                events,
                current_state,
                now=now,
                per_category=default_per_category if per_category is not None else section.items,
                cooldown_days=default_cooldown_days if cooldown_days is not None else section.cooldown_days,
                strict_category=True,
            )
            selected.extend(chosen)
            if section_candidates and not chosen:
                suppressed_sections.add(section.id)
                if _has_fresh_event(events, now):
                    repeated_sections.add(section.id)
        _record_selected_events(current_state, selected, now)
        preserved = dict(
            split_external_sections(legacy_report_text, specs)
            if preserved_sections is None else preserved_sections
        )
        report_text = render_daily_report(
            selected,
            now,
            timezone_name=timezone_name,
            section_specs=specs,
            preserved_sections=preserved,
            repeated_sections=repeated_sections,
            suppressed_sections=suppressed_sections,
        )
        return {
            "generated_at": now.isoformat(),
            "selected": selected,
            "events": all_events,
            "state": current_state,
            "report_text": report_text,
            "diagnostics": [],
            "sections": [section.id for section in specs],
            "preserved_sections": sorted(preserved),
            "repeated_sections": sorted(repeated_sections),
            "suppressed_sections": sorted(suppressed_sections),
        }

    events = cluster_candidates(candidates)
    selected = select_events(
        events,
        current_state,
        now=now,
        per_category=default_per_category,
        cooldown_days=default_cooldown_days,
    )
    _record_selected_events(current_state, selected, now)
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
        "sections": list(result.get("sections") or []),
        "preserved_sections": list(result.get("preserved_sections") or []),
        "repeated_sections": list(result.get("repeated_sections") or []),
        "suppressed_sections": list(result.get("suppressed_sections") or []),
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
            "sections": list(result.get("sections") or []),
            "repeated_sections": list(result.get("repeated_sections") or []),
            "suppressed_sections": list(result.get("suppressed_sections") or []),
            "diagnostics": list(result.get("diagnostics") or []),
        })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the report without writing runtime files")
    args = parser.parse_args(argv)

    import importlib
    config = importlib.import_module("tg_bot.config")
    load_daily_report_state = importlib.import_module("tg_bot.storage").load_daily_report_state

    section_specs = resolve_sections(config.DAILY_REPORT_SECTIONS)
    if not config.DAILY_REPORT_SECTIONS_EXPLICIT and config.DAILY_REPORT_CATEGORIES_EXPLICIT:
        legacy_categories = set(config.DAILY_REPORT_CATEGORIES)
        section_specs = tuple(
            section for section in section_specs
            if section.kind == "snapshot" or section.id in legacy_categories
        )
    section_specs = tuple(
        replace(
            section,
            items=config.DAILY_REPORT_ITEMS_PER_SECTION,
            cooldown_days=config.DAILY_REPORT_EVENT_COOLDOWN_DAYS,
        )
        for section in section_specs
    )
    event_sections = [section.id for section in section_specs if section.kind == "event"]
    candidates, diagnostics = collect_candidates(event_sections)
    if not candidates and _has_provider_failure(diagnostics):
        from tg_bot.file_io import atomic_write_json
        for item in diagnostics:
            print(f"ERROR {item}", file=sys.stderr)
        if not args.dry_run:
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

    legacy_report_text = load_legacy_report(config.REPORT_FILE)
    register_builtin_snapshot_collectors(config.DAILY_REPORT_NATIVE_SNAPSHOTS)
    snapshot_diagnostics = []
    preserved_sections = collect_snapshot_sections(section_specs, legacy_report_text, snapshot_diagnostics)
    diagnostics.extend(snapshot_diagnostics)
    result = build_report(
        candidates,
        state=load_daily_report_state(config.DAILY_REPORT_STATE_FILE),
        timezone_name=config.DAILY_REPORT_TIMEZONE,
        section_specs=section_specs,
        legacy_report_text=legacy_report_text,
        preserved_sections=preserved_sections,
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
