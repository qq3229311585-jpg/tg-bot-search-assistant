#!/usr/bin/env python3
"""Lane router for high-level bot execution paths.

The lane layer is intentionally small: it decides which executor should own
the turn, while bot.py keeps the existing implementation details.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LaneName = Literal["fast", "search", "local_tool", "report", "system"]


@dataclass(frozen=True)
class LaneDecision:
    name: LaneName
    reason: str
    evidence_kind: str = ""


def decide_lane(
    *,
    needs_search: bool,
    route_info: dict | None = None,
    pre: dict | None = None,
    local_evidence_kind: str = "",
) -> LaneDecision:
    """Return the single lane responsible for the current turn."""
    route_info = route_info or {}
    pre = pre or {}
    category = str(route_info.get("category") or pre.get("query_type") or "")
    query_type = str(pre.get("query_type") or "")

    if local_evidence_kind == "vps_traffic":
        return LaneDecision("local_tool", "本地 VPS 流量工具提供证据", local_evidence_kind)
    if local_evidence_kind == "today_report":
        return LaneDecision("report", "读取今日午报作为本地证据", local_evidence_kind)
    if category in ("系统查询", "日历") or query_type in ("系统查询", "日历"):
        return LaneDecision("system", "系统/日历类请求", local_evidence_kind)
    if needs_search:
        return LaneDecision("search", "需要搜索或外部证据")
    return LaneDecision("fast", "纯模型快速回复")
