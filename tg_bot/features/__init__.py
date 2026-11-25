#!/usr/bin/env python3
"""features — 新增的 10 个功能

重点：/cost 花费看板、/watch 话题追踪、链接速读（/tldr）、/weekly 每周回顾、/remind 提醒
好玩：/quiz 午报小测验、/debate 正反方辩论、/eli5 讲给五岁小孩听、/fun 冷知识、/poem 打油诗
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

FEATURE_HELP = (
    "✨ 新功能\n"
    "• /cost — 今天模型花了多少钱、花在哪（/cost 7 看一周，/cost budget 3 设每日预算）\n"
    "• /watch 话题 — 关注话题，有新进展自动推送（/watch list、/watch del 1、/watch now）\n"
    "• 直接发网址 或 /tldr 网址 — 链接速读，三行摘要\n"
    "• /weekly — 本周回顾（/weekly lite 不调模型）\n"
    "• /remind 30分钟后 喝水 — 提醒（支持 明天9点、下午3点半、每天8点；/remind list）\n"
    "• /quiz — 用今天午报出 3 道题\n"
    "• /debate 话题 — 正反方辩论\n"
    "• /eli5 问题 — 讲给五岁小孩听\n"
    "• /fun — 随机冷知识\n"
    "• /poem — 把今天的聊天写成打油诗\n"
)

_PREFIX_HANDLERS = (
    ("/cost", "tg_bot.features.cost", "handle_cost"),
    ("/watch", "tg_bot.features.watch", "handle_watch"),
    ("/tldr", "tg_bot.features.link_digest", "handle_link"),
    ("/weekly", "tg_bot.features.weekly", "handle_weekly"),
    ("/remind", "tg_bot.features.remind", "handle_remind"),
    ("/quiz", "tg_bot.features.fun", "handle_fun"),
    ("/debate", "tg_bot.features.fun", "handle_fun"),
    ("/eli5", "tg_bot.features.fun", "handle_fun"),
    ("/fun", "tg_bot.features.fun", "handle_fun"),
    ("/poem", "tg_bot.features.fun", "handle_fun"),
    ("/features", None, None),
)


def _matches(text: str, prefix: str) -> bool:
    return text == prefix or text.startswith(prefix + " ")


def dispatch(chat_id, text: str, send) -> bool:
    """处理新功能命令。返回 True 表示已处理，主循环不用再走聊天流程。"""
    import importlib
    from tg_bot.usage import llm_request

    text = (text or "").strip()
    for prefix, module_name, func_name in _PREFIX_HANDLERS:
        if _matches(text, prefix):
            if module_name is None:
                send(chat_id, FEATURE_HELP)
                return True
            handler = getattr(importlib.import_module(module_name), func_name)
            try:
                with llm_request("feature:" + prefix[1:]):
                    handler(chat_id, text, send)
            except Exception as exc:
                log.warning("feature %s failed: %s", prefix, exc, exc_info=True)
                send(chat_id, f"这个功能刚才出错了（{type(exc).__name__}），稍后再试。")
            return True

    from tg_bot.features.link_digest import is_link_message
    if is_link_message(text):
        from tg_bot.features.link_digest import handle_link
        with llm_request("feature:tldr"):
            handle_link(chat_id, text, send)
        return True
    return False
