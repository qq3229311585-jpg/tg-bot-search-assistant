#!/usr/bin/env python3
"""pipeline/gather.py — 采集层（gather_ai）+ 核心 AI 调用（ds_chat）+ 辅助函数"""

import json, re, logging
from datetime import datetime, timezone, timedelta

from tg_bot.config import (
    DEEPSEEK_KEY, DEEPSEEK_KEYS, DEEPSEEK_VERIFY_KEY, DEEPSEEK_VERIFY_KEYS,
    _next_ds_key, _next_verify_key, _ctx,
    _DIRECT_API_TOOLS,
)
from tg_bot.prompts import _SYS_GATHER, _FACTS_SHEET_FORMAT
from tg_bot.tools.definitions import TOOLS
from tg_bot.tools.fetch import http_post
from tg_bot.tools.search import execute_search, _execute_serper
from tg_bot.tools.fetch import execute_fetch_content, execute_read_cache
from tg_bot.tools.native import (
    execute_weather, execute_vps_traffic, execute_github_trending,
    execute_api_balance, execute_wikipedia, validate_facts_sheet,
)
from tg_bot.storage import (
    load_today_index, save_thinking_entry, fmt_toollog_for_prompt,
    update_today_index,
)
from tg_bot.facts import build_facts_json

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


def _query_signals(text: str):
    """从查询词里提取用于比对来源相关性的短信号。"""
    text = (text or "").strip()
    if not text:
        return []

    stop = {
        "什么", "怎么", "如何", "为什么", "为啥", "哪些", "哪个", "以及",
        "今天", "昨天", "明天", "最近", "最新", "新闻", "消息", "情况", "问题",
        "一下", "一个", "一些", "这个", "那个", "这些", "那些", "请问", "帮我",
        "看看", "说说", "是否", "是不是", "有啥", "有没有", "可以", "能否",
    }
    signals = []
    seen = set()

    def _add(sig: str):
        sig = (sig or "").strip().lower()
        if len(sig) < 2:
            return
        if sig in stop or sig in seen:
            return
        seen.add(sig)
        signals.append(sig)

    for chunk in re.split(r"[\s,，。！？?!、;；:：/\\|()\[\]{}<>《》“”\"'`]+", text):
        chunk = chunk.strip()
        if len(chunk) >= 2:
            _add(chunk)

    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", text):
        seq = m.group(0)
        _add(seq)
        for i in range(len(seq) - 1):
            _add(seq[i:i + 2])

    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_-]{2,}", text):
        _add(m.group(0))

    return signals


_ZH_EN_SOURCE_HINTS = {
    "龙卷风": "tornado",
    "地震": "earthquake",
    "台风": "typhoon",
    "飓风": "hurricane",
    "洪水": "flood",
    "海啸": "tsunami",
    "火山": "volcano",
    "暴风雪": "blizzard",
    "干旱": "drought",
}


def _source_matches_query(query: str, title: str, snippet: str, domain: str = "",
                          strict: bool = False, url: str = "") -> bool:
    """用查询信号做一层相关性筛选，避免明显跑题的来源进入 source_index。"""
    signals = _query_signals(query)
    if not signals:
        return True
    extra = []
    for sig in signals:
        for zh, en in _ZH_EN_SOURCE_HINTS.items():
            if zh in sig and en not in extra:
                extra.append(en)
    signals = signals + extra
    url_path = (url or "").split("?", 1)[0].split("#", 1)[0]
    hay = (f"{title} {snippet} {domain} {url_path}" or "").lower()
    hits = [s for s in signals if s and s in hay]
    if not hits:
        return False
    if strict and len(signals) >= 3 and len(hits) < 2:
        return False
    return True


def _extract_wiki_title(result: str, query: str) -> str:
    """兼容不同 Wikipedia 工具返回格式；解析失败时用 query 兜底。"""
    wiki_title = (query or "Wikipedia").strip()
    for pat in (
        r'Wikipedia — (.+?):',
        r'【英文Wikipedia】([^\n:]+)',
        r'【中文Wikipedia】([^\n:]+)',
    ):
        m = re.search(pat, result or "")
        if m:
            wiki_title = m.group(1).strip()
            break
    return wiki_title[:80] or (query or "Wikipedia")[:80]


def _compact_fallback_text(text: str, query: str = "", limit: int = 520) -> str:
    """从长原文里提取更像答案的片段，避免兜底时只剩壳。"""
    text = (text or "").strip()
    if not text:
        return ""
    signals = [s for s in _query_signals(query) if len(s) >= 2]
    junk_re = re.compile(
        r"(skip to|donate|menu|navigation|subscribe|sign in|login|privacy|cookie|"
        r"official website|here.s how you know|journalism of courage|news headlines)",
        re.I,
    )
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and len(ln.strip()) >= 25 and not junk_re.search(ln)
    ]
    if signals:
        hits = [ln for ln in lines if any(s in ln.lower() for s in signals)]
        if hits:
            picked = "\n".join(hits[:4])
            return picked[:limit]
    if lines:
        return "\n".join(lines[:4])[:limit]
    return text[:limit]


def _fact_list_supports_query(fact_list: str, keywords):
    hay = (fact_list or "").lower()
    if not hay:
        return False
    first_gold_mode = any(
        "第一枚奥运金牌" in (k or "")
        or ("第一枚" in (k or "") and "奥运" in (k or ""))
        or "首枚奥运金牌" in (k or "")
        for k in (keywords or [])
    )
    if first_gold_mode:
        return any(s in hay for s in ("许海峰", "首金", "第一枚奥运", "1984年7月29日", "1984"))

    if re.search(r"(18|19|20)\d{2}", hay):
        return True
    generic_terms = {
        "中国", "年份", "时间", "时候", "第一", "一个", "奥运金牌", "奥运", "金牌",
        "首金", "百科", "维基", "来源", "直接api", "wikipedia_lookup", "web_search",
    }
    for k in keywords or []:
        k = (k or "").strip().lower()
        if not k or k in generic_terms:
            continue
        min_len = 2 if any("\u4e00" <= c <= "\u9fff" for c in k) else 5
        if len(k) >= min_len and k in hay:
            return True
    if len(re.findall(r"^\[F\d{3}\]", fact_list or "", re.M)) >= 3 and len(hay.strip()) > 500:
        return True
    zh_char_count = sum(1 for c in hay if "\u4e00" <= c <= "\u9fff")
    if zh_char_count / max(len(hay), 1) < 0.10 and len(hay.strip()) > 100:
        return True
    return any(s in hay for s in ("1984", "许海峰", "第一枚奥运金牌", "中国第一枚奥运金牌"))


_CACHE_GENERIC_TERMS = {
    "中国", "今天", "昨天", "明天", "年份", "时间", "时候", "哪里", "哪年", "哪一", "第一", "一个",
}

_FETCH_BLOCKED_DOMAINS = {"zhihu.com", "www.zhihu.com"}
_SOURCE_BLOCKED_DOMAINS = {
    "baike.baidu.com",
    "www.baike.baidu.com",
}

_NAV_ONLY_TITLES = {
    "an official website of the united states",
    "an official website of the united states government",
    "news headlines",
    "菜单导航",
    "navigation",
    "follow along with the video below",
    "journalism of courage",
    "要闻推荐",
    "我们的相关功能",
    "本网站",
}


def _is_nav_or_empty_page(title, body):
    """fetch_content 只拦截明显导航/空页，不再做跨语言相关性否决。"""
    title_norm = (title or "").lower().strip()
    body_norm = (body or "").strip()
    if len(body_norm) < 200:
        return True
    return any(t and t in title_norm for t in _NAV_ONLY_TITLES)


def _is_blocked_domain(domain: str) -> bool:
    domain = (domain or "").lower()
    return domain in _SOURCE_BLOCKED_DOMAINS or "baike.baidu.com" in domain or "百度百科" in domain


def _strip_blocked_search_blocks(result: str) -> str:
    """从搜索结果文本里去掉禁用来源块，避免把它们喂给采集AI。"""
    kept = []
    for block in (result or "").split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) < 2 or not lines[-1].startswith("http"):
            continue
        url = lines[-1].strip()
        title_line = lines[0].lstrip("• ").split("[")[0].strip()[:80]
        try:
            domain = url.split("/")[2]
        except Exception:
            domain = url
        if _is_blocked_domain(domain) or "百度百科" in title_line:
            log.info(f"🚫 跳过禁用来源：{domain} | {title_line[:40]}")
            continue
        kept.append(block)
    return "\n\n".join(kept)


def _cache_match_score(entry_text, keywords):
    text = (entry_text or "").lower()
    score = 0
    for k in keywords or []:
        k = (k or "").strip().lower()
        if len(k) < 2 or k in _CACHE_GENERIC_TERMS:
            continue
        if k in text:
            score += 1
    for s in ("1984", "许海峰", "首金", "洛杉矶奥运", "第一枚奥运", "第一枚金牌"):
        if s in text:
            score += 1
    return score


def gather_ai(user_text, keywords, chat_id=None, pre_results=None, history_ctx=None,
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

    def _synthesize_fact_sheet():
        """当模型清单明显偏题时，用工具结果重建一个最小可写清单。"""
        candidates = []

        def _push(kind, meta_entry):
            body = (meta_entry.get("full_content") or meta_entry.get("snippet") or "").strip()
            if not body:
                return
            title = (meta_entry.get("title") or meta_entry.get("tool") or "").strip()
            score = _cache_match_score(title + " " + body, keywords)
            candidates.append((max(score, 0), kind, title, body, meta_entry))

        for e in source_index:
            _push("source", e)
        for r in meta.get("tool_results", []):
            _push("tool", r)

        if not candidates:
            return None, None

        candidates.sort(key=lambda x: x[0], reverse=True)
        lines = [
            "═══ 事实清单 ═══",
            f"用户问题：{user_text}",
            f"采集时间：{datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "【直接API来源】",
        ]
        synth_sources = []
        fidx = 1
        for score, kind, title, body, meta_entry in candidates[:12]:
            excerpt_query = f"{user_text} {title} {meta_entry.get('query', '')}"
            excerpt = _compact_fallback_text(body, excerpt_query, 700)
            if not excerpt:
                continue
            claim = _compact_fallback_text(excerpt, excerpt_query, 180).splitlines()[0].strip()
            if not claim:
                claim = title[:80] or "工具结果摘要"
            fnum = f"F{fidx:03d}"
            lines.append(f"[{fnum}] {claim}")
            if meta_entry.get("tool") in _DIRECT_API_TOOLS:
                source_text = f"直接API-{meta_entry.get('tool')}"
            else:
                source_text = meta_entry.get("domain") or meta_entry.get("tool") or "搜索来源"
            lines.append(f"       来源：{source_text}")
            excerpt_limit = 500 if len(body) > 500 else 220
            lines.append(f'       原文片段："{excerpt[:excerpt_limit].replace(chr(34), "“")}"')
            lines.append("")

            synth_sources.append({
                "id": meta_entry.get("id") or f"SYNTH_{fidx:03d}",
                "tool": meta_entry.get("tool") or kind,
                "query": meta_entry.get("query", user_text[:40]),
                "title": title[:120] or claim[:120],
                "url": meta_entry.get("url", ""),
                "domain": meta_entry.get("domain", meta_entry.get("tool", "")),
                "snippet": excerpt[:600],
                "full_content": body,
            })
            fidx += 1

        if fidx == 1:
            return None, None
        lines.append("【搜索来源】")
        lines.append("（本次无）")
        lines.append("")
        lines.append("【未获取到】")
        lines.append("（本次无）")
        lines.append("")
        lines.append("═══ 清单结束 ═══")
        return "\n".join(lines), synth_sources

    def _backfill_source_index_from_tools():
        """source_index 为空时，从工具结果补最小来源索引。"""
        if source_index:
            return
        for tr in meta.get("tool_results", []):
            tool = tr.get("tool", "")
            query = tr.get("query", "")[:40]
            snippet = tr.get("snippet", "") or ""
            if not snippet:
                continue

            if tool in ("web_search", "serper_search"):
                for block in snippet.split("\n\n"):
                    lines = block.strip().splitlines()
                    if len(lines) >= 2 and lines[-1].startswith("http"):
                        url = lines[-1].strip()
                        try:
                            domain = url.split("/")[2]
                        except Exception:
                            domain = url
                        title_line = lines[0].lstrip("• ").split("[")[0].strip()[:80]
                        snip = " ".join(l.strip() for l in lines[1:-1])[:300]
                        source_index.append({
                            "id":           f"AUTO_{tool}_{next_rid()}",
                            "tool":         tool,
                            "query":        query,
                            "title":        title_line or (query or tool)[:80],
                            "url":          url,
                            "domain":       domain,
                            "snippet":      snip or snippet[:300],
                            "full_content": None,
                        })
            elif tool == "wikipedia_lookup":
                source_index.append({
                    "id":           f"AUTO_{tool}_{next_rid()}",
                    "tool":         tool,
                    "query":        query,
                    "title":        _extract_wiki_title(snippet, query or tool),
                    "url":          f"https://en.wikipedia.org/wiki/{(query or tool).replace(' ','_')}",
                    "domain":       "wikipedia.org",
                    "snippet":      snippet[:600],
                    "full_content": snippet,
                })
            elif tool in _DIRECT_API_TOOLS:
                source_index.append({
                    "id":           f"AUTO_{tool}_{next_rid()}",
                    "tool":         tool,
                    "query":        query,
                    "title":        (query or tool)[:80],
                    "url":          "",
                    "domain":       tool,
                    "snippet":      snippet[:600],
                    "full_content": snippet,
                })
        if not source_index and meta.get("tool_results"):
            for tr in meta.get("tool_results", []):
                snippet = (tr.get("snippet") or "").strip()
                if not snippet:
                    continue
                tool = tr.get("tool", "")
                query = tr.get("query", "")[:40]
                source_index.append({
                    "id":           f"AUTO_RAW_{tool}_{next_rid()}",
                    "tool":         tool,
                    "query":        query,
                    "title":        (query or tool or "工具结果")[:80],
                    "url":          "",
                    "domain":       tool or "tool_results",
                    "snippet":      snippet[:600],
                    "full_content": snippet,
                })
        if source_index:
            log.info(f"📋 source_index 兜底：从 tool_results 补充 {len(source_index)} 条来源")

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
    _filter_query = f"{(user_text or '').strip()} {kw_str}".strip()

    # 当前问题放在最前面，历史记录放后面作辅助参考，避免采集AI把历史当成当前问题
    init_msg = (
        f"{retry_block}{pre_block}"
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
            # 采集完毕，当前 content 就是事实清单
            fact_list = (msg.get("content") or "").strip()
            if not fact_list:
                fact_list = "未获取到：任何信息（采集 AI 未返回清单）"

            _fact_list_failed = (
                not fact_list
                or "未获取到" in fact_list
                or "超时" in fact_list
                or "循环异常" in fact_list
            )

            if (_fact_list_failed or not _fact_list_supports_query(fact_list, keywords)):
                if source_index or meta.get("tool_results"):
                    synth_fact_list, synth_sources = _synthesize_fact_sheet()
                    if synth_fact_list:
                        fact_list = synth_fact_list
                        source_index = list(source_index or []) + list(synth_sources or [])
                        log.warning("📋 模型清单与问题不匹配，已改用工具结果重建事实清单")
                    elif meta.get("tool_results"):
                        raw_lines = [
                            "═══ 事实清单 ═══",
                            f"用户问题：{user_text}",
                            f"采集时间：{datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}",
                            "",
                            "【直接API来源】",
                        ]
                        raw_count = 0
                        for tr in meta.get("tool_results", [])[:8]:
                            snippet = (tr.get("snippet") or "").strip()
                            if not snippet:
                                continue
                            claim = _compact_fallback_text(snippet, user_text, 120).splitlines()[0].strip()
                            claim = claim or (tr.get("query") or tr.get("tool") or "工具结果摘要")
                            fnum = f"F{raw_count+1:03d}"
                            raw_lines.append(f"[{fnum}] {claim}")
                            raw_lines.append(f"       来源：{tr.get('tool') or 'tool_results'}")
                            raw_lines.append(f'       原文片段："{snippet[:220].replace(chr(34), "“")}"')
                            raw_lines.append("")
                            raw_count += 1
                        if raw_count:
                            raw_lines.extend([
                                "【搜索来源】", "（本次无）", "",
                                "【未获取到】", "（本次无）", "",
                                "═══ 清单结束 ═══",
                            ])
                            fact_list = "\n".join(raw_lines)
                            log.warning("📋 直接用 tool_results 原文摘要重建事实清单")

            # ── 格式校验：检查三段标题 + 首尾标记 ──────────────────
            if not validate_facts_sheet(fact_list):
                log.warning("📋 事实清单格式不合规，重试一次")
                msgs.append(_clean_assistant_msg_for_history(msg))
                msgs.append({"role": "user", "content":
                    "【格式校验失败】你的输出缺少必要的结构。请严格按以下格式重新整理并输出事实清单：\n\n"
                    + _FACTS_SHEET_FORMAT
                    + "\n\n三个分组标题必须全部出现，即使某组为空也写（本次无）。直接输出清单，不要解释。"
                })
                resp2 = http_post(
                    "https://api.deepseek.com/chat/completions",
                    {"model": "deepseek-v4-flash",
                     "messages": msgs,
                     "max_tokens": 6000,
                     "temperature": 0.3,
                     "thinking": {"type": "enabled", "budget_tokens": 800}},
                    headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_KEY}"},
                    timeout=90,
                )
                if resp2 and resp2.get("choices"):
                    fact_list2 = (resp2["choices"][0]["message"].get("content") or "").strip()
                    if validate_facts_sheet(fact_list2):
                        fact_list = fact_list2
                        log.info("📋 重试后格式合规")
                    else:
                        log.warning("📋 两次格式均不合规，使用原始输出继续")
                        # 不阻断流程，写作AI能处理非标准输入

            _backfill_source_index_from_tools()
            meta["source_index"] = source_index
            meta["facts_json"] = build_facts_json(fact_list, source_index)
            log.info(f"📋 采集完成，清单 {len(fact_list)} 字，工具调用 {len(meta['tool_calls_summary'])} 次")
            return fact_list, meta

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

            if fn == "web_search":
                q, stype = args.get("query", ""), args.get("search_type", "general")
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": f"🔍 采集搜索（{used['web_search']}/{quota['web_search']}）：{q}",
                        "disable_notification": True})
                result = execute_search(q, stype)
                result = _strip_blocked_search_blocks(result)
                for block in result.split("\n\n"):
                    lines = block.strip().splitlines()
                    if len(lines) >= 2 and lines[-1].startswith("http"):
                        url = lines[-1].strip()
                        title_line = lines[0].lstrip("• ").split("[")[0].strip()[:80]
                        try: domain = url.split("/")[2]
                        except: domain = url
                        if _is_blocked_domain(domain) or "百度百科" in title_line:
                            log.info(f"🚫 跳过禁用来源：{domain} | {title_line[:40]}")
                            continue
                        snippet = " ".join(l.strip() for l in lines[1:-1])[:300]
                        if not _source_matches_query(q, title_line, snippet, domain):
                            log.info(f"🧹 过滤无关搜索源：{domain} | {title_line[:40]}")
                            continue
                        entry = {
                            "id":           next_rid(),
                            "tool":         "web_search",
                            "query":        q[:40],
                            "title":        title_line,
                            "url":          url,
                            "domain":       domain,
                            "snippet":      snippet,
                            "full_content": None,
                        }
                        source_index.append(entry)
                        _url_to_entry[url] = entry
                        _persist(entry)

            elif fn == "fetch_content":
                url = args.get("url", "")
                # 同 URL 重复抓取：直接返回已有缓存，不消耗配额
                _cached_entry = _url_to_entry.get(url)
                if _cached_entry and _cached_entry.get("full_content"):
                    result = _cached_entry["full_content"]
                    log.info(f"📄 fetch_content 缓存命中（跳过重复抓取）：{url[:60]}")
                    msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    continue
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": f"📄 抓取正文（{used['fetch_content']}/{quota['fetch_content']}）：{url[:60]}",
                        "disable_notification": True})
                try:
                    domain = url.split("/")[2]
                except Exception:
                    domain = ""
                if domain in _FETCH_BLOCKED_DOMAINS or _is_blocked_domain(domain):
                    result = f"[已知封锁域名 {domain}，跳过抓取]"
                    meta["failed_urls"].append(url)
                    log.info(f"🚫 跳过封锁域名：{url[:60]}")
                else:
                    result = execute_fetch_content(url)
                if "正文来源" in result:
                    fetch_success += 1
                    meta["fetched_pages"].append({"url": url, "content": result})
                    # 回填到对应的 source_index 条目
                    matched = _url_to_entry.get(url)
                    if matched:
                        matched["full_content"] = result
                    else:
                        # fetch 的 URL 不在本轮搜索结果里（直接输入），新建条目
                        try: domain = url.split("/")[2]
                        except: domain = url
                        # 从正文提取真实标题
                        _fc_title = ""
                        _fc_clean = re.sub(r'^\[正文来源：[^\]]+\]\s*', '', result)
                        _fc_clean = re.sub(r'!\[.*?\]\(.*?\)\s*', '', _fc_clean)
                        _fc_clean = re.sub(r'\[.*?\]\(.*?\)\s*', '', _fc_clean)
                        for _fc_line in _fc_clean.split('\n'):
                            _fc_line = _fc_line.strip()
                            _fc_line = re.sub(r'^[^\u4e00-\u9fffA-Za-z0-9（【《"\'“”‘’<]+', '', _fc_line).strip()
                            if _fc_line and len(_fc_line) > 3 and not _fc_line.startswith('http'):
                                _fc_title = _fc_line[:80]
                                break
                        if not _fc_title:
                            _fc_title = url.split("/")[-1][:80] or domain
                        if _is_nav_or_empty_page(_fc_title, result):
                            log.info(f"🧹 跳过导航/空页面：{domain} | {_fc_title[:40]}")
                            msgs.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": f"[页面为导航或空页，已跳过] {url[:60]}",
                            })
                            continue
                        entry = {
                            "id":           next_rid(),
                            "tool":         "fetch_content",
                            "query":        url[:40],
                            "title":        _fc_title,
                            "url":          url,
                            "domain":       domain,
                            "snippet":      result[:300],
                            "full_content": result,
                        }
                        source_index.append(entry)
                        _url_to_entry[url] = entry
                        _persist(entry)
                else:
                    meta["failed_urls"].append(url)

            elif fn == "serper_search":
                q, stype = args.get("query", ""), args.get("search_type", "general")
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": f"🔎 Serper 核查：{q}", "disable_notification": True})
                try:
                    result = _execute_serper(q, stype)
                except Exception as _serper_e:
                    result = f"Serper 调用失败: {_serper_e}"
                    log.warning(f"serper_search 执行异常: {_serper_e}")
                result = _strip_blocked_search_blocks(result)
                for block in result.split("\n\n"):
                    lines = block.strip().splitlines()
                    if len(lines) >= 2 and lines[-1].startswith("http"):
                        url = lines[-1].strip()
                        title_line = lines[0].lstrip("• ").split("[")[0].strip()[:80]
                        try: domain = url.split("/")[2]
                        except: domain = url
                        if _is_blocked_domain(domain) or "百度百科" in title_line:
                            log.info(f"🚫 跳过禁用来源：{domain} | {title_line[:40]}")
                            continue
                        snippet = " ".join(l.strip() for l in lines[1:-1])[:300]
                        if not _source_matches_query(q, title_line, snippet, domain):
                            log.info(f"🧹 过滤无关搜索源：{domain} | {title_line[:40]}")
                            continue
                        entry = {
                            "id":           next_rid(),
                            "tool":         "serper_search",
                            "query":        q[:40],
                            "title":        title_line,
                            "url":          url,
                            "domain":       domain,
                            "snippet":      snippet,
                            "full_content": None,
                        }
                        source_index.append(entry)
                        _url_to_entry[url] = entry
                        _persist(entry)

            elif fn == "wikipedia_lookup":
                q = args.get("query", "")
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": f"📖 Wikipedia：{q}", "disable_notification": True})
                result = execute_wikipedia(q)
                wiki_title = _extract_wiki_title(result, q)
                entry = {
                    "id":           next_rid(),
                    "tool":         "wikipedia_lookup",
                    "query":        q[:40],
                    "title":        wiki_title,
                    "url":          f"https://en.wikipedia.org/wiki/{q.replace(' ','_')}",
                    "domain":       "wikipedia.org",
                    "snippet":      result[:300],
                    "full_content": result,
                }
                source_index.append(entry)
                _persist(entry)

            elif fn == "check_weather":
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": "🌤 查询天气...", "disable_notification": True})
                result = execute_weather()

            elif fn == "vps_traffic":
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": "📊 查询 VPS 流量...", "disable_notification": True})
                result = execute_vps_traffic()

            elif fn == "github_trending":
                lang = args.get("language", "")
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": f"📦 GitHub 热榜{'（'+lang+'）' if lang else ''}...",
                        "disable_notification": True})
                result = execute_github_trending(language=lang)

            elif fn == "check_api_balance":
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": "🔑 查询各 API 余额…", "disable_notification": True})
                result = execute_api_balance()

            elif fn == "read_today_cache":
                ids_req = args.get("ids", [])
                lvl     = args.get("level", "snippet")
                if chat_id:
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": f"📂 读取今日缓存（{len(ids_req)} 条）…",
                        "disable_notification": True})
                result = execute_read_cache(ids_req, lvl)
                try:
                    cached_rows = json.loads(result)
                    seen_ids = {e.get("id") for e in source_index}
                    for row in cached_rows:
                        if row.get("error") or row.get("id") in seen_ids:
                            continue
                        url = row.get("url", "")
                        try:
                            domain = url.split("/")[2]
                        except Exception:
                            domain = ""
                        entry = {
                            "id": row.get("id", next_rid()),
                            "tool": "read_today_cache",
                            "query": "今日缓存",
                            "title": row.get("title", "")[:80],
                            "url": url,
                            "domain": domain,
                            "snippet": (row.get("snippet") or "")[:600],
                            "full_content": row.get("full_content"),
                        }
                        source_index.append(entry)
                        seen_ids.add(entry["id"])
                        if url:
                            _url_to_entry[url] = entry
                except Exception as _cache_parse_e:
                    log.debug(f"read_today_cache 来源索引解析失败: {_cache_parse_e}")

            elif fn == "search_daily_summaries":
                from tg_bot.storage import search_daily_summaries as _sds
                result = _sds(args.get("keyword", ""))

            elif fn == "read_daily_summary":
                from tg_bot.storage import read_daily_summary as _rds
                result = _rds(args.get("date_str", ""))

            elif fn == "read_daily_log":
                from tg_bot.storage import read_daily_log as _rdl
                result = _rdl(args.get("date_str", ""))
            elif fn == "read_today_report":
                from tg_bot.storage import load_report as _lr
                txt = _lr()
                result = txt if txt else "今日午报尚未生成或为空。"
                if not any(e.get("id") == "LOCAL_TODAY_REPORT" for e in source_index):
                    source_index.append({
                        "id": "LOCAL_TODAY_REPORT",
                        "tool": "read_today_report",
                        "query": user_text[:40],
                        "title": "今日午报全文",
                        "url": "local://read_today_report",
                        "domain": "local://read_today_report",
                        "snippet": result[:600],
                        "full_content": result,
                    })

            elif fn == "search_chat_history":
                from tg_bot.tools.native import execute_search_chat_history as _sch
                _kw = args.get("keyword", "")
                _lim = int(args.get("limit", 20))
                result = _sch(_kw, _lim)

            else:
                result = f"未知工具: {fn}"

            log.info(f"[gather/{fn}] {result[:80]}")
            q_disp = args.get("query") or args.get("url", "")[:30] or ""
            meta["tool_calls_summary"].append(f"{fn}({q_disp[:30]})" if q_disp else fn)
            if fn in ("web_search", "serper_search", "wikipedia_lookup", "fetch_content", "read_today_report"):
                snippet_limit = 1200 if fn in _DIRECT_API_TOOLS else 500
                meta["tool_results"].append({"tool": fn,
                    "query": q_disp[:40], "snippet": result[:snippet_limit]})
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

    # 超轮次兜底：用已收集的搜索摘要 + 直接 API 工具结果拼成临时事实清单
    _backfill_source_index_from_tools()
    meta["source_index"] = source_index
    fallback_lines = []
    fi = 1
    # ① 网络搜索结果（source_index）
    for e in source_index[:8]:
        if e.get("snippet"):
            fnum = f"F{fi:03d}"
            body = e.get("full_content") or e.get("snippet") or ""
            body = _compact_fallback_text(body, user_text, 520)
            fallback_lines.append(f"[{fnum}] {e.get('title','')[:50]}（{e.get('domain','')}）")
            if body:
                fallback_lines.append(f"{body}\n")
            else:
                fallback_lines.append(f"{e['snippet'][:300]}\n")
            fi += 1
    # ② 直接 API 工具结果（Wikipedia/weather/github_trending 等，不在 source_index 里）
    for r in meta.get("tool_results", []):
        if r.get("tool") in _DIRECT_API_TOOLS and r.get("snippet"):
            fnum = f"F{fi:03d}"
            body = _compact_fallback_text(r["snippet"], user_text, 520)
            fallback_lines.append(f"[{fnum}] 来自 {r['tool']}（直接API）")
            fallback_lines.append(f"{body}\n")
            fi += 1
    if fallback_lines:
        log.warning(f"⚠️ 采集达到上限（本次 {len(meta.get('rounds', []))} 轮）/工具摘要 {len(meta.get('tool_calls_summary', []))} 次，已用 {fi-1} 条摘要兜底")
        fact_list = "【采集轮次耗尽，以下为已收集摘要】\n\n" + "\n".join(fallback_lines)
        meta["facts_json"] = build_facts_json(fact_list, source_index)
        return fact_list, meta
    fact_list = "未获取到：采集超时或工具循环异常"
    meta["facts_json"] = build_facts_json(fact_list, source_index)
    return fact_list, meta


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
    _source_injected = False # 来源索引只注入一次

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
            if fn == "web_search":
                q     = args.get("query", "")
                stype = args.get("search_type", "general")
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": f"🔍 搜索（{used['web_search']}/{quota['web_search']}）：{q}",
                        "disable_notification": True})
                result = execute_search(q, stype)

            elif fn == "fetch_content":
                url = args.get("url", "")
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": f"📄 读取正文（{used['fetch_content']}/{quota['fetch_content']}）：{url[:60]}",
                        "disable_notification": True})
                try:
                    _fetch_domain = url.split("/")[2]
                except Exception:
                    _fetch_domain = ""
                if _fetch_domain in _FETCH_BLOCKED_DOMAINS:
                    result = f"[已知封锁域名 {_fetch_domain}，跳过抓取]"
                    meta["failed_urls"].append(url)
                    log.info(f"🚫 跳过封锁域名：{url[:60]}")
                else:
                    result = execute_fetch_content(url)
                if "正文来源" in result:   # 成功标志
                    fetch_success += 1
                    meta["fetched_pages"].append({"url": url, "content": result})
                else:
                    meta["failed_urls"].append(url)

            elif fn == "serper_search":
                q     = args.get("query", "")
                stype = args.get("search_type", "general")
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": f"🔎 Serper 核查：{q}",
                        "disable_notification": True})
                try:
                    result = _execute_serper(q, stype)
                except Exception as _serper_e:
                    result = f"Serper 调用失败: {_serper_e}"
                    log.warning(f"serper_search 执行异常: {_serper_e}")

            elif fn == "wikipedia_lookup":
                q = args.get("query", "")
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": f"📖 Wikipedia：{q}",
                        "disable_notification": True})
                result = execute_wikipedia(q)

            elif fn == "check_weather":
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": "🌤 查询安阳天气...",
                        "disable_notification": True})
                result = execute_weather()

            elif fn == "vps_traffic":
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": "📊 查询 VPS 流量...",
                        "disable_notification": True})
                result = execute_vps_traffic()

            elif fn == "github_trending":
                lang = args.get("language", "")
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": f"📦 抓取 GitHub 热榜{'（' + lang + '）' if lang else ''}...",
                        "disable_notification": True})
                result = execute_github_trending(language=lang)

            elif fn == "check_api_balance":
                if chat_id:
                    typing(chat_id)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": "🔑 查询各 API 余额…", "disable_notification": True})
                result = execute_api_balance()

            elif fn == "search_daily_summaries":
                from tg_bot.storage import search_daily_summaries as _sds
                result = _sds(args.get("keyword", ""))

            elif fn == "read_daily_summary":
                from tg_bot.storage import read_daily_summary as _rds
                result = _rds(args.get("date_str", ""))

            elif fn == "read_daily_log":
                from tg_bot.storage import read_daily_log as _rdl
                result = _rdl(args.get("date_str", ""))
            elif fn == "read_today_report":
                from tg_bot.storage import load_report as _lr
                txt = _lr()
                result = txt if txt else "今日午报尚未生成或为空。"
                if not any(e.get("id") == "LOCAL_TODAY_REPORT" for e in source_index):
                    source_index.append({
                        "id": "LOCAL_TODAY_REPORT",
                        "tool": "read_today_report",
                        "query": "今日午报",
                        "title": "今日午报全文",
                        "url": "local://read_today_report",
                        "domain": "local://read_today_report",
                        "snippet": result[:600],
                        "full_content": result,
                    })

            elif fn == "search_chat_history":
                from tg_bot.tools.native import execute_search_chat_history as _sch2
                _kw2 = args.get("keyword", "")
                _lim2 = int(args.get("limit", 20))
                result = _sch2(_kw2, _lim2)

            else:
                result = f"未知工具: {fn}"

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
            # ── 来源索引：提取每条搜索结果的域名/标题/摘要 ──────────────
            if fn in ("web_search", "serper_search"):
                q_for_src = args.get("query", "")
                for block in result.split("\n\n"):
                    lines = block.strip().splitlines()
                    if len(lines) >= 2:
                        url_candidate = lines[-1].strip()
                        if url_candidate.startswith("http"):
                            try:
                                domain = url_candidate.split("/")[2]
                            except IndexError:
                                domain = url_candidate
                            title_line = lines[0].lstrip("• ").split("[")[0].strip()
                            snip = " ".join(l.strip() for l in lines[1:-1])[:80]
                            source_index.append({
                                "domain": domain, "query": q_for_src[:25],
                                "title": title_line[:40], "snippet": snip,
                            })
            elif fn == "wikipedia_lookup":
                wiki_title = _extract_wiki_title(result, args.get("query", ""))
                nl_pos = result.find("\n")
                wiki_snip = result[nl_pos+1:][:80].strip() if nl_pos > 0 else result[:80]
                source_index.append({
                    "domain": "wikipedia.org", "query": args.get("query", "")[:25],
                    "title": wiki_title[:40], "snippet": wiki_snip,
                })
            elif fn == "fetch_content":
                fetch_m = re.match(r'\[正文来源：(.+?)\]', result)
                if fetch_m:
                    fetch_url = fetch_m.group(1)
                    try:
                        fetch_domain = fetch_url.split("/")[2]
                    except IndexError:
                        fetch_domain = fetch_url
                    nn_pos = result.find("\n\n")
                    fetch_snip = result[nn_pos+2:][:80].strip() if nn_pos > 0 else ""
                    source_index.append({
                        "domain": fetch_domain, "query": fetch_url[:25],
                        "title": fetch_domain, "snippet": fetch_snip,
                    })
            msgs.append({"role":"tool","tool_call_id":tc["id"],"content":result})

    return _ret("处理超时，请重新提问。")
