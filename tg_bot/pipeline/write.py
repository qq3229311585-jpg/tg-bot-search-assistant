#!/usr/bin/env python3
"""pipeline/write.py — 第三层：写作 AI"""

import logging, re

from tg_bot.prompts import _SYS_WRITE
from tg_bot.tools.fetch import http_post

log = logging.getLogger(__name__)

_USER_LENGTH_RE = re.compile(r"(?:[写出给我]+(?:一个|个|一篇|篇)?|请帮我写|帮我写)(\d{2,4})\s*(字|个字|个汉字)")
_BRIEF_HINT_RE = re.compile(r"(简短|简要|一句话|短一点|短点|简单说|简单介绍)")
_LONG_HINT_RE = re.compile(r"(详细|展开|具体|完整|详尽|尽量长|长一点|越详细越好)")


def _extract_length_constraint(user_text):
    """从用户问题中提取字数约束，返回 (min, max) 或 None。"""
    if not user_text:
        return None
    m = _USER_LENGTH_RE.search(user_text)
    if m:
        n = int(m.group(1))
        return int(n * 0.85), int(n * 1.15)
    if _BRIEF_HINT_RE.search(user_text):
        return 30, 120
    if _LONG_HINT_RE.search(user_text):
        return 500, 1200
    return None


def _fmt_evidence_appendix(facts_json, source_index=None, tool_results=None, limit_facts=8, limit_evidence=2):
    facts = (facts_json or {}).get("facts") or []
    if not facts:
        return ""

    lines = ["\n\n【原文证据附录】"]
    count = 0
    for fact in facts:
        fid = fact.get("id", "")
        claim = (fact.get("claim") or "")[:120]
        evs = fact.get("evidence") or []
        if not evs:
            continue
        lines.append(f"[{fid}] {claim}")
        for ev in evs[:limit_evidence]:
            src = ev.get("domain") or ev.get("source_id") or ""
            title = (ev.get("title") or "")[:80]
            excerpt = (ev.get("material_excerpt") or ev.get("quote") or "")[:220].replace("\n", " ")
            if src or title:
                lines.append(f"  - {src}｜{title}")
            if excerpt:
                lines.append(f"    {excerpt}")
        count += 1
        if count >= limit_facts:
            break

    # 补充 tool_results 里的 web_search / serper_search 摘要
    # （这些进了 tool_results 但可能没进 source_index）
    existing_snippets = {e.get("snippet","")[:50] for e in (source_index or [])}
    idx = len(lines) // 3 + 1  # 当前编号接续
    for tr in (tool_results or []):
        if tr.get("tool") not in ("web_search", "serper_search", "wikipedia_lookup"):
            continue
        snippet = (tr.get("snippet") or "").strip()
        if not snippet or snippet[:50] in existing_snippets:
            continue
        existing_snippets.add(snippet[:50])
        lines.append(f"[来源{idx}] 搜索结果（{tr.get('tool','')}）")
        lines.append(snippet[:2000])
        lines.append("")
        idx += 1
    return "\n".join(lines) if count else ""


def _source_priority(entry):
    domain = (entry.get("domain") or "").lower()
    title = (entry.get("title") or "").lower()
    tool = (entry.get("tool") or "").lower()
    body = (entry.get("full_content") or entry.get("snippet") or "").strip()
    score = 0
    if tool in ("fetch_content", "read_today_report"):
        score -= 20
    elif tool in ("wikipedia_lookup", "read_today_cache"):
        score -= 8
    elif tool in ("web_search", "serper_search"):
        score += 30
    if domain.startswith("www.") or domain.endswith(".gov") or domain.endswith(".edu"):
        score -= 12
    if domain in ("wikipedia.org", "en.wikipedia.org", "zh.wikipedia.org"):
        score -= 4
    if "official" in title or "官网" in title or "学校概况" in title or "学校简介" in title:
        score -= 10
    if len(body) < 160:
        score += 3
    return score


def _has_official_source(source_index):
    for entry in source_index or []:
        tool = (entry.get("tool") or "").lower()
        domain = (entry.get("domain") or "").lower()
        title = (entry.get("title") or "").lower()
        if tool in ("fetch_content", "read_today_report") and (
            domain.endswith(".gov") or domain.endswith(".edu") or
            "官网" in title or "official" in title or "学校概况" in title or "学校简介" in title
        ):
            return True
    return False


def _ordered_source_entries(source_index):
    items = list(source_index or [])
    seen = set()
    if _has_official_source(items):
        items = [
            e for e in items
            if (e.get("tool") or "").lower() in ("fetch_content", "read_today_report", "wikipedia_lookup")
            or (e.get("domain") or "").lower().endswith((".gov", ".edu"))
            or any(k in (e.get("title") or "").lower() for k in ("官网", "official", "学校概况", "学校简介"))
        ]
    ordered = []
    for entry in sorted(items, key=_source_priority):
        key = entry.get("url") or entry.get("id") or entry.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(entry)
    return ordered


def _fmt_source_index_brief(source_index, limit_sources=8):
    seen = set()
    lines = []
    for entry in _ordered_source_entries(source_index):
        title = (entry.get("title") or entry.get("domain") or "")[:100]
        domain = (entry.get("domain") or entry.get("tool") or "")[:40]
        body = (entry.get("full_content") or entry.get("snippet") or "").replace("\n", " ").strip()
        if not body:
            continue
        lines.append(f"[Source {len(lines)+1}] {domain}｜{title}: {body[:420]}")
        if len(lines) >= limit_sources:
            break
    return "\n".join(lines)


def _fmt_verifier_instructions(verifier_checks):
    if not verifier_checks:
        return ""
    lines = []
    for c in verifier_checks:
        status = str(c.get("status", "")).upper()
        sentence = (c.get("sentence") or "").strip()
        fact_id = c.get("fact_id")
        reason = (c.get("reason") or "").strip()
        evidence = (c.get("evidence_excerpt") or "").strip()
        if status == "SOFT":
            lines.append(
                f"- 【保留改写】“{sentence[:80]}”\n"
                f"  原文依据：{evidence[:220]}\n"
                f"  说明：{reason}"
            )
        elif status == "HARD":
            lines.append(
                f"- 【删除】“{sentence[:80]}”\n"
                f"  原因：{reason}" + (f"（{fact_id}）" if fact_id not in (None, "") else "")
            )
    return "\n".join(lines)


def _fmt_sources_for_write(source_index, tool_results=None):
    """把 source_index + tool_results 格式化成编号素材列表，供 write_ai 和 verify_ai 使用。"""
    lines = []
    entries = list(source_index or [])
    for i, e in enumerate(entries, 1):
        title  = (e.get("title") or e.get("domain") or "")[:80]
        domain = (e.get("domain") or e.get("tool") or "")[:40]
        body   = (e.get("full_content") or e.get("snippet") or "").replace("\n", " ").strip()[:2500]
        if not body:
            continue
        lines.append(f"[来源{i}] {title}（{domain}）")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def write_ai(user_text, fact_list, history_context=None, reject_feedback="", facts_json=None, suggested_length="",
             source_index=None, verifier_checks=None, tool_results=None):
    """
    第三层：写作 AI。
    无工具权限，只凭事实清单生成自然语言回复。
    thinking enabled：幻觉最可能在这里发生，需要可追溯。
    reject_feedback: verifier 退回时的问题清单，引导重写。
    返回 (reply: str, reasoning: str)
    """
    import tg_bot.config as _cfg
    # ── 用 source_index 构建素材列表（新逻辑，fact_list 保留作兜底）──
    _sources_text = _fmt_sources_for_write(source_index, tool_results=tool_results)
    if _sources_text:
        user_content = (
            f"用户问题：{user_text}\n\n"
            f"原文素材（每条已编号，写作时用 [来源N] 标注出处）：\n{_sources_text}"
        )
    else:
        # 兜底：旧的事实清单方式
        user_content = f"用户问题：{user_text}\n\n事实清单：\n{fact_list}"
    evidence_appendix = _fmt_evidence_appendix(facts_json, source_index=source_index, tool_results=tool_results)
    if evidence_appendix:
        user_content += (
            "\n\n【使用说明】"
            "\n- 事实清单仍是主输入。"
            "\n- 原文证据附录只作为逐条对照，不要把多条证据揉成一个新结论。"
            "\n- 遇到冲突时，优先信任原文证据附录里能直接对应的句子。"
            f"{evidence_appendix}"
        )
    source_brief = _fmt_source_index_brief(source_index, limit_sources=10)
    if source_brief:
        user_content += (
            "\n\n【原始来源节选】"
            "\n- 原始来源优先于 facts_json 摘要。"
            "\n- 如果官网/政府/学校官网正文与 web_search 摘要冲突，直接以正文为准；"
            "搜索摘要只作线索，不得覆盖官网数字。"
            "\n- 如果来源之间冲突，以更权威、更新、官方的来源为准。"
            "\n- 如果来源已经直接给出具体数据，就不要再写“资料不足”。"
            f"\n{source_brief}"
        )
    if reject_feedback:
        source_excerpts = source_brief or _fmt_source_index_brief(source_index, limit_sources=8)
        verifier_block = _fmt_verifier_instructions(verifier_checks)
        user_content += (
            f"\n\n【上一次草稿被核查员退回，以下是具体问题，请对照修正后重新写】\n"
            f"{reject_feedback}\n"
            "重写规则：\n"
            "- [SOFT]：保留信息点，改写措辞贴近原文，不要删除。\n"
            "- [HARD]：删除或替换为“目前资料未明确说明”。\n"
            "- 未被标记的内容：保持原样，不要改动。\n"
            "- 如果官网/政府/学校官网正文与搜索摘要冲突，必须信任正文，忽略旧搜索摘要里的旧数字或旧说法。\n"
            "- 不要因为删句就把答案压缩成提纲；请重新阅读事实清单和原文证据附录，"
            "用仍有依据的内容补足回答。\n"
            "- 如果用户问的是做法、经验、应对措施，按“提前准备 / 发生时 / 户外或车内 / 事后风险”"
            "这类可用分组组织；只能写事实清单或证据附录能直接支撑的动作。\n"
            "- 资料足够时保持实用完整，优先输出 350-700 个汉字；资料不足时明确说不足，"
            "但不要丢掉已经有依据的具体措施。\n"
            "- 纯文本输出，不要使用 Markdown 加粗、标题符号或代码符号。\n"
        )
        if verifier_block:
            user_content += f"\n\n【核查指令】\n{verifier_block}\n"
        if source_excerpts:
            user_content += f"\n\n【可参考的原始资料节选】\n{source_excerpts}\n"
        user_content += "请按重写规则输出修改后的完整答案，直接输出干净正文。"
    fact_count = len(((facts_json or {}).get("facts") or []))
    intro_style = any(k in (user_text or "") for k in (
        "介绍", "简介", "学校", "大学", "学院", "机构", "单位", "公司", "企业", "人物"))
    if fact_count >= 4 and any(k in (user_text or "") for k in (
            "怎么", "如何", "应对", "措施", "经验", "避险", "建议",
            "真的吗", "是不是真的", "是否", "为什么", "介绍", "简介")):
        user_content += (
            "\n\n【长度要求】"
            "这是需要解释的问题，事实已足够，不要压缩成一句概括。"
            "请先给明确结论，再用 4-6 条独立要点解释依据、机制、边界和注意事项；"
            "每条都只使用事实清单和原文证据附录能支撑的内容。"
        )
    if intro_style and fact_count >= 4:
        user_content += (
            "\n\n【简介模板】"
            "这是简介/介绍类问题，事实已足够时，不要只写一两句。"
            "请优先覆盖：地点/性质、历史或身份、规模或层次、学科/专业/特色、一个最突出的细节。"
            "如果资料够，用 5 条要点写；每条都尽量包含一个具体事实，不要只写抽象概括。"
            "目标长度 220-500 个汉字；如果确有资料不足，也要先写已有的具体信息，不要先下“资料不足”的结论。"
        )

    # ── 注入 gather_ai 的字数建议 ────────────────────────────────────
    _len_hint = _extract_length_constraint(user_text)  # 提前到 _sl 之前
    _sl = (suggested_length or "").strip().lower()
    _sl_map = {
        "short":    "建议简短（80～200字）：这是单一事实查询，直接给答案。",
        "medium":   "建议中等篇幅（250～500字）：有几个维度要说清楚，覆盖关键信息。",
        "long":     "建议详细（600～1200字）：问题需要多维度解释，覆盖机制和细节。",
        "detailed": "建议深度展开（1200～2500字）：用户明确要全面介绍，素材丰富，充分利用所有来源。",
    }
    _sl_max_map = {"short": 1200, "medium": 2500, "long": 4500, "detailed": 7000}
    if _sl in _sl_map:
        user_content += f"\n\n【字数参考（采集AI基于素材和问题给出）】\n{_sl_map[_sl]}\n这是参考，你仍需根据实际内容判断，不要硬凑字数也不要无故缩短。"
        if not _len_hint:  # 用户没有明确指定字数时，才用 suggested_length 调整
            _max_tokens = min(_sl_max_map[_sl], 5000)
    if _len_hint:
        lo, hi = _len_hint
        user_content += (
            "\n\n【强制字数约束（最高优先级，覆盖上方所有长度规则）】"
            f"\n用户明确要求约 {lo}-{hi} 个汉字。"
            f"\n不要超过 {hi} 字，也不要低于 {int(lo * 0.8)} 字。"
            "\n字数控制比内容覆盖度更重要。"
        )
        _max_tokens = min(max(int(hi * 2.5), 2200), 4000)  # 至少 2200：thinking budget(1500) + 700 输出
    else:
        _max_tokens = 3500
        user_content += (
            "\n\n【基础长度要求】"
            "\n搜索类问题，除非事实清单确实不足 2 条，否则不要只输出一句话。"
            "\n至少输出 150 字，包含不少于 2 个具体事实（数字、地点、时间等）。"
        )

    msgs_write = []
    if history_context:
        msgs_write.extend(history_context[-6:])  # 最近 3 轮对话给写作 AI 做语境
    msgs_write.append({"role": "user", "content": user_content})

    try:
        resp = http_post(
            "https://api.deepseek.com/chat/completions",
            {"model": "deepseek-v4-flash",
             "messages": [{"role": "system", "content": _SYS_WRITE}] + msgs_write,
             "max_tokens": _max_tokens,
             "temperature": 0.7,
             "thinking": {"type": "enabled", "budget_tokens": 500}},
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_KEY}"},
            timeout=90,
        )
        if not resp or not resp.get("choices"):
            return "写作 AI 无响应，请稍后再试。", ""
        msg = resp["choices"][0]["message"]
        reasoning = (msg.get("reasoning_content") or "").strip()
        reply     = (msg.get("content") or "").strip()
        log.info(f"✍️ 写作完成，{len(reply)} 字")
        return reply, reasoning
    except Exception as e:
        log.warning(f"write_ai 异常: {e}")
        return "生成回复时出错，请稍后再试。", ""
