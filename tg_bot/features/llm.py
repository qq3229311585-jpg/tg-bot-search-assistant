#!/usr/bin/env python3
"""features/llm.py — 新功能专用的“便宜”模型调用：关闭思考、限制输出长度。"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def cheap_chat(user: str, system: str, *, max_tokens: int = 600, temperature: float = 0.7,
               json_mode: bool = False, stage: str = "feature") -> str:
    import tg_bot.config as _cfg
    from tg_bot.tools.fetch import http_post
    from tg_bot.usage import llm_stage

    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "thinking": {"type": "disabled"},
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        with llm_stage(stage):
            resp = http_post(
                "https://api.deepseek.com/chat/completions",
                payload,
                headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_KEY}"},
                timeout=60,
            )
        if resp and resp.get("choices"):
            return (resp["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:
        log.warning("cheap_chat failed: %s", exc)
    return ""
