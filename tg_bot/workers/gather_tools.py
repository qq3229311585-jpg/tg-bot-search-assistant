#!/usr/bin/env python3
"""Tool-result helpers for gather_ai.

These helpers do not decide which tool to call. They only turn raw tool
outputs into normalized source_index entries.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone, timedelta

from tg_bot.workers.source_utils import (
    extract_wiki_title,
    is_nav_or_empty,
    source_matches_query,
)

log = logging.getLogger(__name__)

_DATE_TAG_RE = re.compile(r'\[([^\[\]]{1,40})\]\s*$')
_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}')
_RELATIVE_AGE_RE = re.compile(
    r'(\d+)\s*(second|minute|hour|day|week|month|year)s?\s*ago', re.I
)
_RELATIVE_DELTA = {
    "second": lambda n: timedelta(seconds=n),
    "minute": lambda n: timedelta(minutes=n),
    "hour": lambda n: timedelta(hours=n),
    "day": lambda n: timedelta(days=n),
    "week": lambda n: timedelta(weeks=n),
    "month": lambda n: timedelta(days=n * 30),
    "year": lambda n: timedelta(days=n * 365),
}


def _bj_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def parse_date_tag(raw: str) -> str:
    """把标题末尾的 [YYYY-MM-DD] / [x days ago] / [2 hours ago] 转成绝对日期。

    解析不出来时返回空串（不是所有素材都带日期）。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    m_iso = _ISO_DATE_RE.match(raw)
    if m_iso:
        return m_iso.group(0)
    m_rel = _RELATIVE_AGE_RE.search(raw)
    if m_rel:
        n = int(m_rel.group(1))
        unit = m_rel.group(2).lower()
        delta_fn = _RELATIVE_DELTA.get(unit)
        if delta_fn:
            dt = _bj_now() - delta_fn(n)
            return dt.strftime("%Y-%m-%d")
    return ""


def _domain_from_url(url: str) -> str:
    try:
        return (url or "").split("/")[2]
    except Exception:
        return url or ""


_BAD_FETCH_TITLE_RE = re.compile(
    r"(gift\s*cards?|recommended by|buy a gift|logo|search icon|close search|"
    r"facebook|instagram|pinterest|newsletter|subscribe|sign in|log in|menu|"
    r"navigation|jump to|skip to|classpop\s*$|magazine\s*$|lifestyle\s*$)",
    re.I,
)


def _fallback_title_from_url(url: str) -> str:
    domain = _domain_from_url(url)
    path = (url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/").split("/")[-1]
    path = re.sub(r"[-_]+", " ", path).strip()
    return (path or domain or url or "")[:80]


def _clean_fetch_title_line(line: str) -> str:
    line = (line or "").strip()
    line = re.sub(r"^#{1,6}\s+", "", line).strip()
    line = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", line)
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
    line = re.sub(r'^[^\u4e00-\u9fffA-Za-z0-9（【《"\'“”‘’<]+', '', line).strip()
    return line


def _is_good_fetch_title(line: str) -> bool:
    line = (line or "").strip()
    if len(line) <= 3 or line.startswith("http"):
        return False
    if _BAD_FETCH_TITLE_RE.search(line):
        return False
    if re.fullmatch(r"[\W_]+", line):
        return False
    return True


def parse_search_entries(
    *,
    result: str,
    query: str,
    tool: str,
    next_rid: Callable[[], str],
) -> list[dict]:
    """Parse web_search/serper_search output blocks into source entries."""
    entries = []
    for block in (result or "").split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) < 2 or not lines[-1].startswith("http"):
            continue
        url = lines[-1].strip()
        domain = _domain_from_url(url)
        raw_title_line = lines[0].lstrip("• ").strip()
        date_match = _DATE_TAG_RE.search(raw_title_line)
        published_at = parse_date_tag(date_match.group(1)) if date_match else ""
        title_line = (_DATE_TAG_RE.sub('', raw_title_line).strip() or raw_title_line)[:80]
        snippet = " ".join(l.strip() for l in lines[1:-1])[:300]
        if not source_matches_query(query, title_line, snippet, domain, url=url):
            log.info(f"🧹 过滤无关搜索源：{domain} | {title_line[:40]}")
            continue
        entries.append({
            "id": next_rid(),
            "tool": tool,
            "query": query[:40],
            "title": title_line,
            "url": url,
            "domain": domain,
            "snippet": snippet,
            "full_content": None,
            "published_at": published_at,
        })
    return entries


def extract_fetch_title(result: str, url: str) -> str:
    """Extract a readable title from fetch_content body."""
    clean = re.sub(r'^\[正文来源：[^\]]+\]\s*', '', result or "")

    # Markdown headings usually preserve the page title better than the first
    # visible line, which is often navigation or an ad banner.
    for line in clean.split("\n"):
        if not re.match(r"^\s*#{1,3}\s+", line or ""):
            continue
        title = _clean_fetch_title_line(line)
        if _is_good_fetch_title(title):
            return title[:80]

    clean_no_media = re.sub(r'!\[.*?\]\(.*?\)\s*', '', clean)
    clean_no_media = re.sub(r'\[([^\]]+)\]\(.*?\)\s*', r'\1', clean_no_media)
    for line in clean_no_media.split("\n"):
        title = _clean_fetch_title_line(line)
        if _is_good_fetch_title(title):
            return title[:80]
    return _fallback_title_from_url(url)


def build_fetch_entry(
    *,
    url: str,
    result: str,
    next_rid: Callable[[], str],
) -> dict | None:
    """Build source_index entry for a successful fetch_content result."""
    domain = _domain_from_url(url)
    title = extract_fetch_title(result, url)
    if is_nav_or_empty(title, result):
        log.info(f"🧹 跳过导航/空页面：{domain} | {title[:40]}")
        return None
    return {
        "id": next_rid(),
        "tool": "fetch_content",
        "query": url[:40],
        "title": title,
        "url": url,
        "domain": domain,
        "snippet": (result or "")[:300],
        "full_content": result,
    }


def build_wikipedia_entry(
    *,
    query: str,
    result: str,
    next_rid: Callable[[], str],
) -> dict:
    title = extract_wiki_title(result, query)
    return {
        "id": next_rid(),
        "tool": "wikipedia_lookup",
        "query": query[:40],
        "title": title,
        "url": f"https://en.wikipedia.org/wiki/{query.replace(' ', '_')}",
        "domain": "wikipedia.org",
        "snippet": (result or "")[:300],
        "full_content": result,
    }


def build_cache_entries(
    *,
    result: str,
    next_rid: Callable[[], str],
    existing_ids: set,
) -> list[dict]:
    """Parse read_today_cache JSON into source entries."""
    entries = []
    rows = json.loads(result or "[]")
    for row in rows:
        if row.get("error") or row.get("id") in existing_ids:
            continue
        url = row.get("url", "")
        entries.append({
            "id": row.get("id", next_rid()),
            "tool": "read_today_cache",
            "query": "今日缓存",
            "title": row.get("title", "")[:80],
            "url": url,
            "domain": _domain_from_url(url),
            "snippet": (row.get("snippet") or "")[:600],
            "full_content": row.get("full_content"),
        })
    return entries
