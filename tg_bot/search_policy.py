#!/usr/bin/env python3
"""search_policy.py — code-level routing policy for search vs fast path."""

import re


_META_TOOL_RE = re.compile(
    r"(你.*(查了没|查过没|查没查|搜了没|搜过没|有没有搜|有没有查|用了什么工具|"
    r"哪来的|来源|出处|依据|怎么知道|从哪)|"
    r"(刚才|上条|那条|前面).*(查|搜|来源|工具|依据)|"
    r"(哪句话|哪些话).*(原文|你自己|提炼))"
)

_SOCIAL_RE = re.compile(
    r"^(谢谢|谢了|好的|好|嗯|哦|哈哈|没事|不用了|再见|bye|拜拜|辛苦了|测试结束|结束了|收工)[。.!！\s]*$",
    re.IGNORECASE,
)

_EMOTION_RE = re.compile(
    r"(难过|伤心|开心|高兴|郁闷|烦死|好烦|好累|崩溃|焦虑|emo|想哭|无聊|"
    r"想你|爱你|喜欢你|讨厌|生气|气死|无奈|好惨|心累|压力大|"
    r"我感觉|我觉得很|我想说|我希望|我打算|我害怕|我担心)"
)

_FIRST_PERSON_FEELING_RE = re.compile(
    r"^(我|今天我|我今天).{0,12}(很|真|好|挺|超|太|有点|有些|实在).{0,8}"
    r"(难过|累|烦|开心|高兴|伤心|郁闷|崩溃|焦虑|无聊|委屈|失落|兴奋|激动)"
)

_ASSISTANT_STATE_RE = re.compile(
    r"^(你|小助|助手).{0,12}(今天|现在|最近|刚刚)?.{0,8}"
    r"(心情|感觉|状态|过得|怎么样|如何|好吗|开心|难过|累不累|忙不忙)"
)

_ASSISTANT_CONTINUE_RE = re.compile(
    r"^(你讲吧|你说吧|你来讲|你来吧|讲吧|说吧|你决定|你看着办|你安排|你推荐|陪我聊聊|安慰我一下)[。.!！\s]*$"
)

_REWRITE_RE = re.compile(
    r"(润色|改写|翻译|总结一下|概括|压缩|扩写|换个说法|大白话|整理成|写成|检查错别字)"
)

_EXPLICIT_SEARCH_RE = re.compile(
    r"(搜一下|搜索|查一下|查查|查一查|帮我查|联网|网上找|找找资料|再查|再搜|重新搜|"
    r"来源呢|出处呢|依据呢|靠谱不|可靠吗|准不准|真的假的|你怎么知道|从哪来的)"
)

_FOLLOWUP_TOO_SHORT_RE = re.compile(
    r"(这么少|太少了|好少|就这|就这么点|不够详细|太简单|太短了|再详细|能详细|详细一点|多说一点|说多点)"
)

_FRESH_RE = re.compile(
    r"(今天|现在|刚刚|最新|最近|新闻|动态|价格|股价|行情|汇率|政策|法规|公告|更新|"
    r"版本|发布|榜单|排名|推荐|评测|论文|研究|数据|统计|预测)"
)

_HIGH_STAKES_RE = re.compile(
    r"(金融|投资|股票|基金|期货|保险|贷款|税务|法律|合同|诉讼|合规|政策|医疗|"
    r"医学|药|症状|诊断|治疗|心理|抑郁|焦虑)"
)

_KNOWLEDGE_RE = re.compile(
    r"(什么是|是啥|啥是|是什么|定义|概念|原理|机制|结论|理论|模型|历史|起源|"
    r"著名|实用|应用|例子|案例|优缺点|区别|影响|为什么|怎么理解|如何看待|"
    r"学科|行为|心理学|经济学|金融学|计算机|算法|物理|化学|生物|哲学)"
)

_STOPWORDS_RE = re.compile(
    r"(帮我|请问|你觉得|我想知道|这个|那个|一下|一些|有什么|有啥|比较|最好|重点|"
    r"讲讲|说说|解释|介绍|作者|一笔带过|今天|现在|最近|最新|新闻|动态|学科|有名|"
    r"靠谱|可靠|真的假的|吗|呢|吧|啊|呀|的|了|和|或|与|以及)"
)
_SPLIT_RE = re.compile(r"[，。？！,.?!；;：:\s「」『』【】（）()]+")


def _base_route_from_pre(pre):
    if pre is None:
        return True
    return bool(pre.get("needs_search", False))


def _keywords_hint(text, pre, category):
    hints = []
    for kw in (pre or {}).get("keywords") or []:
        kw = str(kw).strip()
        if len(kw) >= 2 and kw not in hints:
            hints.append(kw[:40])

    cleaned = _STOPWORDS_RE.sub(" ", text or "")
    parts = [p.strip() for p in _SPLIT_RE.split(cleaned) if len(p.strip()) >= 2]
    for p in parts:
        if p not in hints:
            hints.append(p[:40])

    if category == "时效/数据":
        if re.search(r"\bAI\b|人工智能|大模型|模型", text or "", re.I):
            hints.insert(0, "AI 新闻")
        elif not hints:
            hints.append((text or "")[:40])
    elif category == "高风险领域":
        for word in ("金融", "投资", "法律", "医疗", "医学", "政策", "心理"):
            if word in (text or "") and word not in hints:
                hints.append(word)

    return hints[:6]


def _last_toollog_hints(last_toollog):
    hints = []
    if not last_toollog:
        return hints

    pre_searched = str(last_toollog.get("pre_searched") or "").strip()
    if pre_searched:
        cleaned = pre_searched
        for prefix in ("缓存:", "wiki:"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
        for part in re.split(r"[，,、\s]+", cleaned):
            part = part.strip()
            if len(part) >= 2 and "http" not in part.lower() and part not in hints:
                hints.append(part[:40])

    route_info = last_toollog.get("route_info") or {}
    for kw in route_info.get("keywords_hint") or []:
        kw = str(kw).strip()
        if len(kw) >= 2 and "http" not in kw.lower() and kw not in hints:
            hints.append(kw[:40])

    for tool in last_toollog.get("model_tools") or []:
        tool = str(tool)
        m = re.search(r"\(([^()]*)\)", tool)
        if m:
            inner = m.group(1).strip()
            if inner:
                for part in re.split(r"[，,、\s]+", inner):
                    part = part.strip()
                    if len(part) >= 2 and "http" not in part.lower() and part not in hints:
                        hints.append(part[:40])

    return hints[:6]


def decide_search_policy(text, pre, ctx_turns=None, last_toollog=None):
    """Return a stable, user-readable route decision.

    route is "search" or "fast". This function is intentionally local and
    deterministic so it can correct obvious LLM disambiguation mistakes.
    """
    text = (text or "").strip()
    pre = pre or {}
    query_type = str((pre or {}).get("query_type", "其他"))
    speech_act = str(pre.get("speech_act") or "")
    addressing_assistant = bool(pre.get("addressing_assistant", False))
    needs_external = bool(pre.get("needs_external_evidence", pre.get("needs_search", False)))
    needs_local_tool = bool(pre.get("needs_local_tool", False))
    local_tool_hint = str(pre.get("local_tool_hint") or pre.get("suggested_tool") or "")
    disambig_needs = _base_route_from_pre(pre)
    route = "search"
    reason = "沿用意图消歧路径判断"
    category = query_type or "其他"
    override = False
    keywords_hint = _keywords_hint(text, pre, category)

    if _META_TOOL_RE.search(text):
        route = "fast"
        reason = "用户在追问上一轮来源/工具记录，应读取本地日志，不重新搜索"
        category = "元问题"
        override = disambig_needs
    elif needs_local_tool or query_type == "系统查询":
        route = "search"
        reason = "意图层判断需要调用本地工具"
        category = "系统查询"
        override = not disambig_needs
    elif query_type == "日历":
        route = "search"
        reason = "日历查询/操作，需要调用日历工具"
        category = "日历"
        override = not disambig_needs
    elif _EXPLICIT_SEARCH_RE.search(text):
        route = "search"
        reason = "用户明确要求搜索或质疑准确性"
        category = "明确搜索"
        override = not disambig_needs
    elif (
        speech_act in ("social_chat", "emotion", "meta_question")
        or (speech_act == "continue_previous" and not needs_external)
        or (addressing_assistant and not needs_external)
        or _ASSISTANT_CONTINUE_RE.match(text)
    ):
        route = "fast"
        reason = pre.get("reason") or "意图层判断为社交/情绪/对助手本人说话，不需要外部证据"
        category = "情感闲聊" if speech_act == "emotion" else "闲聊"
        override = disambig_needs
    elif _SOCIAL_RE.match(text):
        route = "fast"
        reason = "社交闲聊，不需要联网"
        category = "闲聊"
        override = disambig_needs
    elif _EMOTION_RE.search(text) or _FIRST_PERSON_FEELING_RE.search(text):
        route = "fast"
        reason = "情感/主观表达，不需要搜索"
        category = "情感闲聊"
        override = disambig_needs
    elif _ASSISTANT_STATE_RE.search(text):
        route = "fast"
        reason = "用户在问助手状态/心情，属于社交闲聊"
        category = "闲聊"
        override = disambig_needs
    elif _REWRITE_RE.search(text):
        route = "search"
        reason = "统一走工具路径"
        category = "改写"
        override = not disambig_needs
    elif needs_external:
        route = "search"
        reason = pre.get("reason") or "意图层判断需要外部证据"
        category = query_type or "搜索"
        override = not disambig_needs
    elif _FRESH_RE.search(text):
        route = "search"
        reason = "问题包含时效信息或数据，需要搜索核实"
        category = "时效/数据"
        override = not disambig_needs
    elif _HIGH_STAKES_RE.search(text):
        route = "search"
        reason = "金融/法律/医疗等高风险领域，默认搜索核实"
        category = "高风险领域"
        override = not disambig_needs
    elif _KNOWLEDGE_RE.search(text):
        route = "search"
        reason = "专业概念、原理或实用结论，默认搜索核实"
        category = "知识/原理"
        override = not disambig_needs

    if (
        _FOLLOWUP_TOO_SHORT_RE.search(text)
        and last_toollog
        and (last_toollog.get("route_info") or {}).get("route") == "search"
        and category != "元问题"
    ):
        route = "search"
        reason = "用户反馈上一轮回复内容不足，继承搜索路径重新回答"
        category = "上下文追问"
        override = True
        inherited = _last_toollog_hints(last_toollog)
        if inherited:
            keywords_hint = inherited

    return {
        "route": route,
        "reason": reason,
        "category": category,
        "disambig_needs_search": disambig_needs,
        "policy_override": bool(override),
        "keywords_hint": keywords_hint,
        "speech_act": speech_act,
        "needs_external_evidence": needs_external,
        "needs_local_tool": needs_local_tool,
        "local_tool_hint": local_tool_hint,
        "addressing_assistant": addressing_assistant,
    }
