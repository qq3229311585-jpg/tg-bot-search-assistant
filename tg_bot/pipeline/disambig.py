#!/usr/bin/env python3
"""pipeline/disambig.py — 第一层：意图消歧"""

import json, re, logging
from datetime import datetime, timezone, timedelta

from tg_bot.prompts import _SYS_DISAMBIG
from tg_bot.tools.fetch import http_post

log = logging.getLogger(__name__)

# ── 消歧模型完全不可用时的兜底：代码规则粗判，好过直接默认搜索 ──────────────
_HEURISTIC_SOCIAL_RE = re.compile(
    r"^(谢谢|谢了|多谢|好的|好|嗯|嗯嗯|哦|哈哈|哈哈哈|ok|okay|收到|没事|晚安|早|"
    r"早上好|辛苦了|行|可以|知道了|算了|嗯好|好嘞|明白了|了解|收到了|拜拜|再见)"
    r"[呀啊哦呢吧啦~！!。.，,\s]*$",
    re.IGNORECASE,
)
_HEURISTIC_META_RE = re.compile(
    r"(你.*(查了没|查过没|查没查|搜了没|搜过没|有没有搜|有没有查|用了什么工具|"
    r"哪来的|来源|出处|依据|怎么知道|从哪)|"
    r"(刚才|上条|那条|前面).*(查|搜|来源|工具|依据))"
)


def _heuristic_pre(text: str, ctx=None) -> dict:
    """消歧模型 JSON 解析彻底失败时的代码兜底判断。

    不再直接返回 None 让流程默认搜索——那样会把"谢谢""好的"这类闲聊也当成
    搜索请求处理。规则简单但保守：明确的社交/元问题短句才判闲聊，其余一律
    按需要搜索处理，keywords 用 query_signals 兜底。
    """
    from tg_bot.workers.source_utils import query_signals

    stripped = (text or "").strip()

    if _HEURISTIC_SOCIAL_RE.match(stripped):
        return {
            "clear": True, "query_type": "闲聊", "keywords": [],
            "needs_search": False, "speech_act": "social_chat",
            "addressing_assistant": False, "needs_external_evidence": False,
            "needs_local_tool": False, "local_tool_hint": "",
            "topic_continuity": "none", "user_intent": "社交闲聊/确认",
            "confidence": 0.5, "reason": "消歧模型不可用，代码规则识别为社交短句",
            "clarify_question": "", "retry_hint": False, "prev_searches": [],
            "focus_action": "none", "goal": "", "missing_slot": "",
            "user_deferred": False, "topic_anchor": [], "suggested_tool": "",
            "suggested_length": "", "heuristic": True,
        }

    if _HEURISTIC_META_RE.search(stripped):
        return {
            "clear": True, "query_type": "闲聊", "keywords": [],
            "needs_search": False, "speech_act": "meta_question",
            "addressing_assistant": True, "needs_external_evidence": False,
            "needs_local_tool": False, "local_tool_hint": "",
            "topic_continuity": "none", "user_intent": "追问助手自身行为",
            "confidence": 0.5, "reason": "消歧模型不可用，代码规则识别为元问题",
            "clarify_question": "", "retry_hint": False, "prev_searches": [],
            "focus_action": "none", "goal": "", "missing_slot": "",
            "user_deferred": False, "topic_anchor": [], "suggested_tool": "",
            "suggested_length": "", "heuristic": True,
        }

    keywords = query_signals(stripped)[:5]
    return {
        "clear": True, "query_type": "其他", "keywords": keywords,
        "needs_search": True, "speech_act": "factual_question",
        "addressing_assistant": False, "needs_external_evidence": True,
        "needs_local_tool": False, "local_tool_hint": "",
        "topic_continuity": "none", "user_intent": stripped[:40],
        "confidence": 0.3, "reason": "消歧模型不可用，代码规则默认按搜索处理",
        "clarify_question": "", "retry_hint": False, "prev_searches": [],
        "focus_action": "none", "goal": stripped[:80], "missing_slot": "",
        "user_deferred": False, "topic_anchor": keywords, "suggested_tool": "web_search",
        "suggested_length": "", "heuristic": True,
    }


def _load_features_brief():
    """从 features.md 提取功能名（· 开头行），拼成一行供 disambig 注入"""
    try:
        import os
        path = "/var/lib/morning-report/features.md"
        lines = open(path, encoding="utf-8").readlines()
        names = []
        for ln in lines:
            ln = ln.strip()
            if ln.startswith("· "):
                # 取第一个冒号或逗号前的功能名
                name = ln[2:].split("：")[0].split("，")[0].strip()
                if name:
                    names.append(name)
        return "、".join(names) if names else ""
    except Exception:
        return ""


def _pre_check(text, ctx=None, focus=None):
    """
    第一层：意图消歧。
    ctx: load_context() 返回的最近几轮摘要列表，用于理解省略句和追问。
    返回 dict {clear, query_type, keywords, needs_search, clarify_question,
               retry_hint, prev_searches} 或 None。
    thinking disabled：固定 JSON 输出，对错一眼看出，不需要推理记录。
    """
    import tg_bot.config as _cfg

    # ── 代码级催促重试拦截（比 AI 判断更可靠）────────────────────────────
    _RETRY_PATTERNS = re.compile(
        r"^(再想想|再查|再查一下|再查查|再找找|再找一下|重新搜|你再试试|再看看|再搜|再搜一下|继续找|继续查|再试试)$"
    )
    if _RETRY_PATTERNS.match(text.strip()):
        _prev_kw = []
        _prev_type = "搜索"
        _prev_searches = []  # 上轮实际搜过的查询词

        # ① 优先从 tool_log 读取上轮实际搜索记录（最可靠）
        try:
            from tg_bot.storage import load_toollog
            tl = load_toollog()
            if tl:
                last = tl[-1]
                # pre_searched 记录了代码层预查/缓存词
                if last.get("pre_searched"):
                    ps = last["pre_searched"]
                    for _prefix in ("缓存:", "wiki:"):
                        if ps.startswith(_prefix):
                            ps = ps[len(_prefix):]
                            break
                    _prev_searches = [s.strip() for s in ps.split(",") if s.strip()]
                # model_tools 记录了 AI 自行调用的工具名
                mt = last.get("model_tools") or []
                # user 字段记录了上轮用户问题——用作关键词来源
                _u = last.get("user", "")
                if _u:
                    _prev_kw = [w for w in re.split(r"[，。？！,.?!\s]+", _u) if len(w) >= 2][:5]
                if "历史" in (last.get("reply_preview") or "") or "记录" in (last.get("reply_preview") or ""):
                    _prev_type = "历史查询"
        except Exception as _e:
            log.debug(f"retry: tool_log 读取失败: {_e}")

        # ② 如果 tool_log 没拿到关键词，降级从 context 文本提取
        if not _prev_kw and ctx:
            for _t in reversed(ctx):
                _u = _t.get("user", "")
                _a = _t.get("assistant", "")
                if _u:
                    _prev_kw = [w for w in re.split(r"[，。？！,.?!\s]+", _u) if len(w) >= 2][:5]
                    if "历史" in _a or "记录" in _a or "聊过" in _u:
                        _prev_type = "历史查询"
                    break

        log.info(f"🔁 代码级催促拦截: retry → type={_prev_type} kw={_prev_kw} prev_searches={_prev_searches}")
        return {
            "clear": True,
            "query_type": _prev_type,
            "keywords": _prev_kw,
            "needs_search": True,
            "speech_act": "continue_previous",
            "addressing_assistant": False,
            "needs_external_evidence": True,
            "needs_local_tool": False,
            "local_tool_hint": "",
            "topic_continuity": "continue_same_topic",
            "user_intent": "用户要求继续围绕上一轮话题搜索",
            "confidence": 1.0,
            "reason": "代码级催促重试命中",
            "clarify_question": "",
            "retry_hint": True,          # ← 新增：告知 gather AI 这是重试
            "prev_searches": _prev_searches,  # ← 新增：上轮实际搜过的词
        }
    # ─────────────────────────────────────────────────────────────────────

    # 拼上文前缀
    if ctx:
        import time as _time
        _now = _time.time()
        ctx_lines = ["[最近对话]"]
        for turn in ctx:
            ts = turn.get("ts")
            if ts:
                diff_min = int((_now - ts) / 60)
                if diff_min < 60:
                    label = f"[{diff_min}分钟前]"
                else:
                    label = f"[{diff_min // 60}小时{diff_min % 60}分钟前]"
            else:
                label = "[时间未知]"
            ctx_lines.append(f"{label} 用户：{turn['user']}")
            ctx_lines.append(f"{label} 助手：{turn['assistant']}")
        ctx_lines.append("")
        ctx_lines.append("[当前消息]")
        user_content = "\n".join(ctx_lines) + "\n" + text
    else:
        user_content = text

    # ── 注入当前挂起意图（若有）──────────────────────────────────────
    if focus and focus.get("active"):
        import time as _ft
        _elapsed = int((_ft.time() - (focus.get("created_ts") or _ft.time())) / 60)
        _focus_block = (
            "[当前挂起意图]\n"
            f"目标：{focus.get('goal', '')}\n"
            f"还缺：{focus.get('missing_slot', '（未知）')}\n"
            f"已反问次数：{focus.get('clarify_count', 0)}\n"
            f"话题锚点：{', '.join(focus.get('topic_anchor') or [])}\n"
        )
        user_content = _focus_block + "\n" + user_content
    # ─────────────────────────────────────────────────────────────────

        # 注入 bot 功能清单到 disambig 系统提示（让 AI 识别"午报"等功能词）
    _features_brief = _load_features_brief()
    _features_note = (
        f"\n\n【本 bot 已有功能（仅供参考，识别系统查询用）】\n{_features_brief}\n"
        "凡问及以上任一功能本身（如几点发、有没有、怎么查），均视为 query_type='系统查询'，needs_search=false。"
    ) if _features_brief else ""

    try:
        resp = http_post(
            "https://api.deepseek.com/chat/completions",
            {"model": "deepseek-v4-flash",
             "messages": [
                 {"role": "system", "content": _SYS_DISAMBIG + _features_note + f"\n\n【当前日期】今天是 {datetime.now(timezone(timedelta(hours=8))).strftime('%Y年%m月%d日')}，提取关键词时如需年份请用此日期。"},
                 {"role": "user",   "content": user_content}
             ],
             "max_tokens": 600,
             "response_format": {"type": "json_object"},
             "thinking": {"type": "disabled"}},  # 固定 JSON，对错一眼看出
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_KEY}"},
            timeout=20
        )
        if not resp or not resp.get("choices"):
            log.warning("_pre_check: 空响应，转启发式兜底")
            return _heuristic_pre(text, ctx)
        out = (resp["choices"][0]["message"].get("content") or "").strip()
        # 提取 JSON（模型可能在 ```json ... ``` 里）
        m = re.search(r'\{.*\}', out, re.DOTALL)
        if not m:
            log.warning(f"_pre_check: 未找到 JSON，原始输出前300字: {out[:300]!r}")
            return _heuristic_pre(text, ctx)
        try:
            data = json.loads(m.group(0))
        except Exception as _je:
            log.warning(f"_pre_check: JSON 解析失败({_je})，原始输出前300字: {out[:300]!r}")
            return _heuristic_pre(text, ctx)
        result = {
            "clear":            bool(data.get("clear", True)),
            "query_type":       str(data.get("query_type", "其他")),
            "keywords":         data.get("keywords", []),
            "needs_search":     bool(data.get("needs_search", False)),
            "speech_act":       str(data.get("speech_act", "")),
            "addressing_assistant": bool(data.get("addressing_assistant", False)),
            "needs_external_evidence": bool(data.get("needs_external_evidence", data.get("needs_search", False))),
            "needs_local_tool":  bool(data.get("needs_local_tool", False)),
            "local_tool_hint":   str(data.get("local_tool_hint", "")),
            "topic_continuity":  str(data.get("topic_continuity", "none")),
            "user_intent":      str(data.get("user_intent", "")),
            "confidence":       float(data.get("confidence", 0.0) or 0.0),
            "reason":           str(data.get("reason", "")),
            "clarify_question": str(data.get("clarify_question", "")),
            "retry_hint":       False,
            "prev_searches":    [],
            # 焦点字段
            "focus_action":     str(data.get("focus_action", "none")),
            "goal":             str(data.get("goal", "")),
            "missing_slot":     str(data.get("missing_slot", "")),
            "user_deferred":    bool(data.get("user_deferred", False)),
            "topic_anchor":     data.get("topic_anchor") or [],
            "suggested_tool":   str(data.get("suggested_tool", "")),
            "suggested_length": str(data.get("suggested_length", "")),
        }
        log.info(f"🔍 意图消歧: clear={result['clear']} type={result['query_type']} "
                 f"speech={result.get('speech_act','')} external={result.get('needs_external_evidence')} "
                 f"search={result['needs_search']} kw={result['keywords']}")
        return result
    except Exception as e:
        log.warning(f"_pre_check 异常: {e}，转启发式兜底")
        return _heuristic_pre(text, ctx)
