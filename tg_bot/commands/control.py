#!/usr/bin/env python3
"""commands/control.py — /clear /recap /help /start /ask 命令处理函数"""

import json
import logging
from tg_bot.file_io import atomic_write_json

from tg_bot.config import THINKING_FILE, TOOLLOG_FILE, CONTEXT_FILE
from tg_bot.bot_utils import send
from tg_bot.storage import load_report

log = logging.getLogger(__name__)

_HELP_TEXT = (
    "📋 命令列表\n\n"
    "💬 对话\n"
    "• /ask [问题] — 强制搜索模式提问\n"
    "• /clear — 清空本轮对话上下文（历史记录不受影响）\n\n"
    "📊 数据查看\n"
    "• /thinking — 上一条回复的思考过程\n"
    "• /tools — 最近几轮工具使用记录\n"
    "• /sources — 最近搜索来源存档列表\n"
    "• /worklog — 今日工作日志（主AI+审核员）\n"
    "• /worklog 20260517 — 指定日期工作日志\n"
    "• /diary — 今天的日总结和用户画像\n"
    "• /diary 昨天 — 昨天的记录\n"
    "• /diary list — 所有有记录的日期\n"
    "• /diary profile — 最近7天用户画像\n\n"
    "🔑 API 配额\n"
    "• /balance — 各 API 余额和剩余次数\n"
    "• /quota — 本月用量进度条\n"
    "• /quota set 70 — 设置预警阈值（百分比）\n"
    "• /quota set brave 1000 — 设置某 API 月上限\n"
    "• /quota set tavily 1000 — 同时设置两个 Tavily key\n\n"
    "📰 午报\n"
    "• /recap — 重新发送今日午报\n"
)


def handle_start(chat_id):
    send(chat_id,
         "👋 你好！我是你的午报助手。\n\n"
         "• 每天中午自动推送午报\n"
         "• 随时追问报告内容\n"
         "• 🔍 需要最新信息时自动搜索\n"
         "• 📖 问事实/概念时自动查 Wikipedia\n\n"
         + _HELP_TEXT +
         "\n有什么想聊的？")


def handle_help(chat_id):
    send(chat_id, _HELP_TEXT)


def handle_clear(chat_id):
    # 只清上下文缓冲、思考记录、工具记录，保留完整聊天历史
    for _f in [CONTEXT_FILE, THINKING_FILE, TOOLLOG_FILE]:
        try:
            atomic_write_json(_f, [])
        except Exception:
            pass
    send(chat_id, "✅ 本轮对话上下文已清空，重新开始。历史记录和日志完整保留。")


def handle_recap(chat_id):
    report = load_report()
    if report:
        send(chat_id, "📋 今日午报重发：\n\n" + report)
    else:
        send(chat_id, "今天还没有午报，等中午自动推送，或联系管理员手动触发。")


def handle_ask(chat_id, text, handle_fn):
    """处理 /ask 命令，调用 handle_fn 发起搜索"""
    q = text[5:].strip()
    if q:
        handle_fn(chat_id, f"【强制搜索模式】请立即搜索并回答：{q}")
    else:
        send(chat_id, "用法：/ask 你想搜索的问题")
