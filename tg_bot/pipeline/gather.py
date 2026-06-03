#!/usr/bin/env python3
"""pipeline/gather.py — 采集层（gather_ai）+ 核心 AI 调用（ds_chat）+ 辅助函数"""

import json, logging
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
    update_today_index,
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


def _send_tool_status(chat_id, tg_func, typing_func, fn, args, used=None, quota=None, prefix="采集"):
    """Send a short Telegram status message for a gather tool call."""
    if not chat_id:
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
    if text:
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
            "https://api.deepseek.com/chat/completions",
            {"model": "deepseek-v4-flash",
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
              retry_hint=False, prev_searches=None, pre_source_entries=None):
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

    # 当前问题放在最前面，历史记录放后面作辅助参考，避免采集AI把历史当成当前问题
    init_msg = (
        f"{_focus_prefix}{retry_block}{pre_block}"
        f"【当前用户问题（本次需要回答的是这个）】\n{user_text}\n关键词：{kw_str}\n\n"
        f"{toollog_block}{cache_hint}"
        f"请开始采集信息，回答上方【当前用户问题】。"
    )

    _bj_now = datetime.now(timezone(timedelta(hours=8)))
    _bj_date = _bj_now.strftime("%Y年%m月%d日")
    _bj_time = _bj_now.strftime("%H:%M")
    _sys_gather_with_date = _SYS_GATHER + f"\n\n【当前日期时间】今天是 {_bj_date} {_bj_time}（北京时间），判断历史记录距今多久请以此为准。搜索关键词中如需年份也请以此为准。"
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

    for round_ in range(14):
        resp = http_post(
            "https://api.deepseek.com/chat/completions",
            {"model": "deepseek-v4-flash",
             "messages": msgs,
             "tools": TOOLS,
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
        meta["rounds"].append({
            "round": round_, "role": "gather",
            "reasoning": reasoning, "tool_calls": tc_names,
        })
        if reasoning:
            log.info(f"🧠 采集R{round_} 思考: {reasoning[:150]}")

        if finish != "tool_calls":
            raw_out = (msg.get("content") or "").strip()
            completion = parse_gather_completion(raw_out)
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
            return finalize_gather_sources(source_index, meta, completion)

        msgs.append(_clean_assistant_msg_for_history(msg))
        for tc in msg.get("tool_calls", []):
            fn   = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception as _arg_e:
                result = f"[{fn} 参数解析失败: {_arg_e}]"
                log.warning(f"{fn} 参数解析失败: {_arg_e}")
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                continue

            if fn in quota:
                if used[fn] >= quota[fn]:
                    result = f"[{fn} 配额已用完]"
                    msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    continue
                used[fn] += 1

            _send_tool_status(chat_id, tg, typing, fn, args, used, quota, prefix="采集")
            exec_ctx = GatherExecContext(
                user_text=user_text,
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

            log.info(f"[gather/{fn}] {result[:80]}")
            q_disp = args.get("query") or args.get("url", "")[:30] or ""
            meta["tool_calls_summary"].append(f"{fn}({q_disp[:30]})" if q_disp else fn)
            if fn in ("web_search", "serper_search", "wikipedia_lookup", "fetch_content", "read_today_report", "check_weather", "vps_traffic", "github_trending", "calendar_query", "calendar_add", "check_api_balance"):
                snippet_limit = 1200 if fn in _DIRECT_API_TOOLS else 500
                meta["tool_results"].append({"tool": fn,
                    "query": q_disp[:40], "snippet": result[:snippet_limit]})
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

    source_index = complete_source_index(
        source_index,
        meta.get("tool_results", []),
        next_rid,
        _DIRECT_API_TOOLS,
    )
    log.warning(f"⚠️ 采集达到上限，返回 {len(source_index)} 条素材")
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
            "https://api.deepseek.com/chat/completions",
            {"model": "deepseek-v4-flash",
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
            "https://api.deepseek.com/chat/completions",
            {"model": "deepseek-v4-flash",
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
            if fn in ("web_search", "serper_search", "wikipedia_lookup", "fetch_content", "read_today_report"):
                meta["tool_results"].append({
                    "tool": fn,
                    "query": q_display[:40],
                    "snippet": result[:400],
                })
            msgs.append({"role":"tool","tool_call_id":tc["id"],"content":result})

    return _ret("处理超时，请重新提问。")
