#!/usr/bin/env python3
"""User-visible reply cleanup helpers."""
from __future__ import annotations

import re

_SOURCE_MARK_RE = re.compile(r"\s*(?:\[来源\d+\]|【来源\d+】)")


def clean_reply_for_user(reply: str) -> str:
    """Remove internal source markers from ordinary user-facing replies."""
    text = _SOURCE_MARK_RE.sub("", reply or "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
