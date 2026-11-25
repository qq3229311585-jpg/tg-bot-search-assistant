#!/usr/bin/env python3
"""features/remind.py — /remind 自然语言提醒（纯规则解析，不调模型）

支持的说法：
  /remind 30分钟后 喝水            /remind 2小时后 收衣服
  /remind 15:30 开会               /remind 下午3点 取快递
  /remind 明天9点 交报告           /remind 后天早上8点半 体检
  /remind 每天8点 吃药             （每天重复）
  /remind list                     /remind del 2
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

from tg_bot.features.store import BJ, load_json, now_bj, update_json

_FILE = "reminders.json"
MAX_REMINDERS = 30

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(token: str) -> int:
    if token.isdigit():
        return int(token)
    if token == "半":
        return 30
    total, current = 0, 0
    for ch in token:
        if ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch in _CN_NUM:
            current = _CN_NUM[ch]
        else:
            raise ValueError(token)
    return total + current


_NUM = r"(\d+|[零一二两三四五六七八九十]+)"
_RELATIVE_RE = re.compile(rf"^{_NUM}\s*(分钟|分|小时|个小时|钟头|天)\s*(?:以?后|之后)\s*(.*)$")
_ABSOLUTE_RE = re.compile(
    rf"^(每天|今天|明天|后天)?\s*(早上|上午|中午|下午|晚上|傍晚|凌晨)?\s*"
    rf"{_NUM}\s*(?:[:：]\s*(\d{{1,2}})|点\s*(半|{_NUM}\s*分?)?|时)\s*(.*)$"
)


def parse_reminder(text: str, now: datetime | None = None) -> tuple[datetime, str, bool] | None:
    """解析提醒文本，返回 (触发时间, 内容, 是否每天重复)；无法解析返回 None。"""
    now = now or now_bj()
    text = (text or "").strip()
    m = _RELATIVE_RE.match(text)
    if m:
        amount = _cn_to_int(m.group(1))
        unit = m.group(2)
        delta = {"天": timedelta(days=amount)}.get(unit) or (
            timedelta(minutes=amount) if unit in ("分钟", "分") else timedelta(hours=amount)
        )
        content = m.group(3).strip() or "时间到了"
        return now + delta, content, False

    m = _ABSOLUTE_RE.match(text)
    if not m:
        return None
    day_word, period, hour_tok, colon_min, half_or_min, _min_inner, content = m.groups()
    hour = _cn_to_int(hour_tok)
    minute = 0
    if colon_min:
        minute = int(colon_min)
    elif half_or_min:
        minute = 30 if half_or_min == "半" else _cn_to_int(re.sub(r"\s*分$", "", half_or_min))
    if period in ("下午", "晚上", "傍晚") and hour < 12:
        hour += 12
    if period == "中午" and hour < 6:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day_word == "明天":
        base += timedelta(days=1)
    elif day_word == "后天":
        base += timedelta(days=2)
    elif base <= now:
        # 没说哪天、或者说了“每天/今天”但时间已过：没写上午下午的 12 小时制先试试下午
        if not period and hour < 12 and day_word != "每天" and (base + timedelta(hours=12)) > now:
            base += timedelta(hours=12)
        else:
            base += timedelta(days=1)
    return base, (content or "").strip() or "时间到了", day_word == "每天"


def add_reminder(text: str, now: datetime | None = None) -> str:
    parsed = parse_reminder(text, now)
    if not parsed:
        return (
            "没看懂时间 😅 试试这些说法：\n"
            "/remind 30分钟后 喝水\n/remind 明天9点 交报告\n/remind 下午3点半 取快递\n/remind 每天8点 吃药"
        )
    when, content, daily = parsed
    result = {}

    def mutate(items):
        if len(items) >= MAX_REMINDERS:
            result["msg"] = f"提醒太多啦（上限 {MAX_REMINDERS} 个），先用 /remind del 删掉几个"
            return
        items.append({
            "id": uuid.uuid4().hex[:8],
            "at": when.isoformat(timespec="seconds"),
            "text": content[:200],
            "daily": daily,
        })
        items.sort(key=lambda r: r["at"])
        result["msg"] = f"⏰ 好的，{_fmt_when(when, now)}{'（每天）' if daily else ''}提醒你：{content}"

    update_json(_FILE, [], mutate)
    return result["msg"]


def _fmt_when(when: datetime, now: datetime | None = None) -> str:
    now = now or now_bj()
    days = (when.date() - now.date()).days
    day = {0: "今天", 1: "明天", 2: "后天"}.get(days, when.strftime("%m月%d日"))
    return f"{day} {when.strftime('%H:%M')} "


def list_reminders() -> str:
    items = load_json(_FILE, [])
    if not items:
        return "没有待提醒的事项。用法：/remind 30分钟后 喝水"
    lines = ["⏰ 待提醒"]
    for i, r in enumerate(items, 1):
        when = datetime.fromisoformat(r["at"])
        lines.append(f"{i}. {_fmt_when(when)}{'（每天）' if r.get('daily') else ''}— {r['text']}")
    lines.append("\n删除：/remind del 编号")
    return "\n".join(lines)


def remove_reminder(index: int) -> str:
    result = {}

    def mutate(items):
        if 1 <= index <= len(items):
            result["msg"] = f"✅ 已删除提醒：{items.pop(index - 1)['text']}"
        else:
            result["msg"] = "没有这个编号，先发 /remind list 看看"

    update_json(_FILE, [], mutate)
    return result["msg"]


def pop_due(now: datetime | None = None) -> list[str]:
    """取出所有到期提醒（每天重复的顺延一天），返回要发送的文本。"""
    now = now or now_bj()
    due: list[str] = []

    def mutate(items):
        keep = []
        for r in items:
            try:
                at = datetime.fromisoformat(r["at"])
            except (KeyError, ValueError):
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=BJ)
            if at <= now:
                late = now - at
                note = f"（晚了 {int(late.total_seconds() // 60)} 分钟，可能刚才 bot 不在线）" if late > timedelta(minutes=10) else ""
                due.append(f"⏰ 提醒：{r['text']}{note}")
                if r.get("daily"):
                    while at <= now:
                        at += timedelta(days=1)
                    r["at"] = at.isoformat(timespec="seconds")
                    keep.append(r)
            else:
                keep.append(r)
        keep.sort(key=lambda r: r["at"])
        return keep

    update_json(_FILE, [], mutate)
    return due


def handle_remind(chat_id, text: str, send) -> None:
    arg = text[len("/remind"):].strip()
    if not arg or arg == "list":
        send(chat_id, list_reminders())
    elif arg.startswith("del"):
        try:
            send(chat_id, remove_reminder(int(arg[3:].strip())))
        except ValueError:
            send(chat_id, "用法：/remind del 编号")
    else:
        send(chat_id, add_reminder(arg))
