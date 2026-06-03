#!/usr/bin/env python3
"""agents/curator.py — 素材筛选 AI

职责（只做这一件事）：
  输入：所有原始 Source 列表 + 用户问题
  输出：经过打分、筛选、排序的 top-N Source 列表，带 target_words 建议

不做的事：
  - 不做搜索
  - 不写正文
  - 不核查事实

设计原则：
  如果 source_utils.score_source 已经能排出好序，就直接用（无 LLM 调用）。
  只有当 sources 数量很多（>8）且分布复杂时，才调用轻量 AI 辅助判断。
"""
from __future__ import annotations
import logging
import re
from typing import Optional

from tg_bot.core.contracts import Source, WriteRequest
from tg_bot.workers.source_utils import (
    score_source, deduplicate, is_nav_or_empty, format_sources_for_writer,
)

log = logging.getLogger(__name__)

# 每个档位的字数区间 (min, max)
_LENGTH_MAP = {
    "short":    (80,   200),
    "medium":   (250,  500),
    "long":     (600,  1200),
    "detailed": (1200, 2500),
}

# 每个档位对应的 max_tokens
_TOKEN_MAP = {
    "short":    1200,
    "medium":   2500,
    "long":     4500,
    "detailed": 7000,
}

_USER_LENGTH_RE = re.compile(
    r"(?:写|给我|帮我|生成|输出|整理|介绍|说明).{0,8}?(\d{2,4})\s*(?:字|个字|个汉字)"
)
_BRIEF_HINT_RE = re.compile(r"(简短|简要|一句话|短一点|短点|简单说|简单介绍)")
_LONG_HINT_RE = re.compile(r"(详细|展开|具体|完整|详尽|尽量长|长一点|越详细越好)")
_SINGLE_ITEM_RE = re.compile(
    r"((搜|查|讲|说|来|给).{0,8}(一个|一条|一则|1个|1条).{0,12}"
    r"(冷知识|事实|趣闻|例子|案例)|"
    r"(一个|一条|一则|1个|1条).{0,8}(冷知识|事实|趣闻|例子|案例))"
)


def _extract_length_constraint(user_query: str) -> tuple[int, int] | None:
    """用户显式字数要求优先于自动档位。"""
    q = user_query or ""
    m = _USER_LENGTH_RE.search(q)
    if m:
        n = int(m.group(1))
        return (max(int(n * 0.85), 20), max(int(n * 1.15), 40))
    if _BRIEF_HINT_RE.search(q):
        return (30, 120)
    if _LONG_HINT_RE.search(q):
        return (500, 1200)
    return None


def curate(
    sources: list[Source],
    user_query: str,
    keywords: list[str],
    suggested_length: str = "",
    history_context: Optional[list[dict]] = None,
    max_sources: int = 8,
) -> WriteRequest:
    """
    筛选、排序 sources，生成 WriteRequest 供 Writer 使用。

    返回 WriteRequest（含 sources 列表、target_words、history_context）。
    """
    # 1. 去重
    deduped = deduplicate(sources)

    # 2. 过滤空页/导航页
    valid = [s for s in deduped if not is_nav_or_empty(s.title, s.body)]

    # 3. 打分
    for s in valid:
        s.score = score_source(s, query=user_query)

    # 4. 排序：score 降序
    ranked = sorted(valid, key=lambda s: s.score, reverse=True)

    # 5. 截取 top-N
    selected = ranked[:max_sources]

    # 6. 确定目标字数
    explicit_words = _extract_length_constraint(user_query)
    sl = (suggested_length or "").strip().lower()
    single_item = bool(_SINGLE_ITEM_RE.search(user_query or ""))

    if explicit_words:
        target_words = explicit_words
    elif single_item:
        target_words = _LENGTH_MAP["short"]
    elif sl in _LENGTH_MAP:
        target_words = _LENGTH_MAP[sl]
    else:
        # 根据 source 数量和质量自动推断
        has_full_content = any(len(s.full_content) > 500 for s in selected)
        n = len(selected)
        if n <= 1:
            target_words = _LENGTH_MAP["short"]
        elif n <= 3 and not has_full_content:
            target_words = _LENGTH_MAP["medium"]
        elif n <= 5:
            target_words = _LENGTH_MAP["long"]
        else:
            target_words = _LENGTH_MAP["detailed"]

    # 7. 推断 style_hints
    style_hints = []
    q = (user_query or "").lower()
    if any(k in q for k in ("介绍", "简介", "学校", "大学", "公司", "机构")):
        style_hints.append("intro_style")
    if any(k in q for k in ("怎么", "如何", "措施", "建议", "应对")):
        style_hints.append("how_to_style")
    if single_item:
        style_hints.append("single_item")

    log.info(
        f"📋 Curator: {len(sources)} 条原始 → {len(deduped)} 去重 → {len(valid)} 有效 → "
        f"选取 {len(selected)} 条，target_words={target_words}, style={style_hints}"
    )

    return WriteRequest(
        user_query=user_query,
        sources=selected,
        target_words=target_words,
        history_context=history_context or [],
        style_hints=style_hints,
    )


def target_words_to_max_tokens(target_words: tuple[int, int]) -> int:
    """根据目标字数推算 max_tokens（中文 1 字 ≈ 1.5 token + thinking budget）。"""
    _, hi = target_words
    # thinking budget 固定 500；1 中文字 ≈ 1.5 token；留 20% 余量
    return min(max(int(hi * 1.8) + 500, 1500), 7000)
