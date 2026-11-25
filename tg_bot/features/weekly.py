#!/usr/bin/env python3
"""features/weekly.py — /weekly 每周回顾

统计最近 7 天：聊了多少轮、哪几天最活跃、最常聊的话题词、走搜索的比例、花了多少钱，
最后用一次便宜的模型调用写一句“本周你最关心……”。不想花钱可以发 /weekly lite。
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import timedelta

from tg_bot.features.llm import cheap_chat
from tg_bot.features.store import now_bj
from tg_bot.usage import load_usage, summarize_usage

_STOP = set("的了吗呢吧啊是在我你他她它们这那有没和与就都也还要会能可以什么怎么为什么一下一个今天现在帮我一下吗")
_WEEKDAY = "一二三四五六日"


def _load_day(date_str: str) -> list[dict]:
    from tg_bot.config import DAILY_LOGS_DIR
    rows = []
    try:
        with open(os.path.join(DAILY_LOGS_DIR, f"{date_str}.jsonl"), encoding="utf-8") as handle:
            for line in handle:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return rows


def _keywords(texts: list[str], top: int = 8) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for text in texts:
        if text.startswith("/"):
            continue
        for word in re.findall(r"[A-Za-z][A-Za-z0-9+.#-]{1,20}|[一-鿿]{2,6}", text):
            if len(word) >= 2 and not all(ch in _STOP for ch in word):
                counter[word.lower()] += 1
    return [(w, c) for w, c in counter.most_common(top * 2) if c >= 2][:top]


def build_weekly(*, lite: bool = False, now=None, chat=None) -> str:
    now = now or now_bj()
    per_day = []
    user_texts = []
    for offset in range(6, -1, -1):
        day = now - timedelta(days=offset)
        rows = _load_day(day.strftime("%Y-%m-%d"))
        users = [r.get("content", "") for r in rows if r.get("role") == "user"]
        user_texts.extend(users)
        per_day.append((day, len(users)))

    total = sum(n for _, n in per_day)
    if not total:
        return "📅 最近 7 天还没有聊天记录，下周再来看回顾吧。"

    peak = max(per_day, key=lambda item: item[1])
    spark = "".join(" ▁▂▃▄▅▆▇█"[min(8, round(n / max(peak[1], 1) * 8))] for _, n in per_day)
    rows = load_usage(7, now=now)
    usage = summarize_usage(rows)["total"]
    # 写作环节只在搜索路径出现，用它和意图判断次数的比值估算搜索占比
    asked = sum(1 for r in rows if r.get("stage") == "disambig")
    searched = sum(1 for r in rows if r.get("stage") == "writer")
    search_ratio = f"{min(searched / asked, 1):.0%}" if asked else "—"

    keywords = _keywords(user_texts)
    lines = [
        f"📅 每周回顾（{(now - timedelta(days=6)).strftime('%m/%d')} – {now.strftime('%m/%d')}）",
        "",
        f"💬 一共聊了 {total} 轮，最活跃是周{_WEEKDAY[peak[0].weekday()]}（{peak[1]} 轮）",
        f"📈 每日走势 {spark}",
        f"🔍 需要搜索的问题占比 {search_ratio}",
        f"💰 模型花费约 ¥{usage['cost']:.2f}（{usage['calls']} 次调用）",
    ]
    if keywords:
        lines.append("🏷 高频话题：" + "、".join(f"{w}×{c}" for w, c in keywords))
    if not lite:
        sample = "\n".join(t[:80] for t in user_texts[-60:])
        line = (chat or cheap_chat)(
            f"下面是用户这一周发给助手的消息（节选）：\n{sample}",
            "用一句不超过 60 字的中文，像老朋友一样点评这个人本周最关心什么，可以带一点幽默，不要编造消息里没有的事。",
            max_tokens=120, temperature=0.8, stage="feature:weekly",
        )
        if line:
            lines += ["", "🗒 " + line.strip()]
    return "\n".join(lines)


def handle_weekly(chat_id, text: str, send) -> None:
    send(chat_id, build_weekly(lite="lite" in text))
