"""pipeline/__init__.py — 统一导出所有流水线函数"""

from tg_bot.pipeline.disambig import _pre_check
from tg_bot.pipeline.gather import (
    gather_ai, ds_chat, fast_chat, build_execution_report, summarize_for_context,
)
from tg_bot.pipeline.write import write_ai
from tg_bot.pipeline.verify import verify_reply, patch_by_verifier, regenerate_reply

__all__ = [
    "_pre_check",
    "gather_ai", "ds_chat", "fast_chat", "build_execution_report", "summarize_for_context",
    "write_ai",
    "verify_reply", "patch_by_verifier", "regenerate_reply",
]
