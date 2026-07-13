#!/usr/bin/env python3
"""Build the daily news report without starting Telegram polling."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo


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


def _collect_one_category(category, execute_news_candidates):
    """Collect one event lane in isolation for bounded parallel execution."""
    query = _CATEGORY_QUERIES.get(category, f"{category} 今日 新闻")
    try:
        raw_items, provider_diagnostics = execute_news_candidates(query)
        found = [
            normalize_candidate(item, category, item.get("source", "news"))
            for item in raw_items
        ]
        diagnostics = [f"{category}: {item}" for item in provider_diagnostics]
        if not found and not provider_diagnostics:
            diagnostics.append(f"{category}: no_candidates")
        return found, diagnostics
    except Exception as exc:
        return [], [f"{category}: provider_error:{type(exc).__name__}"]


def collect_candidates(categories: Iterable[str] | None = None):
    """Collect news candidates with bounded concurrency and stable output order."""
    from tg_bot.config import DAILY_REPORT_MAX_WORKERS
    from tg_bot.tools.search import execute_news_candidates

    requested = tuple(categories or EVENT_SECTION_IDS)
    candidates = []
    diagnostics = []
    event_categories = [category for category in requested if category in _CATEGORY_QUERIES]
    results = {}
    worker_count = min(max(1, int(DAILY_REPORT_MAX_WORKERS)), max(1, len(event_categories)))
    if event_categories:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="daily-report") as pool:
            futures = {
                category: pool.submit(_collect_one_category, category, execute_news_candidates)
                for category in event_categories
            }
            # Read results in request order, not completion order, so report
            # ordering and diagnostics remain deterministic across runs.
            for category in event_categories:
                results[category] = futures[category].result()
    for category in requested:
        # Snapshot lanes are intentionally not sent through a news provider;
        # they are supplied by native/external collectors and preserved below.
        if category not in _CATEGORY_QUERIES:
            diagnostics.append(f"{category}: snapshot_or_no_news_collector")
            continue
        found, category_diagnostics = results[category]
        candidates.extend(found)
        diagnostics.extend(category_diagnostics)
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


def write_report_files(result, *, report_file, json_file, state_file, status_file=None, save_state=True):
    """Atomically write TXT/JSON/state after a successful collection."""
    from tg_bot.file_io import atomic_write_json, atomic_write_text
    from tg_bot.storage import save_daily_report_state

    payload = {
        "schema_version": 1,
        "generated_at": result["generated_at"],
        "content_sha256": _report_content_hash(result),
        "events": [_serialize_event(event) for event in result["selected"]],
        "sections": list(result.get("sections") or []),
        "preserved_sections": list(result.get("preserved_sections") or []),
        "repeated_sections": list(result.get("repeated_sections") or []),
        "suppressed_sections": list(result.get("suppressed_sections") or []),
        "diagnostics": list(result.get("diagnostics") or []),
    }
    atomic_write_json(json_file, payload)
    atomic_write_text(report_file, result["report_text"])
    if save_state:
        save_daily_report_state(result["state"], state_file)
    if status_file:
        write_report_status(status_file, result)


def _report_content_hash(result):
    """Hash report meaning, excluding the generated timestamp."""
    report_text = str(result.get("report_text") or "")
    report_text = re.sub(r"^(📰 今日热点日报 · )[^\r\n]+", r"\1<timestamp>", report_text, count=1)
    canonical = {
        "report_text": report_text,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_report_status(status_file, result, *, status="fresh", extra=None):
    """Write the machine-readable report status without touching report files."""
    from tg_bot.file_io import atomic_write_json

    payload = {
        "schema_version": 1,
        "status": status,
        "generated_at": result["generated_at"],
        "content_sha256": _report_content_hash(result),
        "event_count": len(result["selected"]),
        "sections": list(result.get("sections") or []),
        "repeated_sections": list(result.get("repeated_sections") or []),
        "suppressed_sections": list(result.get("suppressed_sections") or []),
        "diagnostics": list(result.get("diagnostics") or []),
    }
    if extra:
        payload.update(extra)
    atomic_write_json(status_file, payload)


def _load_report_status(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _same_report_day(left, right, timezone_name):
    try:
        zone = ZoneInfo(timezone_name)
        left_dt = _parse_timestamp(left)
        right_dt = _parse_timestamp(right)
        return bool(left_dt and right_dt and left_dt.astimezone(zone).date() == right_dt.astimezone(zone).date())
    except Exception:
        return False


@contextmanager
def _report_push_lock(status_file):
    """Serialize report/status writes and push decisions across invocations."""
    lock_path = f"{status_file}.push.lock"
    try:
        import fcntl
    except ImportError:  # pragma: no cover - deployment target is Unix
        yield
        return
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the report without writing runtime files")
    parser.add_argument("--push", action="store_true", help="send the report to ALLOWED_CHAT when content changes")
    args = parser.parse_args(argv)

    import importlib
    config = importlib.import_module("tg_bot.config")
    load_daily_report_state = importlib.import_module("tg_bot.storage").load_daily_report_state
    status_before_collection = _load_report_status(config.DAILY_REPORT_STATUS_FILE)

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
                with _report_push_lock(config.DAILY_REPORT_STATUS_FILE):
                    current_status = _load_report_status(config.DAILY_REPORT_STATUS_FILE)
                    if current_status == status_before_collection:
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
    push_requested = bool(args.push or config.DAILY_REPORT_PUSH)
    lock_context = _report_push_lock(config.DAILY_REPORT_STATUS_FILE)
    with lock_context:
        previous_status = _load_report_status(config.DAILY_REPORT_STATUS_FILE)
        content_hash = _report_content_hash(result)
        push_state = {"enabled": push_requested, "status": "disabled"}
        if push_requested:
            previous_push = previous_status.get("push")
            previous_push_status = previous_push.get("status") if isinstance(previous_push, dict) else None
            if previous_status.get("content_sha256") == content_hash and previous_push_status in {"sent", "skipped_unchanged"}:
                push_state["status"] = "skipped_unchanged"
            else:
                push_state["status"] = "pending"
            previous_push_sent = previous_push_status in {"sent", "skipped_unchanged"}
            if (
                previous_push_sent
                and not result.get("selected")
                and _same_report_day(previous_status.get("generated_at"), result.get("generated_at"), config.DAILY_REPORT_TIMEZONE)
            ):
                # A same-day rerun can suppress the already-published events
                # through the cooldown state and render a misleading empty
                # placeholder. Keep the last sent payload byte-for-byte.
                for item in result.get("diagnostics") or []:
                    print(f"WARNING {item}")
                from tg_bot.file_io import atomic_write_json
                status_payload = dict(previous_status)
                push_payload = dict(previous_push) if isinstance(previous_push, dict) else {}
                push_payload.update({"enabled": True, "status": "skipped_unchanged"})
                status_payload["push"] = push_payload
                atomic_write_json(config.DAILY_REPORT_STATUS_FILE, status_payload)
                print("OK daily report unchanged; push skipped")
                return 0
        defer_state = push_state["status"] == "pending"
        write_report_files(
            result,
            report_file=config.REPORT_FILE,
            json_file=config.DAILY_REPORT_JSON_FILE,
            state_file=config.DAILY_REPORT_STATE_FILE,
            status_file=config.DAILY_REPORT_STATUS_FILE,
            save_state=not defer_state,
        )
        if push_state["status"] == "pending":
            try:
                from tg_bot.bot_utils import send
                delivered = bool(send(config.ALLOWED_CHAT, result["report_text"]))
                push_state["status"] = "sent" if delivered else "failed"
            except Exception as exc:
                push_state["status"] = "failed"
                push_state["error"] = type(exc).__name__
            if push_state["status"] == "sent":
                from tg_bot.storage import save_daily_report_state
                save_daily_report_state(result["state"], config.DAILY_REPORT_STATE_FILE)
        write_report_status(
            config.DAILY_REPORT_STATUS_FILE,
            result,
            extra={"push": push_state},
        )
        if push_state["status"] == "failed":
            print("ERROR daily_report_push_failed", file=sys.stderr)
            return 1
    for item in diagnostics:
        print(f"WARNING {item}")
    print(f"OK daily report generated; events={len(result['selected'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
