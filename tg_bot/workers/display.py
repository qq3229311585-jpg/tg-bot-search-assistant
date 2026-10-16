#!/usr/bin/env python3
"""User-visible reply cleanup helpers."""
from __future__ import annotations

import re

_SOURCE_MARK_RE = re.compile(r"\s*(?:\[来源\d+\]|【来源\d+】)")


_TRAILING_SOURCES_RE = re.compile(
    r"\n(?:📎|📚|🔗)?\s*[【\[]?(?:来源|参考来源|参考资料|资料来源)[】\]]?\s*[:：]?\s*\n"
    r"(?:.*\n?)*$"
)


def clean_reply_for_user(reply: str) -> str:
    """Remove internal source markers from ordinary user-facing replies."""
    text = _SOURCE_MARK_RE.sub("", reply or "")
    # 剥掉正文结尾的来源列表段（来源详情由系统折叠附加，不在正文里堆链接）
    text = _TRAILING_SOURCES_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
