#!/usr/bin/env python3
"""Google 新闻 RSS 搜索提供方(免费、不限量、带发布日期)

所有网络访问都通过参数注入(get/post/sleep),便于测试。
解码算法参照 scratchpad/gdec.py(已在服务器上实测可用)。
"""

from email.utils import parsedate_to_datetime
from datetime import timezone
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
RSS_SEARCH = "https://news.google.com/rss/search"
ARTICLE_URL = "https://news.google.com/rss/articles/{aid}?hl=en-US&gl=US&ceid=US:en"
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
CJK_RE = re.compile(r"[㐀-鿿぀-ヿ가-힯]")
WHEN = {"day": " when:1d", "week": " when:7d", "month": " when:30d", "any": ""}
# batchexecute 响应里 garturlres 后面跟着跳转 URL;允许 \uXXXX 转义穿过捕获
GARTURL_RE = r'garturlres\\",\\"(https?://(?:[^\\"]|\\u[0-9a-fA-F]{4})+)'
MAX_FAILS = 3
DECODE_GAP = 0.15


def build_rss_url(query, time_range="any"):
    q = query + WHEN.get(time_range, "")
    if CJK_RE.search(query):
        region = "hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    else:
        region = "hl=en-US&gl=US&ceid=US:en"
    return f"{RSS_SEARCH}?q={urllib.parse.quote(q)}&{region}"


def _published_iso(pub):
    try:
        dt = parsedate_to_datetime(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def parse_rss(xml_text, now=None):
    """解析 RSS 的 item;解析失败返回 []。now 为保留参数,暂未使用。"""
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        source_el = it.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        source_url = (source_el.get("url") or "") if source_el is not None else ""
        suffix = " - " + source
        if source and title.endswith(suffix):
            title = title[: -len(suffix)].rstrip()
        out.append({
            "google_link": (it.findtext("link") or "").strip(),
            "title": title,
            "source": source,
            "source_url": source_url,
            "published_at": _published_iso(it.findtext("pubDate") or ""),
        })
    return out


def decode_link(google_link, *, get, post):
    """Google 跳转链接 -> 真实文章 URL;任何失败返回 None。"""
    try:
        aid = google_link.split("/articles/")[1].split("?")[0]
        html = get(ARTICLE_URL.format(aid=aid))
        sg = re.search(r'data-n-a-sg="([^"]+)"', html)
        ts = re.search(r'data-n-a-ts="([^"]+)"', html)
        if not sg or not ts:
            return None
        inner = json.dumps(
            ["garturlreq",
             [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
              "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
             aid, int(ts.group(1)), sg.group(1)],
            separators=(",", ":"),
        )
        freq = json.dumps([[["Fbv4je", inner, None, "generic"]]], separators=(",", ":"))
        data = urllib.parse.urlencode({"f.req": freq}).encode()
        body = post(BATCH_URL, data)
        m = re.search(GARTURL_RE, body)
        if not m:
            return None
        raw = m.group(1)
        try:
            url = json.loads('"' + raw + '"')
        except ValueError:
            url = raw
        return url if url.startswith(("http://", "https://")) else None
    except Exception:
        return None


def gnews_rows(query, time_range="any", *, max_items=8, get, post, sleep=time.sleep, cache=None):
    """返回项目统一行格式:{url,title,snippet,published_at,source}。"""
    try:
        items = parse_rss(get(build_rss_url(query, time_range)))
        rows = []
        fails = 0
        requested = False
        for it in items[:max_items]:
            link = it["google_link"]
            url = cache.get(link) if cache is not None else None
            if url is None:
                if requested:
                    sleep(DECODE_GAP)
                requested = True
                url = decode_link(link, get=get, post=post)
                if url and cache is not None:
                    cache[link] = url
            if not url:
                fails += 1
                if fails >= MAX_FAILS:
                    break
                continue
            fails = 0
            rows.append({
                "url": url,
                "title": it["title"],
                "snippet": f"{it['source']} · {it['title']}",
                "published_at": it["published_at"],
                "source": "gnews",
            })
        return rows
    except Exception:
        return []


def default_get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def default_post(url, data_bytes, timeout=15):
    headers = dict(UA, **{"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    req = urllib.request.Request(url, data=data_bytes, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
