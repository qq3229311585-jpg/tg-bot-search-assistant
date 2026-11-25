#!/usr/bin/env python3
"""features/cost.py — /cost 花费看板 + 每日预算提醒

用法：
  /cost          今天的花费、各环节占比、缓存命中率，和昨天对比
  /cost 7        最近 7 天（1~31）
  /cost budget 3 设置每日预算（元），超出后当天提醒一次；/cost budget 0 关闭
"""
from __future__ import annotations

from datetime import timedelta

from tg_bot.features.store import load_json, now_bj, save_json, update_json
from tg_bot.usage import load_usage, prices, summarize_usage

_STAGE_LABELS = {
    "disambig": "意图判断",
    "query_fixer": "查询改写",
    "gather": "搜索采集",
    "writer": "写作",
    "critic": "模型核查",
    "patcher": "修补",
    "fast_chat": "快速聊天",
    "chat": "聊天杂项",
    "length_fix": "字数压缩",
    "summarize_for_context": "上下文摘要",
    "verify_legacy": "旧核查",
    "write_legacy": "旧写作",
    "feature": "新功能",
    "other": "其他",
}
_STATE_FILE = "cost_budget.json"


def _label(stage: str) -> str:
    if stage.startswith("feature:"):
        return "功能·" + stage.split(":", 1)[1]
    return _STAGE_LABELS.get(stage, stage)


def _bar(ratio: float, width: int = 10) -> str:
    filled = max(0, min(width, round(ratio * width)))
    return "▓" * filled + "░" * (width - filled)


def _money(value: float) -> str:
    if value >= 1:
        return f"¥{value:.2f}"
    return f"¥{value:.3f}"


def render_cost_report(days: int = 1, *, now=None) -> str:
    now = now or now_bj()
    rows = load_usage(days, now=now)
    summary = summarize_usage(rows)
    total = summary["total"]
    title = "今天" if days == 1 else f"最近 {days} 天"
    if not total["calls"]:
        return (
            f"💰 {title}还没有模型调用记录。\n"
            "账本从这个版本上线后开始记，聊几句再来看。"
        )

    hit_rate = total["hit"] / total["prompt"] if total["prompt"] else 0.0
    lines = [
        f"💰 {title}模型花费（估算）",
        "",
        f"合计 {_money(total['cost'])} · {total['calls']} 次调用",
        f"输入 {total['prompt']:,} token（缓存命中 {hit_rate:.0%}）· 输出 {total['completion']:,} token",
    ]

    if days == 1:
        yesterday = summarize_usage(
            [r for r in load_usage(2, now=now) if str(r.get("ts", "")).startswith((now - timedelta(days=1)).strftime("%Y-%m-%d"))]
        )["total"]
        if yesterday["calls"]:
            diff = total["cost"] - yesterday["cost"]
            arrow = "↑" if diff > 0 else "↓"
            lines.append(f"对比昨天 {_money(yesterday['cost'])}：{arrow}{_money(abs(diff))}")

    chats = sum(1 for r in rows if r.get("stage") == "disambig")
    if chats:
        lines.append(f"约 {chats} 轮对话，平均每轮 {_money(total['cost'] / chats)}")

    lines += ["", "各环节占比："]
    for stage, bucket in list(summary["by_stage"].items())[:8]:
        ratio = bucket["cost"] / total["cost"] if total["cost"] else 0
        lines.append(f"{_bar(ratio)} {_label(stage)} {ratio:.0%} · {bucket['calls']} 次")

    budget = get_budget()
    if budget:
        today_cost = summarize_usage(load_usage(1, now=now))["total"]["cost"]
        lines += ["", f"每日预算 {_money(budget)}：今天已用 {today_cost / budget:.0%}"]

    p = prices()
    lines += [
        "",
        f"单价按 输入命中 ¥{p['input_hit']}/M · 未命中 ¥{p['input_miss']}/M · 输出 ¥{p['output']}/M 估算，"
        "可用 LLM_PRICE_* 环境变量改成账单实价。",
    ]
    return "\n".join(lines)


def get_budget() -> float:
    state = load_json(_STATE_FILE, {})
    try:
        return max(float(state.get("daily_budget") or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def set_budget(value: float) -> None:
    update_json(_STATE_FILE, {}, lambda s: s.update({"daily_budget": max(value, 0.0)}))


def check_budget_alert(*, now=None) -> str:
    """超出每日预算时返回一条提醒（每天最多一次），否则返回空字符串。"""
    budget = get_budget()
    if not budget:
        return ""
    now = now or now_bj()
    today = now.strftime("%Y-%m-%d")
    state = load_json(_STATE_FILE, {})
    if state.get("alerted_on") == today:
        return ""
    spent = summarize_usage(load_usage(1, now=now))["total"]["cost"]
    if spent < budget:
        return ""
    state["alerted_on"] = today
    save_json(_STATE_FILE, state)
    return (
        f"⚠️ 今天的模型花费已到 {_money(spent)}，超过每日预算 {_money(budget)}。\n"
        "发 /cost 看看钱花在哪个环节。"
    )


def handle_cost(chat_id, text: str, send) -> None:
    args = text.split()[1:]
    if args and args[0] == "budget":
        if len(args) < 2:
            budget = get_budget()
            send(chat_id, f"当前每日预算：{_money(budget) if budget else '未设置'}。用法：/cost budget 3")
            return
        try:
            value = float(args[1])
        except ValueError:
            send(chat_id, "预算要是数字，比如：/cost budget 3")
            return
        set_budget(value)
        send(chat_id, "✅ 已关闭每日预算提醒" if value <= 0 else f"✅ 每日预算设为 {_money(value)}，超出后当天提醒一次")
        return
    days = 1
    if args:
        try:
            days = max(1, min(int(args[0]), 31))
        except ValueError:
            send(chat_id, "用法：/cost、/cost 7、/cost budget 3")
            return
    send(chat_id, render_cost_report(days))
