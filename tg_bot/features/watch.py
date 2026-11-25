#!/usr/bin/env python3
"""features/watch.py — /watch 话题追踪：关注一个话题，有新进展自动推送

用法：
  /watch 英伟达财报       关注一个话题（最多 10 个）
  /watch list            查看关注列表和上次检查时间
  /watch del 2           取消第 2 个
  /watch now             立刻检查一遍所有话题

设计：
- 每个话题每 WATCH_INTERVAL_HOURS（默认 12）小时最多查一次，只调一次新闻搜索，不调模型，几乎零成本。
- 用 URL + 规范化标题做指纹，推过的不会再推；同一事件换个标题转载也会被识别成旧闻。
- 第一次关注时只建立基线（把当前结果记为“已看过”），之后才推送真正的新进展。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta

from tg_bot.features.store import load_json, now_bj, update_json

log = logging.getLogger(__name__)

_FILE = "watches.json"
MAX_WATCHES = 10
MAX_SEEN = 300
MAX_PUSH_ITEMS = 3


def _interval() -> timedelta:
    try:
        hours = float(os.getenv("WATCH_INTERVAL_HOURS", "12"))
    except ValueError:
        hours = 12.0
    return timedelta(hours=max(hours, 1.0))


def _norm_title(title: str) -> str:
    title = re.sub(r"\s*[-|｜_—]\s*[^-|｜_—]{1,20}$", "", title or "")  # 去掉“ - 媒体名”后缀
    title = re.sub(r"[\W_]+", "", title.lower())
    return title[:40]


def fingerprints(item: dict) -> set[str]:
    out = set()
    url = (item.get("url") or "").split("#")[0].split("?")[0].rstrip("/")
    if url:
        out.add("u:" + hashlib.sha1(url.encode()).hexdigest()[:16])
    title = _norm_title(item.get("title") or "")
    if len(title) >= 6:
        out.add("t:" + hashlib.sha1(title.encode()).hexdigest()[:16])
    return out


def _load() -> list[dict]:
    return load_json(_FILE, [])


def add_watch(topic: str) -> str:
    topic = re.sub(r"\s+", " ", topic or "").strip()[:60]
    if not topic:
        return "用法：/watch 话题，比如 /watch 英伟达财报"
    result = {}

    def mutate(watches):
        if any(w.get("topic") == topic for w in watches):
            result["msg"] = f"「{topic}」已经在关注列表里了"
            return
        if len(watches) >= MAX_WATCHES:
            result["msg"] = f"最多关注 {MAX_WATCHES} 个话题，先用 /watch del 删掉一个"
            return
        watches.append({
            "topic": topic,
            "created": now_bj().isoformat(timespec="seconds"),
            "last_run": "",
            "seen": [],
            "baseline": False,
            "pushed": 0,
        })
        result["msg"] = (
            f"👀 开始关注「{topic}」。\n"
            f"每 {int(_interval().total_seconds() // 3600)} 小时查一次新闻，有新进展才推送；"
            "第一次检查只记录现状，不会刷屏。"
        )

    update_json(_FILE, [], mutate)
    return result["msg"]


def remove_watch(index: int) -> str:
    result = {}

    def mutate(watches):
        if 1 <= index <= len(watches):
            result["msg"] = f"✅ 已取消关注「{watches.pop(index - 1)['topic']}」"
        else:
            result["msg"] = "没有这个编号，先发 /watch list 看看"

    update_json(_FILE, [], mutate)
    return result["msg"]


def list_watches() -> str:
    watches = _load()
    if not watches:
        return "还没有关注任何话题。发 /watch 话题 开始关注，比如 /watch 英伟达财报"
    lines = ["👀 关注列表"]
    for i, w in enumerate(watches, 1):
        last = w.get("last_run") or ""
        last_txt = last[5:16].replace("T", " ") if last else "还没查过"
        lines.append(f"{i}. {w['topic']}（上次检查 {last_txt}，已推送 {w.get('pushed', 0)} 条）")
    lines.append("\n取消：/watch del 编号 · 立刻检查：/watch now")
    return "\n".join(lines)


def check_watches(search=None, *, force: bool = False, now: datetime | None = None) -> list[str]:
    """检查到期的话题，返回要推送的消息列表。``search(query) -> (items, diagnostics)``。"""
    if search is None:
        from tg_bot.tools.search import execute_news_candidates as search
    now = now or now_bj()
    watches = _load()
    due = []
    for i, w in enumerate(watches):
        last = w.get("last_run")
        try:
            last_dt = datetime.fromisoformat(last) if last else None
        except ValueError:
            last_dt = None
        if force or last_dt is None or now - last_dt >= _interval():
            due.append(i)
    if not due:
        return []

    results = {}
    for i in due:
        topic = watches[i]["topic"]
        try:
            items, _diag = search(topic)
        except Exception as exc:
            log.warning("watch search failed for %s: %s", topic, exc)
            items = None
        results[topic] = items

    messages = []

    def mutate(current):
        for w in current:
            topic = w.get("topic")
            if topic not in results:
                continue
            w["last_run"] = now.isoformat(timespec="seconds")
            items = results[topic]
            if items is None:
                continue
            seen = set(w.get("seen") or [])
            fresh = []
            for item in items:
                fps = fingerprints(item)
                if not fps or fps & seen:
                    continue
                seen |= fps
                fresh.append(item)
            w["seen"] = list(seen)[-MAX_SEEN:]
            if not w.get("baseline"):
                w["baseline"] = True
                if not force:
                    continue
            if not fresh:
                continue
            w["pushed"] = int(w.get("pushed") or 0) + min(len(fresh), MAX_PUSH_ITEMS)
            lines = [f"👀 「{topic}」有新进展："]
            for item in fresh[:MAX_PUSH_ITEMS]:
                summary = re.sub(r"\s+", " ", item.get("summary") or "").strip()[:80]
                lines.append(f"\n• {item.get('title', '').strip()}")
                if summary:
                    lines.append(f"  {summary}")
                if item.get("url"):
                    lines.append(f"  {item['url']}")
            if len(fresh) > MAX_PUSH_ITEMS:
                lines.append(f"\n另有 {len(fresh) - MAX_PUSH_ITEMS} 条相关报道未展开。")
            messages.append("\n".join(lines))

    update_json(_FILE, [], mutate)
    return messages


def handle_watch(chat_id, text: str, send) -> None:
    arg = text[len("/watch"):].strip()
    if not arg or arg == "list":
        send(chat_id, list_watches())
        return
    if arg.startswith("del"):
        try:
            send(chat_id, remove_watch(int(arg[3:].strip())))
        except ValueError:
            send(chat_id, "用法：/watch del 编号")
        return
    if arg == "now":
        if not _load():
            send(chat_id, list_watches())
            return
        messages = check_watches(force=True)
        if not messages:
            send(chat_id, "查了一遍，关注的话题暂时没有新进展。")
        for message in messages:
            send(chat_id, message)
        return
    send(chat_id, add_watch(arg))
