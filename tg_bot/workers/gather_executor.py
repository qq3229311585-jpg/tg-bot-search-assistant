#!/usr/bin/env python3
"""Tool executor for gather_ai.

This module executes one tool call and mutates the gather context with
standardized source_index / fetched_pages / failed_urls updates.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import logging

from tg_bot.workers.gather_tools import (
    build_cache_entries,
    build_fetch_entry,
    build_wikipedia_entry,
    parse_search_entries,
)
from tg_bot.workers.source_utils import FETCH_BLOCKED_DOMAINS

log = logging.getLogger(__name__)


def execute_search(query: str, search_type: str = "general") -> str:
    from tg_bot.tools.search import execute_search as _impl
    return _impl(query, search_type)


def execute_serper(query: str, search_type: str = "general") -> str:
    from tg_bot.tools.search import _execute_serper as _impl
    return _impl(query, search_type)


def execute_fetch_content(url: str) -> str:
    from tg_bot.tools.fetch import execute_fetch_content as _impl
    return _impl(url)


def execute_read_cache(ids, level: str = "snippet") -> str:
    from tg_bot.tools.fetch import execute_read_cache as _impl
    return _impl(ids, level)


def execute_wikipedia(query: str) -> str:
    from tg_bot.tools.native import execute_wikipedia as _impl
    return _impl(query)


def execute_weather() -> str:
    from tg_bot.tools.native import execute_weather as _impl
    return _impl()


def execute_vps_traffic() -> str:
    from tg_bot.tools.native import execute_vps_traffic as _impl
    return _impl()


def execute_github_trending(language: str = "") -> str:
    from tg_bot.tools.native import execute_github_trending as _impl
    return _impl(language=language)


def execute_api_balance() -> str:
    from tg_bot.tools.native import execute_api_balance as _impl
    return _impl()


def execute_calendar_query(days: int = 7, calendar_names=None) -> str:
    from tg_bot.tools.calendar_tool import execute_calendar_query as _impl
    return _impl(days=days, calendar_names=calendar_names)


def execute_calendar_add(**kwargs) -> str:
    from tg_bot.tools.calendar_tool import execute_calendar_add as _impl
    return _impl(**kwargs)


def execute_search_chat_history(keyword: str, limit: int = 20) -> str:
    from tg_bot.tools.native import execute_search_chat_history as _impl
    return _impl(keyword, limit)


@dataclass
class GatherExecContext:
    user_text: str
    source_index: list[dict]
    url_to_entry: dict[str, dict]
    meta: dict
    next_rid: Callable[[], str]
    persist: Callable[[dict], None]


def _domain_from_url(url: str) -> str:
    try:
        return (url or "").split("/")[2]
    except Exception:
        return ""


def _append_entry(ctx: GatherExecContext, entry: dict, *, persist: bool = True) -> None:
    ctx.source_index.append(entry)
    if entry.get("url"):
        ctx.url_to_entry[entry["url"]] = entry
    if persist:
        ctx.persist(entry)


def _execute_search_tool(fn: str, args: dict, ctx: GatherExecContext) -> str:
    q = args.get("query", "")
    stype = args.get("search_type", "general")
    if fn == "serper_search":
        try:
            result = execute_serper(q, stype)
        except Exception as e:
            result = f"Serper 调用失败: {e}"
            log.warning(f"serper_search 执行异常: {e}")
    else:
        result = execute_search(q, stype)

    for entry in parse_search_entries(
        result=result, query=q, tool=fn, next_rid=ctx.next_rid
    ):
        _append_entry(ctx, entry)
    return result


def _execute_fetch(args: dict, ctx: GatherExecContext) -> str:
    url = args.get("url", "")
    cached = ctx.url_to_entry.get(url)
    if cached and cached.get("full_content"):
        log.info(f"📄 fetch_content 缓存命中（跳过重复抓取）：{url[:60]}")
        return cached["full_content"]

    domain = _domain_from_url(url)
    if domain in FETCH_BLOCKED_DOMAINS:
        ctx.meta.setdefault("failed_urls", []).append(url)
        log.info(f"🚫 跳过封锁域名：{url[:60]}")
        return f"[已知封锁域名 {domain}，跳过抓取]"

    result = execute_fetch_content(url)
    if "正文来源" not in result:
        ctx.meta.setdefault("failed_urls", []).append(url)
        return result

    ctx.meta.setdefault("fetched_pages", []).append({"url": url, "content": result})
    matched = ctx.url_to_entry.get(url)
    if matched:
        matched["full_content"] = result
        return result

    entry = build_fetch_entry(url=url, result=result, next_rid=ctx.next_rid)
    if entry:
        _append_entry(ctx, entry)
    return result if entry else f"[页面为导航或空页，已跳过] {url[:60]}"


def _execute_wikipedia(args: dict, ctx: GatherExecContext) -> str:
    q = args.get("query", "")
    result = execute_wikipedia(q)
    _append_entry(ctx, build_wikipedia_entry(query=q, result=result, next_rid=ctx.next_rid))
    return result


def _execute_cache(args: dict, ctx: GatherExecContext) -> str:
    result = execute_read_cache(args.get("ids", []), args.get("level", "snippet"))
    try:
        seen_ids = {e.get("id") for e in ctx.source_index}
        for entry in build_cache_entries(
            result=result, next_rid=ctx.next_rid, existing_ids=seen_ids
        ):
            _append_entry(ctx, entry, persist=False)
            seen_ids.add(entry["id"])
    except Exception as e:
        log.debug(f"read_today_cache 来源索引解析失败: {e}")
    return result


def _execute_today_report(ctx: GatherExecContext) -> str:
    from tg_bot.storage import load_report

    result = load_report() or "今日午报尚未生成或为空。"
    if not any(e.get("id") == "LOCAL_TODAY_REPORT" for e in ctx.source_index):
        _append_entry(ctx, {
            "id": "LOCAL_TODAY_REPORT",
            "tool": "read_today_report",
            "query": ctx.user_text[:40],
            "title": "今日午报全文",
            "url": "local://read_today_report",
            "domain": "local://read_today_report",
            "snippet": result[:600],
            "full_content": result,
        }, persist=False)
    return result


def execute_gather_tool(fn: str, args: dict, ctx: GatherExecContext) -> str:
    """Execute one gather tool call and update ctx side effects."""
    if fn in ("web_search", "serper_search"):
        return _execute_search_tool(fn, args, ctx)
    if fn == "fetch_content":
        return _execute_fetch(args, ctx)
    if fn == "wikipedia_lookup":
        return _execute_wikipedia(args, ctx)
    if fn == "check_weather":
        return execute_weather()
    if fn == "vps_traffic":
        return execute_vps_traffic()
    if fn == "github_trending":
        return execute_github_trending(language=args.get("language", ""))
    if fn == "check_api_balance":
        return execute_api_balance()
    if fn == "calendar_query":
        return execute_calendar_query(
            days=int(args.get("days", 7)),
            calendar_names=args.get("calendar_names") or None,
        )
    if fn == "calendar_add":
        return execute_calendar_add(
            summary=args.get("summary", ""),
            start=args.get("start", ""),
            end=args.get("end", ""),
            calendar_name=args.get("calendar_name", "个人"),
            location=args.get("location", ""),
            description=args.get("description", ""),
        )
    if fn == "read_today_cache":
        return _execute_cache(args, ctx)
    if fn == "read_today_report":
        return _execute_today_report(ctx)
    if fn == "search_chat_history":
        return execute_search_chat_history(
            args.get("keyword", ""),
            int(args.get("limit", 20)),
        )
    if fn == "search_daily_summaries":
        from tg_bot.storage import search_daily_summaries
        return search_daily_summaries(args.get("keyword", ""))
    if fn == "read_daily_summary":
        from tg_bot.storage import read_daily_summary
        return read_daily_summary(args.get("date_str", ""))
    if fn == "read_daily_log":
        from tg_bot.storage import read_daily_log
        return read_daily_log(args.get("date_str", ""))
    return f"未知工具: {fn}"
