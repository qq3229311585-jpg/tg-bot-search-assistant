#!/usr/bin/env python3
"""commands/quota.py — /quota /balance 命令处理函数"""

import logging

from tg_bot.bot_utils import send
from tg_bot.storage import load_quota, save_quota, fmt_quota
from tg_bot.tools.native import execute_api_balance

log = logging.getLogger(__name__)


def handle_balance(chat_id):
    send(chat_id, "🔑 查询中，稍等…")
    send(chat_id, execute_api_balance())


def handle_quota(chat_id):
    send(chat_id, fmt_quota())


def handle_quota_set(chat_id, text):
    import tg_bot.config as _cfg
    parts_qs = text.split()
    _VALID_APIS = {
        "tavily": ["tavily_0", "tavily_1"],
        "tavily0": ["tavily_0"], "tavily_0": ["tavily_0"],
        "tavily1": ["tavily_1"], "tavily_1": ["tavily_1"],
        "brave":  ["brave"],
        "serper": ["serper"],
    }
    if len(parts_qs) == 4:
        # /quota set <api> <n>
        api_arg = parts_qs[2].lower()
        val_arg = parts_qs[3]
        if api_arg not in _VALID_APIS:
            send(chat_id,
                 f"未知 API：{api_arg}\n"
                 f"可用：tavily / tavily0 / tavily1 / brave / serper")
        else:
            try:
                n = int(val_arg)
                if n < 1:
                    raise ValueError("上限必须 ≥ 1")
                from tg_bot.config import _save_api_limit
                for k in _VALID_APIS[api_arg]:
                    _save_api_limit(k, n)
                    _cfg.API_FREE_LIMITS[k] = n
                names = " / ".join(_VALID_APIS[api_arg])
                send(chat_id, f"✅ {names} 上限已设为 {n} 次")
            except Exception as e:
                send(chat_id, f"用法：/quota set brave 1000\n{e}")
    elif len(parts_qs) == 3:
        # /quota set <n>  → 预警阈值
        try:
            pct = int(parts_qs[2])
            if not 1 <= pct <= 99:
                raise ValueError("范围 1-99")
            d = load_quota()
            d["warn_pct"] = pct
            d["warned"]   = {}
            save_quota(d)
            send(chat_id, f"✅ 配额预警阈值已设为 {pct}%")
        except Exception as e:
            send(chat_id, f"用法：/quota set 70（预警阈值，1-99）\n{e}")
    else:
        send(chat_id,
             "用法：\n"
             "• /quota set 70 — 设置预警阈值（百分比）\n"
             "• /quota set brave 1000 — 设置某 API 上限\n"
             "• /quota set tavily 1000 — 同时设置两个 Tavily key\n"
             "• /quota set serper 7500 — 设置 Serper 总额")
