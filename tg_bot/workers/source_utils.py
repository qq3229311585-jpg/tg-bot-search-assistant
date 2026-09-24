#!/usr/bin/env python3
"""workers/source_utils.py — 来源处理纯工具函数

从 pipeline/gather.py 提取，均为无副作用纯函数，可单独测试。
"""
from __future__ import annotations
import logging
import re
from typing import Optional

from tg_bot.core.contracts import Source

log = logging.getLogger(__name__)


# ── 查询信号提取 ──────────────────────────────────────────────────────────────

_STOP_WORDS = {
    "什么", "怎么", "如何", "为什么", "为啥", "哪些", "哪个", "以及",
    "今天", "昨天", "明天", "最近", "最新", "新闻", "消息", "情况", "问题",
    "一下", "一个", "一些", "这个", "那个", "这些", "那些", "请问", "帮我",
    "看看", "说说", "是否", "是不是", "有啥", "有没有", "可以", "能否",
}

_ZH_EN_BRIDGE = {
    "龙卷风": "tornado", "地震": "earthquake", "台风": "typhoon",
    "飓风": "hurricane", "洪水": "flood", "海啸": "tsunami",
    "火山": "volcano", "暴风雪": "blizzard", "干旱": "drought",
    "纳斯达克": "nasdaq", "标普": "S&P 500", "道琼斯": "dow jones",
    "比特币": "bitcoin", "以太坊": "ethereum",
    "人工智能": "AI artificial intelligence", "机器学习": "machine learning",
}


def query_signals(text: str) -> list[str]:
    """从查询词提取用于相关性比对的短信号列表。"""
    text = (text or "").strip()
    if not text:
        return []
    signals, seen = [], set()

    def _add(sig: str):
        sig = (sig or "").strip().lower()
        if len(sig) < 2 or sig in _STOP_WORDS or sig in seen:
            return
        seen.add(sig)
        signals.append(sig)

    for chunk in re.split(r"[\s,，。！？?!、;；:：/\\|()\[\]{}<>《》""\"'`]+", text):
        if len(chunk.strip()) >= 2:
            _add(chunk.strip())

    for m in re.finditer(r"[一-鿿]{2,}", text):
        seq = m.group(0)
        _add(seq)
        for i in range(len(seq) - 1):
            _add(seq[i:i + 2])

    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_-]{2,}", text):
        _add(m.group(0))

    # 中英文桥接
    for zh, en in _ZH_EN_BRIDGE.items():
        if zh in text:
            for part in en.split():
                _add(part)

    return signals


def source_matches_query(query: str, title: str, snippet: str,
                         domain: str = "", url: str = "",
                         strict: bool = False) -> bool:
    """判断来源是否与查询相关，过滤明显跑题内容。"""
    signals = query_signals(query)
    if not signals:
        return True
    url_path = (url or "").split("?", 1)[0].split("#", 1)[0]
    hay = f"{title} {snippet} {domain} {url_path}".lower()
    hits = [s for s in signals if s and s in hay]
    if not hits:
        return False
    if strict and len(signals) >= 3 and len(hits) < 2:
        return False
    return True


def compact_excerpt(text: str, query: str = "", limit: int = 520) -> str:
    """从长原文中提取最像答案的片段。"""
    text = (text or "").strip()
    if not text:
        return ""
    signals = [s for s in query_signals(query) if len(s) >= 2]
    junk_re = re.compile(
        r"(skip to|donate|menu|navigation|subscribe|sign in|login|privacy|cookie|"
        r"official website|here.s how you know|journalism of courage|news headlines|"
        r"gift cards?|buy a gift|recommended by|logo|search icon|facebook|instagram|"
        r"newsletter|pageview|adservice)",
        re.I,
    )
    lines = [
        ln.strip() for ln in text.splitlines()
        if ln.strip() and len(ln.strip()) >= 25 and not junk_re.search(ln)
    ]
    if signals:
        hits = [ln for ln in lines if any(s in ln.lower() for s in signals)]
        if hits:
            return "\n".join(hits[:4])[:limit]
    return "\n".join(lines[:4])[:limit] if lines else text[:limit]


# ── 来源打分 ─────────────────────────────────────────────────────────────────

_NAV_TITLES = {
    "an official website of the united states",
    "an official website of the united states government",
    "news headlines", "菜单导航", "navigation",
    "journalism of courage", "要闻推荐",
}

_DIRECT_API_TOOLS = {
    "check_weather", "vps_traffic", "github_trending", "check_api_balance",
    "calendar_query", "calendar_add", "read_today_report",
}

FETCH_BLOCKED_DOMAINS = {"zhihu.com", "www.zhihu.com"}

_CACHE_GENERIC_TERMS = {
    "中国", "今天", "昨天", "明天", "年份", "时间", "时候", "哪里", "哪年", "哪一", "第一", "一个",
}


_LONG_BODY_TOOLS = {"fetch_content", "wikipedia_lookup", "read_today_cache"}
_SNIPPET_TOOLS = {"web_search", "serper_search"}


def is_nav_or_empty(title: str, body: str, tool: str = "") -> bool:
    """是否是导航页 / 空页（应过滤）。

    不同工具的“正常长度”差别很大：抓全文/查百科应有几百字以上，
    搜索摘要本来就短，直接 API 工具的返回从不按长度过滤。
    """
    body_len = len((body or "").strip())
    if body_len == 0:
        return True
    tool = (tool or "").strip()
    if tool in _DIRECT_API_TOOLS:
        min_len = 1
    elif tool in _SNIPPET_TOOLS:
        min_len = 40
    elif tool in _LONG_BODY_TOOLS or not tool:
        min_len = 200
    else:
        min_len = 40
    if body_len < min_len:
        return True
    return any(t and t in (title or "").lower().strip() for t in _NAV_TITLES)


def cache_match_score(entry_text: str, keywords) -> int:
    """今日库存/缓存命中评分：按关键词命中数计分，不对特定问题加分。"""
    text = (entry_text or "").lower()
    score = 0
    for k in keywords or []:
        k = (k or "").strip().lower()
        if len(k) < 2 or k in _CACHE_GENERIC_TERMS:
            continue
        if k in text:
            score += 1
    return score


def score_source(s: Source, query: str = "") -> float:
    """给 Source 打综合质量分（越高越好）。"""
    score = 0.0
    domain = (s.domain or "").lower()
    title = (s.title or "").lower()
    tool = (s.tool or "").lower()
    body = s.body

    # 工具权重
    if tool in ("fetch_content",):
        score += 40       # 抓到全文，信息量最大
    elif tool in ("wikipedia_lookup",):
        score += 20
    elif tool in ("web_search", "serper_search"):
        score += 10
    elif tool in _DIRECT_API_TOOLS:
        score += 30       # 直接 API，精准

    # 内容长度
    body_len = len(body)
    if body_len > 2000:
        score += 15
    elif body_len > 500:
        score += 8
    elif body_len < 100:
        score -= 10

    # 域名权威性
    if domain.endswith((".gov", ".edu")):
        score += 12
    if domain in ("wikipedia.org", "en.wikipedia.org", "zh.wikipedia.org"):
        score += 5
    if any(k in title for k in ("官网", "official", "学校概况", "学校简介")):
        score -= 5        # 官网容易是导航页

    # 相关性
    if query and source_matches_query(query, s.title, s.snippet[:200], s.domain, s.url, strict=True):
        score += 20
    elif query and source_matches_query(query, s.title, s.snippet[:200], s.domain, s.url):
        score += 8

    return score


def deduplicate(sources: list[Source]) -> list[Source]:
    """去重：直接 API 工具只保留最新一条；同 URL 有摘要又有全文时保留全文更长者。"""
    seen_tools: dict[str, int] = {}
    by_key: dict[str, int] = {}   # key -> result 中的下标
    result: list[Source] = []

    # 先扫一遍，记录每个直接API工具最后出现的位置
    for i, s in enumerate(sources):
        if s.tool in _DIRECT_API_TOOLS:
            seen_tools[s.tool] = i

    for i, s in enumerate(sources):
        # 直接API工具：只保留最新
        if s.tool in _DIRECT_API_TOOLS:
            if seen_tools.get(s.tool) != i:
                continue
        # 其他：按 url / snippet 去重，同 key 保留正文更长（更完整）的一条
        key = s.url or (s.snippet or "")[:60]
        if key and key in by_key:
            existing_idx = by_key[key]
            if len(s.body) > len(result[existing_idx].body):
                result[existing_idx] = s
            continue
        if key:
            by_key[key] = len(result)
        result.append(s)

    return result


def format_sources_for_writer(sources: list[Source], max_body: int = 2500) -> str:
    """把编号 sources 格式化为 Writer 使用的 [来源N] 素材文本。

    列表下标+1 = 编号，Writer/Critic/Patcher/claim_check 必须用同一份 sources 列表，
    保证编号一致。空 body 的来源应在 Curator 阶段就被剔除；这里跳过只是兜底防御，
    一旦触发会记日志，因为会导致编号跳号。
    """
    lines = []
    for i, s in enumerate(sources, 1):
        title = (s.title or s.domain or "")[:80]
        domain = (s.domain or s.tool or "")[:40]
        body = s.body.replace("\n", " ").strip()[:max_body]
        if not body:
            log.warning(f"format_sources_for_writer: 来源{i}（{s.url or domain}）正文为空，被跳过，"
                        "会导致后续编号跳号，应在 Curator 阶段过滤")
            continue
        published = (getattr(s, "published_at", "") or "").strip()
        date_suffix = f"，发布于 {published}" if published else ""
        lines.append(f"[来源{i}] {title}（{domain}{date_suffix}）")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def extract_wiki_title(result: str, query: str) -> str:
    """解析 wikipedia_lookup 返回的标题行。"""
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
