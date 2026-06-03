#!/usr/bin/env python3
"""tools/fetch.py — 内容抓取、缓存读取相关工具函数"""

import json, re, os, logging
from urllib.error import HTTPError
from urllib.request import urlopen, Request
from datetime import datetime, timezone, timedelta

from tg_bot.config import (
    SOURCES_DIR,
    _ctx,
)
from tg_bot.storage import load_today_index, update_today_index

log = logging.getLogger(__name__)


# ── HTTP 辅助 ─────────────────────────────────────────────────────────
def http_get(url, headers=None, timeout=15):
    h = {"User-Agent": "Mozilla/5.0"}
    if headers: h.update(headers)
    try:
        with urlopen(Request(url, headers=h), context=_ctx, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"GET err: {e}")
        return ""

def http_post(url, payload, headers=None, timeout=50):
    body = json.dumps(payload, ensure_ascii=False).encode()
    h = {"Content-Type": "application/json"}
    if headers: h.update(headers)
    try:
        with urlopen(Request(url, data=body, headers=h), context=_ctx, timeout=timeout) as r:
            return json.loads(r.read())
    except HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:800]
        except Exception:
            err_body = ""
        log.warning(f"POST err: {e} body={err_body}")
        return None
    except Exception as e:
        log.warning(f"POST err: {e}")
        return None


# ── 付费墙域名列表（fetch 失败时额外尝试 12ft.io 绕过） ────────────────
_PAYWALL_DOMAINS = {
    "ft.com", "economist.com", "nytimes.com", "wsj.com",
    "bloomberg.com", "theatlantic.com", "theinformation.com",
    "wired.com", "hbr.org", "newyorker.com",
    "foreignpolicy.com", "technologyreview.com",
}


def _try_12ft(url):
    """尝试用 12ft.io 代理绕过付费墙，成功返回正文字符串，失败返回 None"""
    proxy_url = f"https://12ft.io/proxy?q={url}"
    try:
        raw = http_get(proxy_url, timeout=15)
        if not raw:
            return None
        # 去掉 HTML 标签提取纯文本
        text = re.sub(r'<style[^>]*>.*?</style>', ' ', raw, flags=re.DOTALL)
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 500:
            log.info(f"✅ 12ft.io 绕过成功，获取 {len(text)} 字符：{url[:60]}")
            return f"[正文来源（12ft代理）：{url}]\n\n{text[:8000]}"
        return None
    except Exception as e:
        log.debug(f"12ft.io 失败: {e}")
        return None


def execute_fetch_content(url):
    """
    用 Tavily /extract 抓取正文。
    付费墙域名且内容过短时，额外尝试 12ft.io 绕过。
    """
    log.info(f"📄 Tavily extract: {url}")
    # 判断是否付费墙域名
    try:
        _domain = url.split("/")[2].lstrip("www.")
    except Exception:
        _domain = ""
    _is_paywall = any(
        _domain == d or _domain.endswith("." + d)
        for d in _PAYWALL_DOMAINS
    )

    from tg_bot.tools.search import _tavily_request
    tavily_text = ""
    try:
        d = _tavily_request("https://api.tavily.com/extract", {"urls": [url]})
        if d:
            results = d.get("results", [])
            if results:
                raw = results[0].get("raw_content", "") or results[0].get("content", "")
                tavily_text = (raw or "").strip()
    except Exception as e:
        log.warning(f"Tavily extract 异常: {e}")

    # 内容够用，直接返回
    if len(tavily_text) > 300:
        return f"[正文来源：{url}]\n\n{tavily_text[:8000]}"

    # 内容过短或失败 + 付费墙，尝试 12ft.io
    if _is_paywall:
        log.info(f"🔓 Tavily 内容不足（{len(tavily_text)}字），尝试 12ft.io：{url[:60]}")
        bypass = _try_12ft(url)
        if bypass:
            return bypass

    # 有一点内容就返回，完全没有才报失败
    if tavily_text:
        return f"[正文来源：{url}]\n\n{tavily_text[:8000]}"
    return f"正文抓取失败（所有方式均不可用）：{url}"


def execute_read_cache(ids, level="snippet"):
    """read_today_cache 工具的实际执行：按 ID 读取今日缓存的简介或正文"""
    day     = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    day_dir = os.path.join(SOURCES_DIR, day)
    id_set  = set(ids)
    found   = {}
    try:
        for fn in sorted(os.listdir(day_dir)):
            if not fn.endswith(".json") or fn == "index.json":
                continue
            data = json.loads(open(os.path.join(day_dir, fn), encoding="utf-8").read())
            for entry in (data.get("results") or data.get("sources") or []):
                if entry.get("id") in id_set:
                    found[entry["id"]] = entry
            if len(found) == len(id_set):
                break
    except Exception as e:
        return f"缓存读取失败: {e}"
    if not found:
        return "未找到指定 ID 的缓存记录"
    out = []
    for rid in ids:
        e = found.get(rid)
        if not e:
            out.append({"id": rid, "error": "未找到"})
            continue
        r = {
            "id":      rid,
            "title":   e.get("title", ""),
            "url":     e.get("url", ""),
            "snippet": e.get("snippet", ""),
        }
        if level == "full":
            fc = e.get("full_content")
            if fc:
                r["full_content"] = fc[:8000]
            else:
                r["note"] = "该页面未曾抓取原文，可调用 fetch_content 工具获取"
        out.append(r)
    return json.dumps(out, ensure_ascii=False, indent=2)
