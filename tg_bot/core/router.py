#!/usr/bin/env python3
"""core/router.py — 路由决策（替代 search_policy.py 的分散逻辑）

职责（只做这一件事）：
  输入：消歧层输出的 pre dict
  输出：lane 名 + 路由原因

Lane 定义：
  "fast"    → 不需要搜索，直接调 fast_chat（闲聊/知识问答/数学/代码）
  "tool"    → 直接 API 工具（天气/日历/VPS流量/API余额）
  "search"  → 完整搜索 pipeline
  "history" → 查询历史日志（read_daily_log / search_daily_summaries）

不做的事：
  - 不做 AI 调用
  - 不修改 keywords
  - 不做任何搜索或抓取
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)

_DIRECT_TOOL_MAP = {
    "check_weather": "tool",
    "vps_traffic": "tool",
    "github_trending": "tool",
    "check_api_balance": "tool",
    "calendar_query": "tool",
    "calendar_add": "tool",
}

_HISTORY_TOOLS = {"read_daily_log", "search_daily_summaries", "read_daily_summary", "search_chat_history"}

# 强制走搜索的查询类型（不管消歧层说什么）
_FORCE_SEARCH_TYPES = {"搜索", "金融", "医疗", "法律"}

# 绝不走搜索的查询类型
_SKIP_SEARCH_TYPES = {"闲聊", "系统查询"}


def decide_lane(pre: dict) -> tuple[str, str]:
    """
    根据消歧层输出 pre 决定走哪条 lane。
    返回 (lane_name, reason)。

    pre 的关键字段：
      - needs_search: bool
      - query_type: str
      - suggested_tool: str
      - keywords: list[str]
    """
    if not pre:
        return "search", "消歧失败，默认走搜索"

    query_type = pre.get("query_type", "其他")
    suggested_tool = pre.get("suggested_tool", "")
    needs_search = pre.get("needs_search", True)

    # 历史查询
    if query_type == "历史查询" or suggested_tool in _HISTORY_TOOLS:
        return "history", "历史对话查询"

    # 直接 API 工具
    if suggested_tool in _DIRECT_TOOL_MAP:
        return "tool", f"直接工具: {suggested_tool}"

    # 强制搜索
    if any(t in query_type for t in _FORCE_SEARCH_TYPES):
        return "search", f"高风险领域强制搜索: {query_type}"

    # 时效性信息（天气已在 tool 里处理，这里处理未明确归类的时效查询）
    if query_type == "天气" and suggested_tool not in _DIRECT_TOOL_MAP:
        return "search", "天气查询走搜索确认"

    # 闲聊 / 系统查询
    if query_type in _SKIP_SEARCH_TYPES:
        return "fast", f"无需搜索: {query_type}"

    # 消歧明确说不需要搜索
    if not needs_search:
        return "fast", "消歧判定无需搜索"

    # 默认走搜索
    return "search", "消歧判定需要搜索"
