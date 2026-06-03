"""Execution lanes for tg_bot."""

from tg_bot.lanes.router import LaneDecision, decide_lane

__all__ = ["LaneDecision", "decide_lane"]
