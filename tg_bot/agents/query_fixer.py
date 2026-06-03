#!/usr/bin/env python3
"""agents/query_fixer.py — 查询改写 AI

职责（只做这一件事）：
  输入：用户原始问题 + 关键词
  输出：1-3 个搜索查询变体（JSON 数组）

解决的问题：
  - 口误/缩写："纳克基金" → ["纳斯达克 基金", "Nasdaq fund ETF"]
  - 中英桥接："气候变暖影响" → ["global warming impact 2024", "气候变化 影响"]
  - 歧义消除："苹果" → 根据上下文保留或扩展

不做的事：
  - 不做搜索
  - 不做路由决策
  - 不修改 intent / keywords（那是消歧层的事）
"""
from __future__ import annotations
import json
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

_SYS_QUERY_FIXER = """\
你是一个搜索查询改写专家。

输入：用户的原始问题（可能含口误、缩写、模糊表达）
输出：1~3 条改写后的搜索查询，JSON 数组格式

改写规则：
1. 去掉"帮我""查一下""介绍一下"等无效前缀，保留核心实体/事件
2. 修正明显口误或缩写：
   - "纳克基金" → "纳斯达克 基金"（"纳"+"克"是"纳斯达克"的缩写）
   - "BTC今天" → "bitcoin price today"
   - "苹果财报" → "Apple 财报 earnings"
3. 如果查询是中文但内容是国际事件/英文专有名词，补一条英文变体
4. 如果查询模糊，补一条更具体的版本（加年份、范围等）
5. 最多输出 3 条，不要重复含义

输出严格为 JSON 数组，不输出任何其他内容：
["查询1", "查询2", "查询3"]

示例：
输入：纳克基金介绍
输出：["纳斯达克基金 ETF 介绍", "Nasdaq fund ETF 纳指", "纳斯达克100指数基金"]

输入：苹果股价
输出：["Apple AAPL 股价 今日", "苹果公司 AAPL stock price"]

输入：今天天气怎么样
输出：["安阳天气"]

注意：天气、日历、VPS流量等本地工具查询，只输出简短精准的关键词，不要加英文。
"""


def fix_query(user_text: str, keywords: list[str],
              context_summary: str = "") -> list[str]:
    """
    把原始问题改写成 1-3 个搜索查询变体。
    失败时返回 [user_text] 作为兜底。
    """
    import tg_bot.config as _cfg
    from tg_bot.tools.fetch import http_post

    kw_hint = "、".join(keywords) if keywords else ""
    user_content = f"原始问题：{user_text}"
    if kw_hint:
        user_content += f"\n已提取关键词：{kw_hint}"
    if context_summary:
        user_content += f"\n上下文摘要：{context_summary}"

    try:
        resp = http_post(
            "https://api.deepseek.com/chat/completions",
            {
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": _SYS_QUERY_FIXER},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 200,
                "temperature": 0.2,
                "thinking": {"type": "disabled"},
            },
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_KEY}"},
            timeout=15,
        )
        if not resp or not resp.get("choices"):
            return [user_text]

        raw = (resp["choices"][0]["message"].get("content") or "").strip()
        m = re.search(r"\[.*?\]", raw, re.DOTALL)
        if not m:
            return [user_text]

        variants = json.loads(m.group(0))
        if not isinstance(variants, list):
            return [user_text]

        cleaned = [v.strip() for v in variants if isinstance(v, str) and v.strip()]
        if not cleaned:
            return [user_text]

        log.info(f"🔧 QueryFixer: {user_text[:30]!r} → {cleaned}")
        return cleaned[:3]

    except Exception as e:
        log.warning(f"query_fixer 失败: {e}")
        return [user_text]
