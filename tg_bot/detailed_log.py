#!/usr/bin/env python3
"""detailed_log.py — 每次对话的完整过程记录"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

_BJ = timezone(timedelta(hours=8))
_LOG_DIR = "/var/lib/morning-report/detailed_logs"
os.makedirs(_LOG_DIR, exist_ok=True)


def save_detailed_log(
    user_text: str,
    meta: dict,
    write_reply: str,
    write_reasoning: str,
    verifier_result: dict,
    source_index: list,
    suggested_length: str = "",
    route_info: dict = None,
    pre: dict = None,
):
    """
    把一次完整对话的所有过程保存到 detailed_logs/YYYYMMDD.jsonl。
    每行是一条独立 JSON，方便 grep 和读取。
    """
    now = datetime.now(tz=_BJ)
    date_str = now.strftime("%Y%m%d")
    ts_str   = now.strftime("%Y-%m-%d %H:%M:%S")

    # gather_ai 的每一轮思考和工具调用
    gather_rounds = []
    for r in (meta.get("rounds") or []):
        gather_rounds.append({
            "round":      r.get("round"),
            "thinking":   r.get("reasoning", ""),   # 完整 thinking
            "tool_calls": r.get("tool_calls", []),
        })

    # 工具调用结果（完整内容）
    tool_results_full = []
    for tr in (meta.get("tool_results") or []):
        tool_results_full.append({
            "tool":    tr.get("tool"),
            "query":   tr.get("query", ""),
            "content": tr.get("snippet", ""),  # 保存完整 snippet
        })

    # source_index 每条（含 full_content）
    sources_full = []
    for s in (source_index or []):
        sources_full.append({
            "domain": s.get("domain", ""),
            "title":  s.get("title", ""),
            "url":    s.get("url", ""),
            "tool":   s.get("tool", ""),
            "snippet_len":      len(s.get("snippet") or ""),
            "full_content_len": len(s.get("full_content") or ""),
            "full_content":     (s.get("full_content") or s.get("snippet") or ""),
        })

    record = {
        "ts":               ts_str,
        "user":             user_text,

        # 意图消歧
        "disambig": {
            "query_type":     (pre or {}).get("query_type", ""),
            "keywords":       (pre or {}).get("keywords", []),
            "suggested_tool": (pre or {}).get("suggested_tool", ""),
            "focus_action":   (pre or {}).get("focus_action", ""),
        },

        # 路由决策
        "route": {
            "route":   (route_info or {}).get("route", ""),
            "reason":  (route_info or {}).get("reason", ""),
            "category":(route_info or {}).get("category", ""),
        },

        # gather_ai 过程
        "gather": {
            "rounds":          gather_rounds,        # 每轮 thinking + tool_calls
            "tool_results":    tool_results_full,    # 每个工具的完整结果
            "tool_calls_summary": meta.get("tool_calls_summary", []),
            "sufficient":      meta.get("sufficient", True),
            "gather_reason":   meta.get("gather_reason", ""),
            "suggested_length":suggested_length,
            "source_count":    len(source_index or []),
        },

        # 素材（完整内容）
        "sources": sources_full,

        # write_ai 输出
        "write": {
            "reasoning": write_reasoning,   # 完整 thinking
            "reply":     write_reply,
            "reply_len": len(write_reply),
        },

        # verify_ai 结果
        "verify": verifier_result or {},
    }

    path = os.path.join(_LOG_DIR, f"{date_str}.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info(f"📝 详细日志已保存 → {date_str}.jsonl（{len(write_reply)}字回复）")
    except Exception as e:
        log.warning(f"详细日志保存失败: {e}")
