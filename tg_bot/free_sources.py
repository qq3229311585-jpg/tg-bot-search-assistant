#!/usr/bin/env python3
"""免费、不限量的补充搜索源:维基百科、GitHub 仓库、Hacker News。

只用标准库。所有网络访问都通过参数注入的 get(url, headers=...) 完成,便于测试。
任何失败(网络、超时、坏 JSON、字段缺失)一律返回 [],永不抛异常。
返回行格式与 gnews_rows 一致:{url,title,snippet,published_at,source}。
"""

import html
import json
import os
import re
import urllib.parse
import urllib.request

WIKI_API = "https://{lang}.wikipedia.org/w/api.php"
GITHUB_API = "https://api.github.com/search/repositories"
HN_API = "https://hn.algolia.com/api/v1/search"
UA = {"User-Agent": "tg_bot-free-search/1.0 (personal use)"}
TIMEOUT = 15

CJK_RE = re.compile(r"[㐀-鿿぀-ヿ가-힯]")
TAG_RE = re.compile(r"<[^>]+>")
# 英文技术词按整词匹配,中文技术词按子串匹配
TECH_LATIN_RE = re.compile(
    r"\b(github|repo|repos|api|sdk|cli|python|rust|golang|java|javascript|typescript|"
    r"js|ts|node|nodejs|react|vue|docker|kubernetes|linux|framework|library|open\s?source)\b",
    re.IGNORECASE)
TECH_CJK = ("开源", "库", "框架", "编程", "代码", "仓库")
LATIN_PHRASE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\- ]*")


def _clean_snippet(text):
    return html.unescape(TAG_RE.sub("", str(text or ""))).strip()


def _fetch_json(get, url, headers=None):
    """取 JSON;任何失败返回 None。"""
    try:
        data = json.loads(get(url, headers=headers or {}))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def default_get(url, headers=None, timeout=TIMEOUT):
    """带自定义 header 的 GET;合并默认 UA。"""
    merged = dict(UA)
    merged.update(headers or {})
    req = urllib.request.Request(url, headers=merged)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def wiki_rows(query, *, get, max_items=4):
    try:
        lang = "zh" if CJK_RE.search(query) else "en"
        url = WIKI_API.format(lang=lang) + "?" + urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": max_items, "format": "json", "utf8": 1,
        })
        data = _fetch_json(get, url)
        items = data["query"]["search"]
        rows = []
        for item in items[:max_items]:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            rows.append({
                "url": f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title)}",
                "title": title,
                "snippet": _clean_snippet(item.get("snippet")),
                "published_at": None,
                "source": "wikipedia",
            })
        return rows
    except Exception:
        return []


def github_rows(query, *, get, max_items=4):
    try:
        url = GITHUB_API + "?" + urllib.parse.urlencode({
            "q": query, "sort": "stars", "per_page": max_items,
        })
        headers = {"Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = _fetch_json(get, url, headers)
        items = data["items"]
        rows = []
        for item in items[:max_items]:
            html_url = str(item.get("html_url") or "").strip()
            full_name = str(item.get("full_name") or "").strip()
            if not html_url or not full_name:
                continue
            pushed = item.get("pushed_at")
            parts = []
            desc = str(item.get("description") or "").strip()
            if desc:
                parts.append(desc)
            parts.append(f"★{item.get('stargazers_count') or 0}")
            if pushed:
                parts.append(f"更新于{str(pushed)[:10]}")
            rows.append({
                "url": html_url,
                "title": full_name,
                "snippet": " · ".join(parts),
                "published_at": pushed,
                "source": "github",
            })
        return rows
    except Exception:
        return []


def hn_rows(query, *, get, max_items=4):
    try:
        url = HN_API + "?" + urllib.parse.urlencode({
            "query": query, "tags": "story", "hitsPerPage": max_items,
        })
        data = _fetch_json(get, url)
        hits = data["hits"]
        rows = []
        for hit in hits[:max_items]:
            title = str(hit.get("title") or hit.get("story_title") or "").strip()
            if not title:
                continue
            object_id = hit.get("objectID")
            link = str(hit.get("url") or "").strip()
            if not link:
                link = f"https://news.ycombinator.com/item?id={object_id}"
            points = hit.get("points") or 0
            comments = hit.get("num_comments") or 0
            rows.append({
                "url": link,
                "title": title,
                "snippet": f"{points} points · {comments} 评论",
                "published_at": hit.get("created_at"),
                "source": "hn",
            })
        return rows
    except Exception:
        return []


def _is_technical(query):
    if TECH_LATIN_RE.search(query):
        return True
    return any(term in query for term in TECH_CJK)


def _is_latin_phrase(query):
    """全拉丁字母、含空格或连字符的查询(如 'machine learning'、'node-fetch')。"""
    q = query.strip()
    return bool(LATIN_PHRASE_RE.fullmatch(q)) and (" " in q or "-" in q)


def free_rows(query, time_range, *, get):
    """按查询与时间窗决定调用哪些免费源,返回合并后的行列表;任何异常返回 []。"""
    try:
        q = str(query or "").strip()
        if not q:
            return []
        tr = str(time_range or "any")
        rows = []
        # 百科无发布日期,只在不限时或按月的查询中使用
        if tr in ("any", "month"):
            rows += wiki_rows(q, get=get)
        if _is_technical(q):
            rows += github_rows(q, get=get)
        if _is_technical(q) and tr != "day":
            rows += hn_rows(q, get=get)
        return rows
    except Exception:
        return []
