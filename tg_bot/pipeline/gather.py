#!/usr/bin/env python3
"""pipeline/gather.py — 采集层（gather_ai）+ 核心 AI 调用（ds_chat）+ 辅助函数"""

import json, logging, re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from tg_bot.config import (
    DEEPSEEK_KEY, DEEPSEEK_KEYS, DEEPSEEK_VERIFY_KEY, DEEPSEEK_VERIFY_KEYS,
    _next_ds_key, _next_verify_key, _ctx,
    _DIRECT_API_TOOLS,
)
from tg_bot.prompts import _SYS_GATHER
from tg_bot.tools.definitions import TOOLS
from tg_bot.tools.fetch import http_post
from tg_bot.storage import (
    load_today_index, save_thinking_entry, fmt_toollog_for_prompt,
    update_today_index, NOVELTY_UNTRUSTED_BOUNDARY,
)
from tg_bot.workers.source_utils import (
    cache_match_score as _cache_match_score,
)
from tg_bot.workers.gather_executor import GatherExecContext, execute_gather_tool
from tg_bot.workers.gather_fallback import (
    finalize_gather_sources,
    finalize_round_limit,
    parse_gather_completion,
)
from tg_bot.workers.source_backfill import complete_source_index

log = logging.getLogger(__name__)

_CORRECTIVE_TOOL_NAMES = {"web_search", "serper_search", "fetch_content"}
_CORRECTIVE_TOOLS = [
    tool for tool in TOOLS
    if tool.get("function", {}).get("name") in _CORRECTIVE_TOOL_NAMES
]
_GAP_INSTRUCTION_RE = re.compile(
    r"(?:ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?|"
    r"(?:system|assistant|user)\s*(?:prompt|message|role)|"
    r"role\s*[:=]|调用\s*(?:非搜索)?工具|忽略(?:以上|之前|系统)?指令)", re.I,
)


def _safe_gap_query(value: object) -> str:
    """Keep model-proposed search terms as data, never as instructions."""
    query = " ".join(str(value or "").split())[:180]
    if not query or _GAP_INSTRUCTION_RE.search(query):
        return ""
    return query


def _clean_assistant_msg_for_history(msg):
    """只保留 DeepSeek 支持的 assistant 历史字段。"""
    msg = msg or {}
    clean = {"role": msg.get("role", "assistant")}
    if "content" in msg:
        clean["content"] = msg.get("content")
    if msg.get("reasoning_content"):
        clean["reasoning_content"] = msg.get("reasoning_content")
    if msg.get("tool_calls"):
        clean["tool_calls"] = []
        for tc in msg.get("tool_calls") or []:
            clean["tool_calls"].append({
                "id": tc.get("id"),
                "type": tc.get("type", "function"),
                "function": tc.get("function", {}),
            })
    if clean.get("content") is None and clean.get("tool_calls"):
        clean["content"] = ""
    elif clean.get("content") is None:
        clean["content"] = ""
    return clean


def _send_tool_status(
    chat_id,
    tg_func,
    typing_func,
    fn,
    args,
    used=None,
    quota=None,
    prefix="采集",
    status_cb=None,
):
    """Send a tool status directly or through the shared progress callback."""
    if not chat_id and not status_cb:
        return
    used = used or {}
    quota = quota or {}
    text = None
    if fn == "web_search":
        text = f"🔍 {prefix}搜索（{used.get(fn, 0)}/{quota.get(fn, 0)}）：{args.get('query', '')}"
    elif fn == "fetch_content":
        text = f"📄 抓取正文（{used.get(fn, 0)}/{quota.get(fn, 0)}）：{args.get('url', '')[:60]}"
    elif fn == "serper_search":
        text = f"🔎 Serper 核查：{args.get('query', '')}"
    elif fn == "wikipedia_lookup":
        text = f"📖 Wikipedia：{args.get('query', '')}"
    elif fn == "check_weather":
        text = "🌤 查询天气..."
    elif fn == "vps_traffic":
        text = "📊 查询 VPS 流量..."
    elif fn == "github_trending":
        lang = args.get("language", "")
        text = f"📦 GitHub 热榜{'（' + lang + '）' if lang else ''}..."
    elif fn == "check_api_balance":
        text = "🔑 查询各 API 余额…"
    elif fn == "calendar_query":
        text = f"📅 查询日历（未来 {int(args.get('days', 7))} 天）…"
    elif fn == "calendar_add":
        text = f"📅 添加日程：{args.get('summary', '')}…"
    elif fn == "read_today_cache":
        text = f"📂 读取今日缓存（{len(args.get('ids', []))} 条）…"
    else:
        _direct_labels = {
            "exchange_rate": "💱 查询汇率…",
            "stock_quote": "📈 查询行情…",
            "world_time": "🕐 查询世界时间…",
            "steam_specials": "🎮 查询 Steam 特价…",
            "hn_front_page": "🗣 拉取 HN 热帖…",
            "proxy_releases": "🔒 查询代理内核版本…",
            "chinese_holiday": "📅 查询节假日安排…",
            "github_release": f"📦 查询 {args.get('repo', '')} 最新版本…",
            "epic_free_games": "🎁 查询 Epic 免费游戏…",
            "cn_hot_topics": "🔥 拉取中文热榜…",
            "earthquake_recent": "🌏 查询近期地震…",
            "x_trends": "🐦 拉取 X 热门话题…",
        }
        text = _direct_labels.get(fn)
    if text:
        if status_cb:
            try:
                status_cb("gather_item", "start", text)
            except Exception:
                pass
        elif chat_id:
            typing_func(chat_id)
            tg_func("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "disable_notification": True,
            })


def build_execution_report(meta, pre_searched=""):
    """
    把写作AI本轮所有操作打包成结构化执行报告，传给监管AI。
    包含：所有工具调用及结果、可信来源索引、抓取的原文摘要、思考摘要。
    监管AI凭此报告审核，不会因信息缺失而误判。
    """
    sections = ["━━━ 写作AI本轮完整执行报告 ━━━\n"]

    # 1. 工具调用总览
    all_calls = meta.get("tool_calls_summary", [])
    if pre_searched:
        all_calls = [f"wikipedia_lookup({pre_searched})【代码层预查】"] + list(all_calls)
    if all_calls:
        sections.append(f"■ 工具调用（共 {len(all_calls)} 次）\n" +
                        "\n".join(f"  {i+1}. {c}" for i, c in enumerate(all_calls)))

    # 2. 各工具返回内容——按类型分组
    tool_results = meta.get("tool_results", [])
    direct_results = [r for r in tool_results if r.get("tool") in _DIRECT_API_TOOLS]
    search_results = [r for r in tool_results if r.get("tool") not in _DIRECT_API_TOOLS]

    if direct_results:
        lines = ["■ 直接API数据（天气/流量/Wikipedia等，内容本身即为真实数据，无需来源引用）"]
        for r in direct_results:
            lines.append(f"  [{r['tool']}]\n  {r['snippet'][:400]}")
        sections.append("\n".join(lines))

    if search_results:
        lines = ["■ 搜索/抓取内容（来源须对照索引）"]
        for r in search_results:
            lines.append(f"  [{r['tool']}] 查询：{r.get('query','')}\n  {r['snippet'][:300]}")
        sections.append("\n".join(lines))

    # 3. 可引用来源索引（搜索类）
    src_idx = meta.get("source_index", [])
    if pre_searched:
        pass
    if src_idx:
        lines = [f"■ 可引用来源索引（{len(src_idx)} 条，审核时以此为准）"]
        for i, s in enumerate(src_idx, 1):
            lines.append(f"  [{i}] {s['domain']}  「{s['title'][:40]}」  [{s.get('query','')}]")
        sections.append("\n".join(lines))
    else:
        sections.append("■ 可引用来源索引：（本轮未进行网络搜索）")

    # 4. 抓取的完整正文
    fetched = meta.get("fetched_pages", [])
    if fetched:
        lines = [f"■ 抓取原文（{len(fetched)} 篇，内容最权威）"]
        for fp in fetched:
            lines.append(f"  URL: {fp.get('url','')}\n  {fp.get('content','')[:500]}")
        sections.append("\n".join(lines))

    # 5. 思考摘要（写作AI的推理过程）
    reasoning = next(
        (r["reasoning"][:400] for r in meta.get("rounds", []) if r.get("reasoning")), ""
    )
    if reasoning:
        sections.append(f"■ 写作AI思考摘要\n  {reasoning}")

    sections.append("━━━ 报告结束 ━━━")
    return "\n\n".join(sections)


def summarize_for_context(reply: str) -> str:
    """
    用核查 AI 把本轮回复压缩成 10-30 字的摘要，供下一轮消歧层使用。
    thinking disabled，max_tokens 极小，几乎无成本。
    失败时降级为截取前 40 字。
    """
    import tg_bot.config as _cfg
    if not reply:
        return ""
    try:
        resp = http_post(
            f"{_cfg.DEEPSEEK_API_BASE}/chat/completions",
            {"model": _cfg.DEEPSEEK_MODEL,
             "messages": [
                 {"role": "system", "content":
                     "将下方助手回复压缩成10到30字的摘要，只说核心内容是什么，"
                     "不要任何标点之外的修饰词。直接输出摘要，不加引号或说明。"},
                 {"role": "user", "content": reply[:600]},
             ],
             "max_tokens": 50,
             "temperature": 0.1,
             "thinking": {"type": "disabled"}},
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_VERIFY_KEY}"},
            timeout=15,
        )
        if resp and resp.get("choices"):
            summary = (resp["choices"][0]["message"].get("content") or "").strip()
            if summary:
                return summary[:60]
    except Exception as e:
        log.warning(f"summarize_for_context 失败: {e}")
    return reply[:40]   # 降级：直接截取


# Source/cache matching helpers live in tg_bot.workers.source_utils.
# Keep the underscored names imported above for backward-compatible call sites in this module.

def gather_ai(user_text, keywords, chat_id=None, pre_results=None, history_ctx=None, focus_task=None,
              retry_hint=False, prev_searches=None, pre_source_entries=None, status_cb=None,
              evidence_audit_enabled=None, evidence_corrective_rounds=None,
              evidence_max_requirements=None, avoid_block="", suggested_words=0):
    """
    第二层：采集 AI。
    调用工具收集原始数据，最终输出结构化事实清单。
    thinking enabled：工具选择和判断过程需要可追溯。
    pre_results: 代码层已预读的缓存内容（字符串），直接注入 init_msg 头部。
    返回 (fact_list: str, meta: dict)
    """
    import tg_bot.config as _cfg
    from tg_bot.bot_utils import tg, typing

    kw_str   = "、".join(keywords) if keywords else user_text[:40]
    sess_ts  = datetime.now(timezone(timedelta(hours=8))).strftime("%H%M%S")
    res_seq  = [0]   # 用列表以便嵌套函数修改

    def _persist(entry):
        """实时写入今日索引（防止中途崩溃丢数据）"""
        try:
            update_today_index([entry], session_user=user_text)
        except Exception as _pe:
            log.debug(f"实时入库失败: {_pe}")

    def next_rid():
        res_seq[0] += 1
        return f"{sess_ts}_R{res_seq[0]:03d}"

    # 今日已采集索引注入（仅注入与当前关键词相关的条目，避免无关内容干扰）
    today_idx = load_today_index()
    if today_idx and keywords:
        kw_low = [k.lower() for k in keywords]
        relevant = [
            e for e in today_idx
            if _cache_match_score(
                e.get("query","") + " " + e.get("title","") + " " +
                e.get("snippet_head","") + " " + e.get("session_user",""),
                kw_low,
            ) >= 2
        ]
    else:
        relevant = []

    if relevant:
        idx_lines = ["【今日已采集索引 — 优先用 read_today_cache 工具读取，节省配额】"]
        for e in relevant[:20]:
            idx_lines.append(f"  {e['id']} | {e.get('query','')[:20]} | {e.get('title','')[:50]}")
        idx_lines.append("（天气/汇率/股价等实时数据不适用缓存，仍需重新获取）\n")
        cache_hint = "\n".join(idx_lines) + "\n"
    else:
        cache_hint = ""

    # ── 注入对话焦点约束（若有）───────────────────────────────────────
    _focus_prefix = ""
    if focus_task and focus_task.get("goal"):
        _goal = focus_task.get("goal", "")
        _deferred = focus_task.get("user_deferred", False)
        _anchor = ", ".join(focus_task.get("topic_anchor") or [])
        _focus_prefix = (
            "━━━ 当前任务焦点（最高优先级）━━━\n"
            f"用户的真实目标：{_goal}\n"
        )
        if _deferred:
            _focus_prefix += "用户已授权你自主选择具体对象，不要再要求用户指定具体名称，直接以最优候选执行。\n"
        if _anchor:
            _focus_prefix += f"话题锚点：{_anchor}\n"
        _focus_prefix += "【硬约束】本次只围绕上述焦点采集，禁止关联历史对话中其他话题。\n━━━\n\n"
    # ─────────────────────────────────────────────────────────────────

    pre_block = ""
    if pre_results:
        pre_block = (
            "【系统代查结果 — 代码层已替你执行完毕，以下内容仅供参考，不等同于本轮工具调用】\n"
            + pre_results
            + "\n（以上代查结果只可作为起点；无论是否足够，都必须额外调用至少 1 次工具（web_search 或 fetch_content）补充或验证信息，再整理事实清单；不得仅凭代查结果直接输出清单。）\n\n"
        )
    # 近几轮工具使用记录注入：让采集AI知道上几轮用过哪些工具/搜过什么，避免重复
    _tl_hint = fmt_toollog_for_prompt(n=3)
    toollog_block = (
        "【近期工具使用记录 — 仅供参考，避免重复搜索已有内容】\n"
        + _tl_hint + "\n\n"
    ) if _tl_hint else ""

    retry_block = ""
    if retry_hint and prev_searches:
        retry_block = "⚠️【重试模式】上轮已搜过：" + "、".join(prev_searches) + "，请换不同关键词/角度/来源\n\n"

    novelty_block = (avoid_block + "采集时请围绕与上述已讲内容不同的新主题搜索。\n\n") if avoid_block else ""

    # 当前问题放在最前面，历史记录放后面作辅助参考，避免采集AI把历史当成当前问题
    init_msg = (
        f"{_focus_prefix}{retry_block}{novelty_block}{pre_block}"
        f"【当前用户问题（本次需要回答的是这个）】\n{user_text}\n关键词：{kw_str}\n\n"
        f"{toollog_block}{cache_hint}"
        f"请开始采集信息，回答上方【当前用户问题】。"
    )

    _bj_now = datetime.now(timezone(timedelta(hours=8)))
    _bj_date = _bj_now.strftime("%Y年%m月%d日")
    _bj_time = _bj_now.strftime("%H:%M")
    _sys_gather_with_date = _SYS_GATHER + (
        f"\n\n【当前日期时间】今天是 {_bj_date} {_bj_time}（北京时间），判断历史记录距今多久请以此为准。搜索关键词中如需年份也请以此为准。"
        f"\n【安全边界】{NOVELTY_UNTRUSTED_BOUNDARY}"
    )
    if suggested_words:
        _sys_gather_with_date += (
            f"\n【目标回答字数】意图层判断这个问题的回答约 {int(suggested_words)} 字即可。"
            "素材足以支撑该篇幅就尽快给出完成结论，不要为更长的篇幅过度采集；"
            "suggested_length 也按这个目标折算，不要因为素材丰富而上调。"
        )
    msgs = [
        {"role": "system", "content": _sys_gather_with_date},
        {"role": "user",   "content": init_msg},
    ]
    quota = {"web_search": 10, "fetch_content": 4, "serper_search": 3, "read_today_cache": 99}
    used  = {"web_search": 0, "fetch_content": 0, "serper_search": 0, "read_today_cache": 0}
    _url_to_entry = {}   # URL → source_index 条目，用于 fetch_content 回填 full_content
    meta  = {"rounds": [], "tool_calls_summary": [], "tool_results": [], "fetched_pages": [], "failed_urls": []}
    fetch_success = 0
    source_index  = []
    for _entry in pre_source_entries or []:
        if _entry and _entry.get("id"):
            source_index.append(dict(_entry))
            if _entry.get("url"):
                _url_to_entry[_entry["url"]] = source_index[-1]

    # Evidence correction deliberately shares this Gather invocation's messages,
    # round limit and tool quota.  It is not a second autonomous search session.
    corrective_rounds = 0
    prior_requirements = ()
    previous_audit_sources = []
    no_novelty_rounds = 0
    audit_started = False
    last_audit = None
    last_completion = None
    audit_enabled = (
        getattr(_cfg, "CHAT_EVIDENCE_AUDIT", True)
        if evidence_audit_enabled is None else bool(evidence_audit_enabled)
    )
    if audit_enabled:
        # Normal search keeps only operational audit state, never raw CoT.
        meta["raw_reasoning_redacted"] = True
    novelty_rounds = []
    correction_limit = (
        getattr(_cfg, "CHAT_EVIDENCE_CORRECTIVE_ROUNDS", 2)
        if evidence_corrective_rounds is None else int(evidence_corrective_rounds)
    )
    max_requirements = (
        getattr(_cfg, "CHAT_EVIDENCE_MAX_REQUIREMENTS", 5)
        if evidence_max_requirements is None else int(evidence_max_requirements)
    )

    def _usable_audit_sources(entries):
        """Exclude failed/empty fetches before they reach the coverage model."""
        usable = []
        for entry in entries or []:
            if not isinstance(entry, dict) or not str(entry.get("id", "")).strip() or entry.get("error"):
                continue
            url = str(entry.get("url", "")).strip().lower()
            if not (url.startswith("http://") or url.startswith("https://") or url.startswith("direct://")):
                continue
            if not (str(entry.get("snippet", "")).strip() or str(entry.get("full_content", "")).strip()):
                continue
            usable.append(entry)
        return usable

    def _audit_progress(stage, status, detail=""):
        if not status_cb:
            return
        try:
            status_cb(stage, status, detail)
        except Exception:
            pass

    def _set_evidence_meta(result, audit_sources, *, stop_reason, supported_ids=()):
        from tg_bot.evidence_audit import evidence_stats

        stats = evidence_stats(audit_sources)
        supported_count = sum(coverage.status == "supported" for coverage in result.coverage)
        coverage_by_requirement = {
            coverage.requirement_id: coverage for coverage in result.coverage
        }
        meta["audit_required"] = True
        meta["supported_source_ids"] = list(supported_ids)
        meta["core_requirement_missing"] = (
            not result.requirements
            or any(
                coverage_by_requirement.get(requirement.id) is None
                or coverage_by_requirement[requirement.id].status != "supported"
                for requirement in result.requirements if requirement.required
            )
        )
        meta["evidence_audit"] = {
            "enabled": True,
            "requirements_total": len(result.requirements),
            "requirements_supported": supported_count,
            "independent_domains": stats.independent_domains,
            "corrective_rounds": corrective_rounds,
            "stop_reason": stop_reason,
            "novelty_rounds": list(novelty_rounds),
        }

    def _finish_audit(result, audit_sources, completion, *, stop_reason=None):
        """Return audit-supported sources and preserve normal Gather shape."""
        from tg_bot.evidence_audit import supported_source_ids

        reason_code = stop_reason or result.reason_code
        supported_ids = supported_source_ids(result)
        filtered_sources = [entry for entry in source_index if entry.get("id") in set(supported_ids)]
        if not filtered_sources:
            # 审计一条来源都没认可（含审计器自身故障 invalid_audit）时，
            # 保留全部已采集素材交给下游降级作答，而不是清空后必然拒答。
            filtered_sources = list(source_index)
        _set_evidence_meta(result, audit_sources, stop_reason=reason_code, supported_ids=supported_ids)
        audited_completion = dict(completion)
        audited_completion.update({
            "sufficient": bool(result.sufficient and reason_code in {"coverage_complete", "direct_api_complete"}),
            "reason_code": reason_code,
            "reason": completion.get("reason", ""),
        })
        return finalize_gather_sources(filtered_sources, meta, audited_completion)

    def _validated_gap_lines(result):
        requirements = {item.id: item for item in result.requirements}
        lines = []
        for coverage in result.coverage:
            if coverage.status not in {"missing", "partial", "conflicting"}:
                continue
            requirement = requirements.get(coverage.requirement_id)
            gap_query = _safe_gap_query(coverage.gap_query)
            if requirement and gap_query:
                # Keep model text structured and omit the untrusted question.
                lines.append({"id": requirement.id, "query": gap_query})
        return lines

    def _needs_audit(entries, completion):
        from tg_bot.evidence_audit import direct_api_complete, needs_evidence_audit

        audit_sources = _usable_audit_sources(entries)
        try:
            from tg_bot.core.pipeline import _HIGH_RISK_RE
            high_risk = bool(_HIGH_RISK_RE.search(user_text or ""))
        except Exception:
            high_risk = False
        return audit_sources, bool(
            audit_enabled
            and not direct_api_complete(entries)
            and needs_evidence_audit(
                user_text=user_text,
                source_count=len(audit_sources),
                tool_call_count=len(meta.get("tool_calls_summary", [])),
                suggested_length=(completion or {}).get("suggested_length", ""),
                high_risk=high_risk,
            )
        )

    def _finish_exhausted_audit():
        from tg_bot.evidence_audit import EvidenceAuditResult

        audit_sources = _usable_audit_sources(source_index)
        exhausted = last_audit or EvidenceAuditResult(
            False, prior_requirements, (), (), "quota_exhausted",
        )
        completion = last_completion or {
            "sufficient": False, "reason": "采集轮次或连接已耗尽",
            "reason_code": "quota_exhausted", "suggested_length": "",
        }
        return _finish_audit(exhausted, audit_sources, completion, stop_reason="quota_exhausted")

    for round_ in range(14):
        resp = http_post(
            f"{_cfg.DEEPSEEK_API_BASE}/chat/completions",
            {"model": _cfg.DEEPSEEK_MODEL,
             "messages": msgs,
             "tools": _CORRECTIVE_TOOLS if corrective_rounds else TOOLS,
             "tool_choice": "auto",
             "temperature": 0.3,
             "max_tokens": 6000,
             "thinking": {"type": "enabled", "budget_tokens": 2000}},
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_KEY}"},
            timeout=120,
        )
        if not resp or not resp.get("choices"):
            break

        choice = resp["choices"][0]
        finish = choice.get("finish_reason", "")
        msg    = choice["message"]

        reasoning = (msg.get("reasoning_content") or "").strip()
        tc_names  = [tc["function"]["name"] for tc in (msg.get("tool_calls") or [])]
        round_meta = {"round": round_, "role": "gather", "tool_calls": tc_names,
                      "reasoning_present": bool(reasoning)}
        if not audit_enabled:
            round_meta["reasoning"] = reasoning
        meta["rounds"].append(round_meta)
        if reasoning and not audit_enabled:
            log.info(f"🧠 采集R{round_} 思考: {reasoning[:150]}")

        if finish != "tool_calls":
            raw_out = (msg.get("content") or "").strip()
            completion = parse_gather_completion(raw_out)
            last_completion = completion
            source_index = complete_source_index(
                source_index,
                meta.get("tool_results", []),
                next_rid,
                _DIRECT_API_TOOLS,
            )
            log.info(
                "📦 采集完成：sufficient=%s length=%s reason=%s",
                completion["sufficient"],
                completion["suggested_length"],
                completion["reason"][:60],
            )
            log.info(f"📦 素材数量：{len(source_index)} 条，工具调用 {len(meta['tool_calls_summary'])} 次")
            audit_sources, audit_required = _needs_audit(source_index, completion)
            if not audit_required:
                meta["audit_required"] = False
                return finalize_gather_sources(source_index, meta, completion)

            audit_started = True
            _audit_progress("evidence_audit", "start", "")
            # Import at the call boundary so tests can replace only this model
            # adapter, while production retains its ordinary API retry behavior.
            from tg_bot.agents.evidence_auditor import audit_evidence
            audit = audit_evidence(
                user_text,
                audit_sources,
                prior_requirements=prior_requirements,
                max_requirements=max_requirements,
            )
            last_audit = audit
            _audit_progress("evidence_audit", "done", f"{len(audit.requirements)}项")
            if audit.reason_code == "invalid_audit":
                return _finish_audit(audit, audit_sources, completion)

            if corrective_rounds:
                from tg_bot.evidence_audit import evidence_novelty
                novelty = evidence_novelty(previous_audit_sources, audit_sources)
                novelty_rounds.append({
                    "round": corrective_rounds,
                    "new_canonical_urls": novelty[0],
                    "new_domains": novelty[1],
                    "new_full_text": novelty[2],
                })
                no_novelty_rounds = no_novelty_rounds + 1 if not any(novelty) else 0
                _audit_progress("evidence_gap", "done", f"第 {corrective_rounds}/{correction_limit} 轮")
                if audit.sufficient:
                    return _finish_audit(audit, audit_sources, completion, stop_reason="coverage_complete")
                if no_novelty_rounds >= 2:
                    _audit_progress("evidence_gap", "stop", f"第 {corrective_rounds}/{correction_limit} 轮")
                    return _finish_audit(audit, audit_sources, completion, stop_reason="no_novelty")

            if audit.sufficient:
                return _finish_audit(audit, audit_sources, completion, stop_reason="coverage_complete")

            previous_audit_sources = list(audit_sources)
            prior_requirements = audit.requirements
            gap_lines = _validated_gap_lines(audit)
            remaining_tools = any(used[name] < quota[name] for name in ("web_search", "serper_search", "fetch_content"))
            if not gap_lines:
                return _finish_audit(audit, audit_sources, completion)
            if not remaining_tools:
                _audit_progress("evidence_gap", "stop", f"第 {corrective_rounds}/{correction_limit} 轮")
                return _finish_audit(audit, audit_sources, completion, stop_reason="quota_exhausted")
            if corrective_rounds >= correction_limit:
                _audit_progress("evidence_gap", "stop", f"第 {corrective_rounds}/{correction_limit} 轮")
                return _finish_audit(audit, audit_sources, completion)

            corrective_rounds += 1
            _audit_progress("evidence_gap", "start", f"第 {corrective_rounds}/{correction_limit} 轮")
            msgs.append(_clean_assistant_msg_for_history(msg))
            msgs.append({
                "role": "user",
                "content": (
                    "【证据补查任务】只补查下列已核对的缺口；使用现有工具搜索并尽量抓取正文，"
                    "不要输出回答或解释。完成后继续按约定 JSON 给出采集完成状态。\n"
                    + json.dumps({"requirements": gap_lines}, ensure_ascii=False)
                ),
            })
            continue

        msgs.append(_clean_assistant_msg_for_history(msg))

        # 同一轮模型经常一次提出好几个工具调用（比如并列发起 3 个搜索），原来是
        # for 循环一个个跑，全是网络 I/O 却串行等待。这里拆成三段：
        #   1）配额判定/状态提示仍按原始顺序做完（不能并发，quota 计数会错乱）；
        #   2）真正耗时的 execute_gather_tool() 并发跑，且每个任务用独立的
        #      source_index/meta/url_to_entry，不直接碰共享状态；
        #   3）并发任务全部结束后，仍按原始 tc 顺序把各自攒的结果合并回共享状态、
        #      写今日索引、append 进 msgs —— 下游看到的顺序和串行版本完全一致。
        approved = []
        for tc in msg.get("tool_calls", []):
            fn = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception as _arg_e:
                result = f"[{fn} 参数解析失败: {_arg_e}]"
                log.warning(f"{fn} 参数解析失败: {_arg_e}")
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                continue

            if corrective_rounds and fn not in _CORRECTIVE_TOOL_NAMES:
                result = f"[补查阶段不允许调用 {fn}]"
                log.warning("补查阶段拒绝非搜索工具: %s", fn)
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                continue

            if fn in quota:
                if used[fn] >= quota[fn]:
                    result = f"[{fn} 配额已用完]"
                    msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    continue
                used[fn] += 1

            _send_tool_status(
                chat_id, tg, typing, fn, args, used, quota,
                prefix="采集", status_cb=status_cb,
            )
            approved.append((tc, fn, args))

        def _run_isolated(fn, args, url_snapshot, rid_prefix):
            """在独立的本地 source_index/meta 里跑一次工具调用，不碰共享状态。"""
            local_index: list = []
            local_meta: dict = {}
            local_seq = [0]

            def _local_next_rid():
                local_seq[0] += 1
                return f"{rid_prefix}{local_seq[0]:02d}"

            local_ctx = GatherExecContext(
                user_text=user_text,
                source_index=local_index,
                url_to_entry=dict(url_snapshot),
                meta=local_meta,
                next_rid=_local_next_rid,
                persist=lambda _entry: None,
            )
            tool_error = ""
            try:
                result = execute_gather_tool(fn, args, local_ctx)
            except Exception as _tool_e:
                result = f"{fn} 执行异常: {_tool_e}"
                tool_error = str(_tool_e)
                log.warning(f"{fn} 执行异常: {_tool_e}")
            return result, tool_error, local_index, local_meta

        results_by_tc = {}
        if approved:
            with ThreadPoolExecutor(
                max_workers=min(6, len(approved)), thread_name_prefix="gather-tool"
            ) as pool:
                futures = {
                    pool.submit(
                        _run_isolated, fn, args, _url_to_entry, f"{next_rid()}_",
                    ): (tc, fn, args)
                    for tc, fn, args in approved
                }
                for future, (tc, fn, args) in futures.items():
                    results_by_tc[tc["id"]] = future.result()

        for tc, fn, args in approved:
            result, tool_error, local_index, local_meta = results_by_tc[tc["id"]]

            # 把这个任务本地攒的新条目合并回共享 source_index / url_to_entry，
            # 并按原来的语义实时落盘（合并阶段是单线程的，不会有并发写竞争）。
            for entry in local_index:
                source_index.append(entry)
                if entry.get("url"):
                    _url_to_entry[entry["url"]] = entry
                _persist(entry)
            if local_meta.get("fetched_pages"):
                meta.setdefault("fetched_pages", []).extend(local_meta["fetched_pages"])
                fetch_success += len(local_meta["fetched_pages"])
            if local_meta.get("failed_urls"):
                meta.setdefault("failed_urls", []).extend(local_meta["failed_urls"])

            log.info(f"[gather/{fn}] {result[:80]}")
            q_disp = args.get("query") or args.get("url", "")[:30] or ""
            if fn in _DIRECT_API_TOOLS and not tool_error:
                from tg_bot.direct_api import direct_api_failure_reason
                tool_error = direct_api_failure_reason(fn, result)
            meta["tool_calls_summary"].append(f"{fn}({q_disp[:30]})" if q_disp else fn)
            if fn in ("web_search", "serper_search", "wikipedia_lookup", "fetch_content", "read_today_report", "check_weather", "vps_traffic", "github_trending", "calendar_query", "calendar_add", "check_api_balance", "exchange_rate", "stock_quote", "world_time", "steam_specials", "hn_front_page", "proxy_releases", "chinese_holiday", "github_release", "epic_free_games", "cn_hot_topics", "earthquake_recent", "x_trends"):
                snippet_limit = 1200 if fn in _DIRECT_API_TOOLS else 500
                meta["tool_results"].append({"tool": fn,
                    "query": q_disp[:40], "snippet": result[:snippet_limit]})
                if tool_error:
                    meta["tool_results"][-1]["error"] = tool_error
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

    source_index = complete_source_index(
        source_index,
        meta.get("tool_results", []),
        next_rid,
        _DIRECT_API_TOOLS,
    )
    log.warning(f"⚠️ 采集达到上限，返回 {len(source_index)} 条素材")
    if audit_started:
        return _finish_exhausted_audit()
    exhausted_completion = {
        "sufficient": False, "reason": "采集轮次耗尽",
        "reason_code": "quota_exhausted", "suggested_length": "",
    }
    _audit_sources, audit_required = _needs_audit(source_index, exhausted_completion)
    if audit_required:
        return _finish_exhausted_audit()
    meta["audit_required"] = False
    return finalize_round_limit(source_index, meta)


def fast_chat(messages, system, max_tokens=3000, temp=0.7, chat_id=None, return_meta=False):
    """Single DeepSeek call without any tool definitions.

    This is the only API path used by bot fast path. It guarantees that a
    "fast" route cannot silently call search/fetch tools and bypass verifier.
    """
    import tg_bot.config as _cfg

    msgs = [{"role": "system", "content": system}] + messages
    meta = {
        "rounds": [],
        "tool_calls_summary": [],
        "tool_results": [],
        "fetched_pages": [],
        "source_index": [],
    }
    try:
        resp = http_post(
            f"{_cfg.DEEPSEEK_API_BASE}/chat/completions",
            {"model": _cfg.DEEPSEEK_MODEL,
             "messages": msgs,
             "temperature": temp,
             "max_tokens": max_tokens,
             "thinking": {"type": "enabled", "budget_tokens": 800}},
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_KEY}"},
            timeout=90,
        )
        if not resp or not resp.get("choices"):
            reply = "DeepSeek 没有响应，稍后再试。"
        else:
            msg = resp["choices"][0]["message"]
            reasoning = (msg.get("reasoning_content") or "").strip()
            reply = (msg.get("content") or "").strip()
            meta["rounds"].append({
                "round": 0,
                "role": "fast",
                "reasoning": reasoning,
                "content_preview": reply[:300],
                "tool_calls": [],
            })
    except Exception as e:
        log.warning(f"fast_chat 失败: {e}")
        reply = "DeepSeek 调用失败，稍后再试。"
    return (reply, meta) if return_meta else reply


def ds_chat(messages, system, max_tokens=3000, temp=0.8, chat_id=None, return_meta=False):
    """
    两阶段工具调用：
      web_search    → 最多 6 次（Tavily→Brave→Serper 自动回退）
      fetch_content → 最多 2 次（Jina Reader 抓正文）
      serper_search → 最多 1 次（Google 交叉验证）
      wikipedia_lookup → 不限次数
    return_meta=True 时返回 (reply, meta)，meta={rounds:[...], tool_calls_summary:[...]}
    """
    import tg_bot.config as _cfg
    from tg_bot.bot_utils import tg, typing

    msgs = [{"role": "system", "content": system}] + messages
    quota = {"web_search": 10, "fetch_content": 4, "serper_search": 3}
    used  = {"web_search": 0, "fetch_content": 0, "serper_search": 0}
    meta  = {"rounds": [], "tool_calls_summary": [], "tool_results": [],
             "fetched_pages": [], "failed_urls": []}  # 用于日志
    fetch_success    = 0     # 成功抓到原文的次数
    _search_enforced = False # 只强制补搜一次，防止死循环
    _fetch_enforced  = False # 只强制补抓一次，防止死循环
    source_index     = []    # 本轮实际搜到的来源 [{domain, query, title, snippet}]
    _url_to_entry    = {}
    _res_seq         = [0]
    _source_injected = False # 来源索引只注入一次

    def next_rid():
        _res_seq[0] += 1
        return f"DS_R{_res_seq[0]:03d}"

    def _persist(_entry):
        return None

    def _ret(reply):
        meta["source_index"] = source_index   # 回传给调用方存档
        return (reply, meta) if return_meta else reply

    for round_ in range(16):   # 扩展到16轮，给强制补搜/补抓留空间
        resp = http_post(
            f"{_cfg.DEEPSEEK_API_BASE}/chat/completions",
            {"model": _cfg.DEEPSEEK_MODEL,
             "messages": msgs,
             "tools": TOOLS,
             "tool_choice": "auto",
             "temperature": temp,
             "max_tokens": max_tokens,
             "thinking": {"type": "enabled", "budget_tokens": 1500}},
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_KEY}"},
            timeout=120
        )

        if not resp or not resp.get("choices"):
            return _ret("DeepSeek 没有响应，稍后再试。")

        choice = resp["choices"][0]
        finish = choice.get("finish_reason", "")
        msg    = choice["message"]

        # 记录本轮的 reasoning + content
        reasoning = (msg.get("reasoning_content") or "").strip()
        content_preview = (msg.get("content") or "").strip()[:300]
        tc_names = [tc["function"]["name"] for tc in (msg.get("tool_calls") or [])]
        meta["rounds"].append({
            "round": round_,
            "reasoning": reasoning,
            "content_preview": content_preview,
            "tool_calls": tc_names,
        })
        if reasoning:
            log.info(f"🧠 R{round_} 思考前200字: {reasoning[:200]}")

        if finish != "tool_calls":
            total_searches = used["web_search"] + used["serper_search"]

            # ── 强制①：搜了但次数不够，必须再搜 ────────────────────
            if (not _search_enforced
                    and total_searches >= 1
                    and total_searches < 3
                    and used["web_search"] < quota["web_search"]):
                _search_enforced = True
                still = 3 - total_searches
                log.info(f"🔁 强制补搜：当前{total_searches}次，还需{still}次")
                msgs.append({"role": "user", "content":
                    f"【系统强制】当前只搜了 {total_searches} 次，搜索面不够。"
                    f"请换不同关键词或角度再搜 {still} 次，扩大信息来源，再给出最终答案。"})
                continue

            # ── 强制②：搜了但没成功抓到任何原文 ────────────────────
            if (not _fetch_enforced
                    and total_searches >= 1
                    and fetch_success == 0
                    and used["fetch_content"] < quota["fetch_content"]):
                _fetch_enforced = True
                log.info("🔁 强制补抓原文")
                msgs.append({"role": "user", "content":
                    "【系统强制】你搜索了但还没有成功获取任何原文。"
                    "请从上面搜索结果中挑最有价值的 URL，调用 fetch_content 获取原文；"
                    "如果失败，立即换下一个 URL 继续尝试，直到成功抓到一篇为止，再给出最终答案。"})
                continue

            # ── 来源索引注入：最终答案前让模型对照真实来源 ──────────────
            if source_index and not _source_injected:
                _source_injected = True
                idx_lines = []
                known_domains = set()
                for i, s in enumerate(source_index[:18]):  # 最多18条，防止过长
                    line = f"  [{i+1}] {s['domain']} | {s['query']} | {s['title']}"
                    if s.get("snippet"):
                        line += f" — {s['snippet']}"
                    idx_lines.append(line)
                    known_domains.add(s["domain"].lower())
                # 把已知合法平台名也加进去，防止误判
                meta["known_domains"] = known_domains
                msgs.append({"role": "user", "content":
                    "【本轮搜索来源索引·请据此给出最终答案】\n"
                    + "\n".join(idx_lines)
                    + "\n以上是本轮实际从搜索结果中提取的真实来源（格式：域名 | 查询词 | 标题 — 内容摘要）。\n"
                    "规则：\n"
                    "① 只能陈述索引里出现过的域名所支持的内容；索引里没有的网站名、平台名一律不在正文里提及。\n"
                    "② 正文里不标注任何来源，不写（来源：xxx）（经验补充）（推测）（Wiki）等任何括号注释——来源只供你内部参考，不出现在输出里。\n"
                    "③ 没有来源支撑的内容，直接删掉那句话，不要替换成猜测。\n"
                    "请直接输出干净的最终答案，不写草稿、不写修改过程。"
                })
                log.info(f"📌 注入来源索引 {len(source_index)} 条，已知域名：{known_domains}")
                continue

            return _ret((msg.get("content") or "").strip())

        msgs.append(_clean_assistant_msg_for_history(msg))
        for tc in msg.get("tool_calls", []):
            fn   = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception as _arg_e:
                result = f"[{fn} 参数解析失败: {_arg_e}]"
                log.warning(f"{fn} 参数解析失败: {_arg_e}")
                msgs.append({"role":"tool","tool_call_id":tc["id"],"content":result})
                continue

            # ── 配额检查 ──────────────────────────────────────────
            if fn in quota:
                if used[fn] >= quota[fn]:
                    result = f"[{fn} 配额已用完，跳过此次调用]"
                    log.info(f"⚠️ {fn} 配额耗尽，跳过")
                    msgs.append({"role":"tool","tool_call_id":tc["id"],"content":result})
                    continue
                used[fn] += 1

            # ── 执行工具 ──────────────────────────────────────────
            _send_tool_status(chat_id, tg, typing, fn, args, used, quota, prefix="")
            exec_ctx = GatherExecContext(
                user_text=(messages[-1].get("content", "") if messages else ""),
                source_index=source_index,
                url_to_entry=_url_to_entry,
                meta=meta,
                next_rid=next_rid,
                persist=_persist,
            )
            try:
                before_fetch_count = len(meta.get("fetched_pages", []))
                result = execute_gather_tool(fn, args, exec_ctx)
                if len(meta.get("fetched_pages", [])) > before_fetch_count:
                    fetch_success += 1
            except Exception as _tool_e:
                result = f"{fn} 执行异常: {_tool_e}"
                log.warning(f"{fn} 执行异常: {_tool_e}")

            log.info(f"[{fn}] 结果前100字: {result[:100]}")
            # 记录到 tool_calls_summary（去重统计）
            q_display = args.get("query") or args.get("url", "")[:30] or ""
            meta["tool_calls_summary"].append(f"{fn}({q_display[:30]})" if q_display else fn)
            # 保存搜索摘要（供 Method C 追溯用），非天气/流量/vps 类才有意义
            if fn in ("web_search", "serper_search", "wikipedia_lookup", "fetch_content", "read_today_report", "exchange_rate", "stock_quote", "world_time", "steam_specials", "hn_front_page", "proxy_releases", "chinese_holiday", "github_release", "epic_free_games", "cn_hot_topics", "earthquake_recent", "x_trends"):
                meta["tool_results"].append({
                    "tool": fn,
                    "query": q_display[:40],
                    "snippet": result[:400],
                })
            msgs.append({"role":"tool","tool_call_id":tc["id"],"content":result})

    return _ret("处理超时，请重新提问。")
