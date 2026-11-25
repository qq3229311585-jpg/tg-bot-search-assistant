#!/usr/bin/env python3
"""features/link_digest.py — 链接速读：直接发一个网址（或 /tldr 网址），回一份三行摘要

- 抓取走现有 execute_fetch_content（带 SSRF 防护、重定向复核）。
- 只调一次关闭思考的模型，正文截到 6000 字，成本约等于一次普通聊天。
- 同一个网址 24 小时内重复发送直接用缓存，不再花钱。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from tg_bot.features.llm import cheap_chat
from tg_bot.features.store import load_json, now_bj, save_json

_URL_RE = re.compile(r"^(https?://\S+)$", re.I)
_CACHE_FILE = "link_digest_cache.json"
_CACHE_TTL = timedelta(hours=24)
_SYSTEM = (
    "你是速读助手。根据给出的网页正文，用中文输出：\n"
    "第一行：「一句话：」+ 不超过 40 字的核心结论\n"
    "然后 3 条要点，每条以「- 」开头，每条不超过 50 字，只写正文里有的事实\n"
    "最后一行：「值得细读吗：」+ 值得/一般/不必 + 不超过 20 字的理由\n"
    "不要用 Markdown 加粗，不要编造正文没有的内容。"
)


def extract_url(text: str) -> str:
    text = (text or "").strip()
    if text.lower().startswith("/tldr"):
        text = text[5:].strip()
    m = _URL_RE.match(text)
    return m.group(1) if m else ""


def is_link_message(text: str) -> bool:
    return bool(_URL_RE.match((text or "").strip()))


def digest(url: str, *, fetch=None, chat=None, now: datetime | None = None) -> str:
    now = now or now_bj()
    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    cache = load_json(_CACHE_FILE, {})
    hit = cache.get(key)
    if isinstance(hit, dict):
        try:
            if now - datetime.fromisoformat(hit["at"]) <= _CACHE_TTL:
                return hit["text"] + "\n\n（24 小时内读过，直接用了缓存）"
        except (KeyError, ValueError):
            pass

    if fetch is None:
        from tg_bot.tools.fetch import execute_fetch_content as fetch
    raw = fetch(url) or ""
    if raw.startswith("正文抓取失败") or len(raw) < 200:
        return f"😕 这个链接的正文抓不下来（可能要登录、是付费墙，或者是纯图片/视频页）。\n{url}"
    body = re.sub(r"^\[正文来源[^\]]*\]\s*", "", raw)[:6000]
    out = (chat or cheap_chat)(f"网址：{url}\n\n正文：\n{body}", _SYSTEM, max_tokens=500, temperature=0.3,
                               stage="feature:tldr")
    if not out:
        return "模型暂时没响应，稍后再发一次链接试试。"
    text = "📎 链接速读\n\n" + out.strip()
    cache = {k: v for k, v in cache.items() if isinstance(v, dict) and v.get("at", "") >= (now - _CACHE_TTL).isoformat()}
    cache[key] = {"at": now.isoformat(timespec="seconds"), "text": text}
    save_json(_CACHE_FILE, cache)
    return text


def handle_link(chat_id, text: str, send) -> None:
    url = extract_url(text)
    if not url:
        send(chat_id, "用法：直接发一个网址，或者 /tldr 网址")
        return
    send(chat_id, digest(url))
