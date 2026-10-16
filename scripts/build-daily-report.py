#!/usr/bin/env python3
"""Build the daily news report without starting Telegram polling."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tg_bot.daily_report import (  # noqa: E402
    NewsCandidate,
    cluster_candidates,
    normalize_candidate,
    render_daily_report,
    select_events,
    select_visible_event_sections,
)
from tg_bot.report_sections import (  # noqa: E402
    DEFAULT_REPORT_SECTIONS,
    EVENT_SECTION_IDS,
    get_section_collector,
    register_section_collector,
    resolve_sections,
    split_external_sections,
)


_CATEGORY_QUERIES = {
    section.id: section.query
    for section in DEFAULT_REPORT_SECTIONS
    if section.kind == "event" and section.query
}

# ── 冷知识话题轮盘：按日期轮转主题，保证每天的查询词都不同 ──────────────
COLD_TOPIC_WHEEL = (
    "深海生物", "天文与宇宙", "古罗马", "古埃及", "中世纪欧洲", "中国古代科技",
    "数学悖论", "人体奥秘", "动物行为", "植物", "微生物", "材料科学",
    "语言学", "地理边界", "气象现象", "火山与地震", "南极与北极", "昆虫",
    "鸟类迁徙", "海洋", "货币史", "地图", "度量衡", "颜色科学",
    "声音与听觉", "睡眠科学", "大脑与记忆", "病毒与免疫", "化学元素", "光学错觉",
    "概率与统计", "桥梁与建筑", "铁路与航海", "食物起源", "文字演化", "历法与时间",
)

# 要闻类板块必须有 24 小时内的发布日期，无日期的常青内容直接淘汰
_DATED_ONLY_SECTIONS = {"china", "global", "ai_tech"}

# 新闻源黑名单：内容农场 / 广告垃圾 / 政治敏感倾向性站点，一律不进日报
_BLOCKED_NEWS_DOMAINS = {
    "bannedbook.org", "epochtimes.com", "ntdtv.com", "aboluowang.com",
    "soundofhope.org", "secretchina.com", "renminbao.com", "minghui.org",
    "kanzhongguo.com", "creaders.net", "wenxuecity.com",
    "baike.baidu.com",
}


def _is_blocked_news_domain(domain: str) -> bool:
    domain = (domain or "").lower()
    if not domain:
        return False
    return any(domain == d or domain.endswith("." + d) for d in _BLOCKED_NEWS_DOMAINS)


def _url_domain(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        host = urlsplit(url).netloc.lower().split("@")[-1].split(":", 1)[0]
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def cold_topic_for(now=None) -> str:
    now = now or datetime.now(timezone.utc)
    day_of_year = now.astimezone(ZoneInfo("Asia/Shanghai")).timetuple().tm_yday
    return COLD_TOPIC_WHEEL[day_of_year % len(COLD_TOPIC_WHEEL)]


def query_for_category(category: str, now=None) -> str:
    return _CATEGORY_QUERIES.get(category, f"{category} 今日 新闻")


def _parse_search_output(result: str, category: str, source: str) -> list[NewsCandidate]:
    """Parse the plain-text format returned by existing search providers."""
    candidates = []
    for block in re.split(r"\n\s*\n", result or ""):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        title = lines[0].lstrip("•-").strip()
        url_index = next((index for index, line in enumerate(lines) if line.startswith("http")), None)
        if url_index is None or not title:
            continue
        url = lines[url_index].split()[0]
        summary = " ".join(lines[1:url_index]).strip()
        date_match = re.search(r"\[(\d{4}[^\]]*)\]", title)
        published = date_match.group(1) if date_match else None
        title = re.sub(r"\s*\[[^\]]+\]\s*$", "", title).strip()
        candidates.append(normalize_candidate({
            "title": title,
            "summary": summary,
            "url": url,
            "published_date": published,
        }, category, source))
    return candidates


_DIGEST_TITLE_RE = re.compile(r"\d{1,2}月\d{1,2}日.{0,8}(热点|要闻)?(汇总|小时报|早报|晚报|速览|一览|盘点|合集)\s*$")
_DIGEST_KEYWORDS = ("汇总", "小时报", "早报", "晚报", "午报", "速览", "一览", "合集", "榜单", "排行榜", "直播", "专题")


def _is_digest_item(candidate) -> bool:
    """Return True for multi-story roundup pages (汇总/快讯杂烩), not single news items."""
    title = str(getattr(candidate, "title", "") or "")
    summary = str(getattr(candidate, "summary", "") or "")
    if _DIGEST_TITLE_RE.search(title):
        return True
    if any(keyword in title for keyword in _DIGEST_KEYWORDS) and summary.count("；") >= 3:
        return True
    return False


def _collect_one_category(category, execute_news_candidates):
    """Collect one event lane in isolation for bounded parallel execution."""
    query = query_for_category(category)
    try:
        raw_items, provider_diagnostics = execute_news_candidates(query)
        found = [
            normalize_candidate(item, category, item.get("source", "news"))
            for item in raw_items
        ]
        found = [item for item in found if not _is_blocked_news_domain(item.domain)]
        digest_diagnostics = []
        kept = []
        for item in found:
            if _is_digest_item(item):
                digest_diagnostics.append(f"{category}: digest:{(item.title or '')[:20]}:dropped")
            else:
                kept.append(item)
        found = kept
        diagnostics = [f"{category}: {item}" for item in provider_diagnostics]
        diagnostics.extend(digest_diagnostics)
        if not found and not provider_diagnostics:
            diagnostics.append(f"{category}: no_candidates")
        return found, diagnostics
    except Exception as exc:
        return [], [f"{category}: provider_error:{type(exc).__name__}"]


def collect_candidates(categories: Iterable[str] | None = None):
    """Collect news candidates with bounded concurrency and stable output order."""
    from tg_bot.config import DAILY_REPORT_MAX_WORKERS
    from tg_bot.tools.search import execute_news_candidates_all

    requested = tuple(categories or EVENT_SECTION_IDS)
    candidates = []
    diagnostics = []
    event_categories = [category for category in requested if category in _CATEGORY_QUERIES]
    results = {}
    worker_count = min(max(1, int(DAILY_REPORT_MAX_WORKERS)), max(1, len(event_categories)))
    if event_categories:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="daily-report") as pool:
            futures = {
                category: pool.submit(_collect_one_category, category, execute_news_candidates_all)
                for category in event_categories
            }
            # Read results in request order, not completion order, so report
            # ordering and diagnostics remain deterministic across runs.
            for category in event_categories:
                results[category] = futures[category].result()
    for category in requested:
        # Snapshot lanes are intentionally not sent through a news provider;
        # they are supplied by native/external collectors and preserved below.
        if category not in _CATEGORY_QUERIES:
            diagnostics.append(f"{category}: snapshot_or_no_news_collector")
            continue
        found, category_diagnostics = results[category]
        candidates.extend(found)
        diagnostics.extend(category_diagnostics)
    return candidates, diagnostics


def research_selected_events(
    eligible_selected,
    budget,
    search_more,
    fetch,
    summary_call,
    verify_call,
    *,
    evidence_top_k,
    summary_retries,
    verify_retries,
    diagnostics=None,
    max_workers=6,
):
    """Research, summarize, and independently verify the hottest lane events.

    每条事件的"深挖证据→摘要→独立核查"链彼此独立，用线程池并发执行——
    这是日报生成里最大的串行耗时；结果与诊断按原板块/热度顺序合并，
    输出与串行版本完全一致。
    """
    from tg_bot.report_research import DroppedSummary, deepen_event, summarize_bundle, verify_summary

    notes = diagnostics if diagnostics is not None else []
    lane_order = list(dict.fromkeys(event.category for event in eligible_selected))
    candidates = []
    for category in lane_order:
        lane = sorted(
            (event for event in eligible_selected if event.category == category),
            key=lambda event: (-event.heat_score, event.event_id),
        )[:max(1, int(evidence_top_k))]
        candidates.extend(lane)
    if not candidates:
        return []

    def _research_one(event):
        prefix = f"{event.category}:{event.event_id}"
        try:
            bundle = deepen_event(event, search_more, fetch, budget)
        except Exception as exc:
            return None, f"{prefix}:research_error:{type(exc).__name__}"
        if bundle is None:
            return None, f"{prefix}:evidence_insufficient"
        summary = summarize_bundle(bundle, summary_call, retries=summary_retries)
        if isinstance(summary, DroppedSummary):
            return None, f"{prefix}:dropped"
        if summary is None:
            return None, f"{prefix}:summary_failed"
        if not verify_summary(bundle, summary, verify_call, retries=verify_retries):
            return None, f"{prefix}:verify_failed"
        cited = set(summary.supporting_source_ids)
        verified_sources = tuple(
            NewsCandidate(
                category=event.category,
                title=source.title,
                summary=source.snippet or source.full_content,
                url=source.url,
                domain=source.domain,
                published_at=source.published_at,
                relevance=0.0,
                source=source.provider,
            )
            for source in bundle.sources
            if source.source_id in cited
        )
        result = replace(
            event, title=summary.title, summary=summary.summary, sources=verified_sources,
        )
        return result, f"{prefix}:verified"

    chosen_by_id = {}
    with ThreadPoolExecutor(
        max_workers=min(max_workers, len(candidates)), thread_name_prefix="report-research"
    ) as pool:
        futures = {pool.submit(_research_one, event): event for event in candidates}
        for future, event in futures.items():
            try:
                result, note = future.result()
            except Exception as exc:
                result, note = None, f"{event.category}:{event.event_id}:research_error:{type(exc).__name__}"
            notes.append(note)
            if result is not None:
                chosen_by_id[event.event_id] = result

    # 并发完成顺序不固定，按原本板块顺序 + 热度顺序重排，保证输出确定性。
    return [chosen_by_id[event.event_id] for event in candidates if event.event_id in chosen_by_id]


# ── 日报中文化：英文标题/摘要/热帖批量翻译（一次调用，失败保留原文） ────
_CJK_RE = re.compile(r"[一-鿿]")
_WORD_RE = re.compile(r"[A-Za-z一-鿿]")


def _needs_translation(text: str) -> bool:
    text = str(text or "")
    words = _WORD_RE.findall(text)
    if len(words) < 8:
        return False
    cjk = len(_CJK_RE.findall(text))
    return cjk / len(words) < 0.4


def _translate_texts_to_chinese(texts: Mapping[str, str]) -> dict:
    """批量翻译 {key: text}；返回成功翻译的子集，任何失败都安全降级为空。"""
    pending = {
        k: str(v)[:1600 if k.startswith("sec_") else 600]
        for k, v in texts.items() if _needs_translation(v)
    }
    if not pending:
        return {}
    try:
        import importlib
        config = importlib.import_module("tg_bot.config")
        from tg_bot.tools.fetch import http_post
        resp = http_post(
            f"{getattr(config, 'DEEPSEEK_API_BASE', 'https://api.deepseek.com')}/chat/completions",
            {
                "model": getattr(config, "DEEPSEEK_MODEL", "deepseek-v4-flash"),
                "messages": [
                    {"role": "system", "content": (
                        "你是翻译器。把用户给出的 JSON 对象里每个值翻译成简体中文：\n"
                        "· 外文句子翻成自然流畅的中文\n"
                        "· URL、数字、版本号、人名/公司名/产品名/仓库名等专有名词保留原样\n"
                        "· emoji、•、换行、HTML 标签（如 <blockquote expandable>）原样保留\n"
                        "· 已经是中文的内容原样返回\n"
                        "只输出 JSON 对象，键必须与输入完全一致，不加任何解释。"
                    )},
                    {"role": "user", "content": json.dumps(pending, ensure_ascii=False)},
                ],
                "max_tokens": 7000,
                "temperature": 0.1,
                "thinking": {"type": "disabled"},
            },
            headers={"Authorization": f"Bearer {config.DEEPSEEK_KEY}"},
            timeout=120,
        )
        raw = (resp["choices"][0]["message"].get("content") or "").strip() if resp and resp.get("choices") else ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {}
        data = json.loads(m.group(0))
        return {k: str(v).strip() for k, v in data.items() if k in pending and str(v).strip()}
    except Exception as exc:
        print(f"WARNING translate: {type(exc).__name__}", file=sys.stderr)
        return {}


_SUMMARY_PROMPT = """\
你是日报事实汇总器。只依据用户 JSON 中 sources 的字段、snippet 和 full_content，
生成一条简体中文事实摘要；不得补充材料外背景、游戏介绍、冷知识、评价或主观看法。
只输出一个 JSON 对象，字段必须恰好为：
id、title、summary、supporting_source_ids、conflicts、drop。
id 原样返回；title 必须非空且不超过24个汉字（含标点，超出会被直接拒绝，请务必精简）；
summary 必须非空；supporting_source_ids 至少引用两个独立域名，
除非唯一可用来源是官方/权威域名（如政府网站、官方 GitHub、路透/美联社等通讯社），
这种情况下允许只引用这一个来源；conflicts 是字符串数组；drop 是布尔值。只有以下两种情况才设为 true:①材料明显不是一条具体新闻/事件，如栏目页、频道首页、聚合/汇总页、排行榜页、纯广告；②内容与 category 明显无关。拿不准时一律为 false。drop 为 true 时 title 和 summary 仍须填非空占位文字（如“丢弃”），其他字段照常给出。
若来源说法冲突，summary 必须明确写出分歧。不得输出任何额外字段。"""

_VERIFY_PROMPT = """\
你是独立事实核查器。只核查用户 JSON 中 summary 是否被随附 sources 支持。
只输出一个 JSON 对象，字段必须恰好为：verdict、unsupported_claims、supporting_source_ids。
verdict 只能是 pass、fail 或 unknown；unsupported_claims 必须是字符串数组；
supporting_source_ids 只能使用输入中存在的来源编号。证据不足或不确定时不得输出 pass。"""


def _report_model_call(system_prompt, payload, *, verify=False):
    try:
        import importlib
        config = importlib.import_module("tg_bot.config")
        from tg_bot.tools.fetch import http_post

        key = config._next_verify_key() if verify else config._next_ds_key()
        model = (
            getattr(config, "DEEPSEEK_MODEL_VERIFY", "deepseek-v4-flash") if verify
            else getattr(config, "DEEPSEEK_MODEL", "deepseek-v4-flash")
        )
        response = http_post(
            f"{getattr(config, 'DEEPSEEK_API_BASE', 'https://api.deepseek.com')}/chat/completions",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                "max_tokens": 5000 if not verify else 2500,
                "temperature": 0.0,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
            },
            headers={"Authorization": f"Bearer {key}"},
            timeout=150,
        )
        raw = (
            response["choices"][0]["message"].get("content") or ""
            if isinstance(response, Mapping) and response.get("choices") else ""
        ).strip()
        return json.loads(raw) if raw else None
    except Exception as exc:
        label = "verify" if verify else "summary"
        print(f"WARNING daily_report_{label}_call:{type(exc).__name__}", file=sys.stderr)
        return None


def _summary_call(payload):
    return _report_model_call(_SUMMARY_PROMPT, payload, verify=False)


def _verify_call(payload):
    return _report_model_call(_VERIFY_PROMPT, payload, verify=True)


_COLD_KNOWLEDGE_PROMPT = """\
你是中文冷知识科普作者，只写你确有把握的真实知识，绝不编造。
围绕用户给定的 topic 主题，写一条具体、准确、有细节（数字、年代、具体实例）的冷知识，
让读者读完有"原来如此"的感觉。
硬性要求：
· content 是一段连续的中文正文，120～200 字，不分点、不用列表、不加小标题
· 直接给出信息，不写"你知道吗""是不是很神奇""有趣的是"这类废话开头
· 不得使用 avoid 数组里已经出现过的具体知识点或说法，也不得只是换个说法重讲同一件事，
  必须是全新的、更具体的角度
· 没有十足把握的具体数字/年代/名称，宁可省略或改用模糊表述，绝不能编造
· 涉及天文、时间或数字的比较，必须区分清楚定义（例如恒星日与太阳日、自转与公转）；不确定就不要写比较，只陈述有把握的单个事实。
只输出一个 JSON 对象，字段恰好为 content（正文字符串），不得输出任何额外字段、
解释或 markdown 代码块标记。"""


def generate_cold_knowledge(topic, avoid_summaries):
    """按 topic 生成一段 120～200 字的中文冷知识正文；AI 不可用/解析失败一律返回 None。

    调用方（cold_knowledge_collector）必须把 None 当作"今天这块没有"处理，整块跳过，
    绝不能用空文案或占位符硬塞进日报——这是本项目一贯坚持的原则。
    """
    payload = {
        "topic": str(topic or ""),
        "avoid": [str(item).strip() for item in (avoid_summaries or []) if str(item or "").strip()],
    }
    data = _report_model_call(_COLD_KNOWLEDGE_PROMPT, payload, verify=False)
    if not isinstance(data, Mapping):
        return None
    content = re.sub(r"[ \t]+", " ", str(data.get("content") or "")).strip().strip("\"'")
    if not (60 <= len(content) <= 320):
        return None
    return content


_COLD_GIST_STRIP_RE = re.compile(r"[^一-鿿A-Za-z0-9]")
_COLD_GIST_SPLIT_RE = re.compile(r"[，,。！？!?；;\s]")


def cold_knowledge_novelty_label(topic, body):
    """把 topic + 正文首句浓缩成 novelty ledger 能接受的短标签。

    ``storage.normalize_novelty_summary`` 只收不含任何标点/空格的短标签（纯中文/
    字母数字，最多用 · — - 连接两三段），生成的冷知识正文是完整句子，直接存整段
    必定被拒——那样账本永远存不进东西，等于没做防重复。这里从正文第一个分句里
    提炼一个短 gist，和当天的轮盘主题拼成「主题·gist」，能通过校验又保留辨识度。
    """
    first_clause = _COLD_GIST_SPLIT_RE.split(str(body or ""), maxsplit=1)[0]
    gist = _COLD_GIST_STRIP_RE.sub("", first_clause)[:16]
    return f"{topic}·{gist}" if gist else str(topic or "")


def _split_link_block(text):
    """把 <blockquote> 链接块从板块文本里摘出来，返回 (核心文本, 链接块)。"""
    cut = str(text or "").find("<blockquote")
    if cut < 0:
        return str(text or ""), ""
    return text[:cut].rstrip(), text[cut:]


_SNAPSHOT_ENRICH_PROMPT = """\
你是日报编辑，为快照板块的每一条追加一段简体中文说明。读者是普通人，不会点开原文，
说明必须让不懂技术的人也看得懂。

hn_items：每条给了标题，以及抓取到的原文正文（可能为空）。为每条给出 desc：
  2～4 句话，基于正文讲清楚这件事具体是什么、有什么关键信息或进展；
  禁止只是换个说法复述标题；正文为空时只依据标题和已知事实，说明保持简短，不要推断任何细节。

steam_items：每条给了游戏名、官方简介、类型标签，以及一段网上搜到的相关资料（可能为空）。
  为每条给出 desc：2～4 句话，综合官方简介和搜索资料写清楚这是什么类型/题材的游戏、
  大致玩法是什么、口碑或话题度如何；只能使用给定材料里的信息，绝不编造评分、销量等
  具体数字；材料不足时只写类型和玩法即可。

github_items：每条给了开源项目名（owner/repo）、编程语言和官方一句话描述（可能为空或
  全是行话）。为每条给出 desc：1～2 句大白话，说清这个项目是干什么用的、给谁用、解决
  什么问题——把行话翻译成人能听懂的话（例如"本地优先的代码智能图谱"要解释成
  "帮 AI 编程工具快速看懂你整个代码库的工具"）。描述缺失或空洞时，按项目名和语言
  说明它大概是哪类工具即可，不得编造具体功能和数据。

只输出 JSON：{"hn": [{"i": 整数, "desc": "..."}], "steam": [{"i": 整数, "desc": "..."}],
"github": [{"i": 整数, "desc": "..."}]}
每个数组的元素数量必须和输入的对应数组一致，i 从 0 开始且不重复。"""

_APP_ID_RE = re.compile(r"/app/(\d+)/?")


def _snapshot_item_lines(section_text):
    """拆出快照板块正文里的 • 条目行，返回 (标题行, 条目行列表, 链接引用块)。"""
    core, suffix = _split_link_block(section_text)
    lines = core.rstrip().splitlines()
    heading = lines[0] if lines else ""
    items = [line for line in lines[1:] if line.strip().startswith("•")]
    return heading, items, suffix


def _snapshot_item_urls(suffix):
    """从链接引用块里按顺序取出 URL 列表。"""
    return re.findall(r"^\d+\.\s*(\S+)", suffix or "", flags=re.MULTILINE)


def _item_title(bullet_line):
    """从「• 标题  其它字段」这类行里摘出标题本身（Steam 名字带 <b> 标签，剥掉）。"""
    text = bullet_line.strip().lstrip("•").strip()
    return re.sub(r"</?b>", "", text.split("  ")[0]).strip()


def _steam_official_context(appid):
    """查官方简介 + 类型标签，供 AI 综合参考；失败返回 None。"""
    from tg_bot.tools.fetch import http_get
    try:
        raw = http_get(
            f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=cn&l=schinese",
            headers={"Accept": "application/json"}, timeout=15,
        )
        if not raw:
            return None
        data = (json.loads(raw).get(str(appid)) or {}).get("data") or {}
        if not data:
            return None
        genres = "、".join(
            g.get("description", "") for g in (data.get("genres") or []) if g.get("description")
        )
        desc = re.sub(r"<[^>]+>", "", data.get("short_description") or "").strip()
        return {"official_description": desc[:400], "genres": genres}
    except Exception:
        return None


def _github_section_items(section_text):
    """GitHub 板块条目：<b>owner/repo</b> · 语言 · +N★ 标题行 + 紧随的描述行。"""
    lines = str(section_text or "").rstrip().splitlines()
    heading = lines[0] if lines else ""
    items = []
    current_title = None
    current_desc = []
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("<b>"):
            if current_title is not None:
                items.append((current_title, " ".join(current_desc).strip()))
            current_title = stripped
            current_desc = []
        elif stripped and current_title is not None:
            current_desc.append(stripped)
    if current_title is not None:
        items.append((current_title, " ".join(current_desc).strip()))
    return heading, items


def enrich_snapshot_sections(preserved_sections):
    """给 Steam / HN / GitHub 板块的每条追加人话说明；任何失败都原样降级。"""
    from tg_bot.tools.search import execute_search
    from tg_bot.workers.gather_executor import execute_fetch_content

    steam_heading, steam_items, steam_suffix = _snapshot_item_lines(preserved_sections.get("steam") or "")
    hn_heading, hn_items, hn_suffix = _snapshot_item_lines(preserved_sections.get("hackernews") or "")
    github_heading, github_items = _github_section_items(preserved_sections.get("github") or "")
    if not steam_items and not hn_items and not github_items:
        return preserved_sections

    hn_urls = _snapshot_item_urls(hn_suffix)
    steam_urls = _snapshot_item_urls(steam_suffix)
    steam_appids = [
        (match.group(1) if (match := _APP_ID_RE.search(url)) else None)
        for url in steam_urls
    ]

    hn_articles = [None] * len(hn_items)
    steam_official = [None] * len(steam_items)
    steam_search = [None] * len(steam_items)
    jobs = []
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="snapshot-enrich") as pool:
        for i in range(len(hn_items)):
            if i < len(hn_urls) and hn_urls[i]:
                jobs.append(("hn", i, pool.submit(execute_fetch_content, hn_urls[i])))
        for i in range(len(steam_items)):
            if i < len(steam_appids) and steam_appids[i]:
                jobs.append(("steam_official", i, pool.submit(_steam_official_context, steam_appids[i])))
            title = _item_title(steam_items[i])
            if title:
                jobs.append(("steam_search", i, pool.submit(execute_search, f"{title} 游戏 玩家评价")))
        for kind, index, future in jobs:
            try:
                result = future.result()
            except Exception:
                result = None
            if kind == "hn":
                text = str(result or "")
                hn_articles[index] = None if text.startswith("正文抓取失败") else text
            elif kind == "steam_official":
                steam_official[index] = result
            elif kind == "steam_search":
                steam_search[index] = result

    payload = {
        "hn_items": [
            {"title": _item_title(item), "article": (hn_articles[i] or "")[:2000]}
            for i, item in enumerate(hn_items)
        ],
        "steam_items": [
            {
                "name": _item_title(item),
                "official_description": (steam_official[i] or {}).get("official_description", ""),
                "genres": (steam_official[i] or {}).get("genres", ""),
                "search_notes": (steam_search[i] or "")[:1500],
            }
            for i, item in enumerate(steam_items)
        ],
        "github_items": [
            {
                "project": re.sub(r"</?b>", "", title_line),
                "official_description": desc[:300],
            }
            for title_line, desc in github_items
        ],
    }
    data = _report_model_call(_SNAPSHOT_ENRICH_PROMPT, payload, verify=False)
    if not data:
        return preserved_sections

    def _apply(heading, items, suffix, key):
        if not items:
            return None
        descs = {}
        for entry in (data.get(key) or []) if isinstance(data, Mapping) else []:
            if not isinstance(entry, Mapping):
                continue
            index = entry.get("i")
            desc = str(entry.get("desc") or "").strip()
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(items) and desc:
                descs[index] = desc
        if not descs:
            return None
        lines = [heading] if heading else []
        for index, item in enumerate(items):
            # 条目行和说明都顶格（Telegram 换行后缩进会碎），条目之间空一行分隔
            lines.append(item.strip())
            if index in descs:
                lines.append(descs[index])
            if index + 1 < len(items):
                lines.append("")
        text = "\n".join(lines)
        return f"{text}\n{suffix}" if suffix else text

    new_sections = dict(preserved_sections)
    hn_text = _apply(hn_heading, hn_items, hn_suffix, "hn")
    if hn_text:
        new_sections["hackernews"] = hn_text
    steam_text = _apply(steam_heading, steam_items, steam_suffix, "steam")
    if steam_text:
        new_sections["steam"] = steam_text

    github_text = None
    if github_items:
        gh_descs = {}
        for entry in (data.get("github") or []) if isinstance(data, Mapping) else []:
            if not isinstance(entry, Mapping):
                continue
            index = entry.get("i")
            desc = str(entry.get("desc") or "").strip()
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(github_items) and desc:
                gh_descs[index] = desc
        if gh_descs:
            gh_lines = [github_heading] if github_heading else []
            for index, (title_line, old_desc) in enumerate(github_items):
                gh_lines.append(title_line)
                # AI 人话说明优先；没给的保底用原始描述
                gh_lines.append(gh_descs.get(index) or old_desc)
                if index + 1 < len(github_items):
                    gh_lines.append("")
            github_text = "\n".join(line for line in gh_lines if line is not None)
            new_sections["github"] = github_text

    print(
        "OK enriched snapshot sections "
        f"(steam={'y' if steam_text else 'n'}, hn={'y' if hn_text else 'n'}, github={'y' if github_text else 'n'})"
    )
    return new_sections


def _translate_section_text(key, core_text):
    """单个板块文本的独立翻译调用（不与新闻候选共享 token 预算，避免被挤占截断）。"""
    if not _needs_translation(core_text):
        return None
    try:
        import importlib
        config = importlib.import_module("tg_bot.config")
        from tg_bot.tools.fetch import http_post
        resp = http_post(
            f"{getattr(config, 'DEEPSEEK_API_BASE', 'https://api.deepseek.com')}/chat/completions",
            {
                "model": getattr(config, "DEEPSEEK_MODEL", "deepseek-v4-flash"),
                "messages": [
                    {"role": "system", "content": (
                        "你是翻译器。把用户给出的文本逐行翻译成简体中文：\n"
                        "· 保持原有行数、缩进、• 项目符号、emoji、数字、星标数等格式完全不变\n"
                        "· HTML 标签（如 <b>、</b>）必须原样保留在原来的位置\n"
                        "· 外文的描述性文字翻成自然中文\n"
                        "· 仓库名/项目名（如 owner/repo 格式）、URL、版本号、代码标识符、人名保留原样\n"
                        "· 已经是中文的行原样返回\n"
                        "只输出翻译后的纯文本，不加解释、不加代码块标记。"
                    )},
                    {"role": "user", "content": str(core_text)[:2400]},
                ],
                "max_tokens": 3000,
                "temperature": 0.1,
                "thinking": {"type": "disabled"},
            },
            headers={"Authorization": f"Bearer {config.DEEPSEEK_KEY}"},
            timeout=90,
        )
        out = (resp["choices"][0]["message"].get("content") or "").strip() if resp and resp.get("choices") else ""
        if not out or "<blockquote" in out:
            return None
        # 粗校验：项目符号数量应大致保留，避免模型漏译/漏行
        if core_text.count("•") and out.count("•") < core_text.count("•") * 0.6:
            return None
        return out
    except Exception as exc:
        print(f"WARNING translate_section[{key}]: {type(exc).__name__}", file=sys.stderr)
        return None


def _translate_section_lines_fallback(core_text):
    """整块翻译被内容风控拦截或格式校验失败时的兜底：逐行独立翻译。

    这样只有真正触发风控的那一行会保留原文，其余行不受连累。
    """
    lines = core_text.splitlines()
    texts = {f"l{i}": line for i, line in enumerate(lines) if _needs_translation(line)}
    if not texts:
        return None
    translated = _translate_texts_to_chinese(texts)
    if not translated:
        return None
    new_lines = list(lines)
    changed = False
    for i in range(len(lines)):
        key = f"l{i}"
        if key in translated:
            new_lines[i] = translated[key]
            changed = True
    return "\n".join(new_lines) if changed else None


def localize_snapshot_sections(snapshot_sections):
    """Translate current snapshot text without adding facts or commentary."""
    jobs = {}
    for key in ("github", "proxy", "hackernews"):
        if snapshot_sections.get(key):
            core, suffix = _split_link_block(snapshot_sections[key])
            if core:
                jobs[key] = (core, suffix)
    if not jobs:
        return snapshot_sections
    new_sections = dict(snapshot_sections)
    translated_count = 0
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="report-translate") as pool:
        futures = {key: pool.submit(_translate_section_text, key, core) for key, (core, _s) in jobs.items()}
        for key, future in futures.items():
            new_core = future.result()
            core, suffix = jobs[key]
            if _needs_translation(core) and not new_core:
                new_core = _translate_section_lines_fallback(core)
                if new_core:
                    print(f"OK translated {key} via line-by-line fallback")
            # 翻译后仍被判定"还需要翻译"，多半是仓库名/版本号等专有名词按
            # 要求保留了英文（这是对的，不是翻译失败）——用已翻译内容；
            # 连 fallback 都没产出才退回原文，绝不因为这个把整块板块删掉。
            final_core = new_core or core
            new_sections[key] = final_core + ("\n" + suffix if suffix else "")
            if new_core:
                translated_count += 1
    if translated_count:
        print(f"OK translated {translated_count} section(s) to Chinese")
    return new_sections


def localize_report_inputs(candidates, snapshot_sections):
    """把候选事件标题/摘要翻成中文；板块文本另走独立调用（见 localize_snapshot_sections）。"""
    texts = {}
    for i, cand in enumerate(candidates):
        texts[f"t{i}"] = cand.title
        texts[f"s{i}"] = cand.summary
    translated = _translate_texts_to_chinese(texts)
    new_sections = localize_snapshot_sections(snapshot_sections)
    if not translated:
        return candidates, new_sections
    new_candidates = []
    for i, cand in enumerate(candidates):
        new_title = translated.get(f"t{i}")
        new_summary = translated.get(f"s{i}")
        if new_title or new_summary:
            cand = replace(cand, title=new_title or cand.title, summary=new_summary or cand.summary)
        new_candidates.append(cand)
    print(f"OK translated {len(translated)} pieces to Chinese")
    return new_candidates, new_sections


def _has_provider_failure(diagnostics: Iterable[str]) -> bool:
    """Return true only for failures that make a fresh event report unsafe."""
    return any(
        "provider_error" in str(item) or "unavailable" in str(item)
        for item in diagnostics
    )


def _has_fresh_event(events, now):
    """Mirror the report freshness window to explain cooldown placeholders."""
    for event in events:
        published = [
            ts
            for ts in (
                _parse_timestamp(item.published_at)
                for item in event.sources
                if item.published_at
            )
            if ts is not None
        ]
        if not published:
            return True
        latest = max(published)
        age_hours = (now.astimezone(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds() / 3600
        if age_hours <= 24:
            return True
    return False


def _serialize_event(event):
    return {
        "event_id": event.event_id,
        "category": event.category,
        "title": event.title,
        "summary": event.summary,
        "status": event.status,
        "heat_score": event.heat_score,
        "heat_basis": list(event.heat_basis),
        "sources": [
            {
                "title": item.title,
                "summary": item.summary,
                "url": item.url,
                "domain": item.domain,
                "published_at": item.published_at,
                "relevance": item.relevance,
                "explicit_heat": item.explicit_heat,
                "source": item.source,
            }
            for item in event.sources
        ],
    }


def _parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _prune_state(state, now, retention_days):
    cutoff = now.astimezone(timezone.utc) - timedelta(days=max(1, int(retention_days)))
    kept = {}
    for event_id, record in state.get("events", {}).items():
        if not isinstance(record, Mapping):
            continue
        last = _parse_timestamp(record.get("last_published"))
        if last is not None and last.astimezone(timezone.utc) >= cutoff:
            kept[event_id] = record
    state["events"] = kept


def load_legacy_report(path):
    """Read the previous report for external/legacy section preservation."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except (FileNotFoundError, OSError):
        return ""


def register_builtin_snapshot_collectors(enabled=True):
    """Install lazy native adapters without importing/networking at module load."""
    if not enabled:
        return

    def weather_collector():
        from tg_bot.tools.native import execute_weather
        return execute_weather()

    def github_collector():
        from tg_bot.tools.native import execute_github_trending
        raw = str(execute_github_trending() or "").strip("\n")
        if not raw or "无法获取" in raw.splitlines()[0]:
            return ""
        lines = raw.splitlines()
        # 抓取器自带的首行标题与日报板块标题重复，去掉
        if lines and lines[0].lstrip().startswith("📦"):
            lines = lines[1:]
        return "\n".join(lines).strip()

    def hackernews_collector():
        from tg_bot.tools.native import execute_hn_front_page
        raw = str(execute_hn_front_page(limit=5) or "").strip("\n")
        return "" if "无法获取" in raw else raw

    def proxy_collector():
        from tg_bot.tools.native import execute_proxy_releases
        return str(execute_proxy_releases() or "").strip("\n")

    def steam_collector():
        from tg_bot.tools.native import execute_steam_specials
        from tg_bot.storage import load_novelty_ledger
        # 最近推荐过的游戏名排除掉，日报里每天尽量不重复
        recent_names = {
            item.get("summary", "").strip()
            for item in load_novelty_ledger(kind="steam", limit=60)
            if item.get("summary")
        }
        return str(execute_steam_specials(exclude_names=recent_names) or "").strip("\n")

    def exchange_collector():
        from tg_bot.tools.native import execute_exchange_snapshot
        return str(execute_exchange_snapshot() or "").strip("\n")

    def market_collector():
        from tg_bot.tools.native import execute_us_market_snapshot
        return str(execute_us_market_snapshot() or "").strip("\n")

    def cold_knowledge_collector():
        from tg_bot.storage import load_novelty_ledger
        # 最近讲过的具体知识点塞进 avoid 列表，让 AI 别重复同一个角度
        avoid = [
            item.get("summary", "")
            for item in load_novelty_ledger(kind="cold_knowledge", limit=25)
            if item.get("summary")
        ]
        return generate_cold_knowledge(cold_topic_for(), avoid) or ""

    register_section_collector("weather", weather_collector)
    register_section_collector("github", github_collector)
    register_section_collector("hackernews", hackernews_collector)
    register_section_collector("proxy", proxy_collector)
    register_section_collector("steam", steam_collector)
    register_section_collector("exchange", exchange_collector)
    register_section_collector("market", market_collector)
    register_section_collector("cold_knowledge", cold_knowledge_collector)


def collect_snapshot_sections(section_specs, legacy_report_text, diagnostics=None):
    """Collect only snapshots produced successfully during this run."""
    sections = {}
    diagnostics = diagnostics if diagnostics is not None else []
    for section in section_specs:
        if section.kind != "snapshot":
            continue
        collector = get_section_collector(section.id)
        if collector is None:
            continue
        try:
            raw = str(collector() or "").strip("\n")
        except Exception as exc:
            diagnostics.append(f"{section.id}: collector_error:{type(exc).__name__}")
            continue
        if not raw or raw.startswith("天气查询失败"):
            diagnostics.append(f"{section.id}: collector_empty")
            continue
        from tg_bot.report_sections import section_heading
        sections[section.id] = f"{section_heading(section)}\n{raw}"
    return sections


def _record_selected_events(current_state, selected, now):
    for event in selected:
        previous = current_state["events"].get(event.event_id, {})
        current_state["events"][event.event_id] = {
            "last_published": now.isoformat(),
            "first_seen": previous.get("first_seen", now.isoformat()),
            "category": event.category,
            "title": event.title,
            "summary": event.summary,
            "sources": sorted({item.domain for item in event.sources if item.domain}),
            "heat_score": event.heat_score,
        }


def build_report(
    candidates,
    *,
    now=None,
    state=None,
    per_category=None,
    cooldown_days=None,
    timezone_name="Asia/Shanghai",
    section_specs=None,
    legacy_report_text="",
    preserved_sections=None,
    event_section_target=3,
):
    """Cluster, select, render, and return a serializable report result.

    ``section_specs`` enables the full report registry. Leaving it unset keeps
    the original three-category API and output contract for older callers.
    """
    now = now or datetime.now(timezone.utc)
    default_per_category = 4 if per_category is None else int(per_category)
    default_cooldown_days = 14 if cooldown_days is None else int(cooldown_days)
    resolved_specs = None
    if section_specs is not None:
        resolved_specs = resolve_sections(getattr(item, "id", item) for item in section_specs)
    retention_days = default_cooldown_days
    if resolved_specs:
        retention_days = max(
            [retention_days]
            + [int(section.cooldown_days) for section in resolved_specs if section.kind == "event"],
        )
    current_state = dict(state or {})
    current_state["schema_version"] = 1
    current_state["events"] = dict(current_state.get("events") or {})
    _prune_state(current_state, now, max(retention_days, 14))

    if section_specs is not None:
        specs = resolved_specs
        selected = []
        repeated_sections = set()
        suppressed_sections = set()
        all_events = []
        for section in specs:
            if section.kind != "event":
                continue
            section_candidates = [item for item in candidates if item.category == section.id]
            events = cluster_candidates(section_candidates)
            all_events.extend(events)
            chosen = select_events(
                events,
                current_state,
                now=now,
                per_category=default_per_category if per_category is not None else section.items,
                cooldown_days=default_cooldown_days if cooldown_days is not None else section.cooldown_days,
                strict_category=True,
                require_dated=section.id in _DATED_ONLY_SECTIONS,
            )
            selected.extend(chosen)
            if section_candidates and not chosen:
                suppressed_sections.add(section.id)
                if _has_fresh_event(events, now):
                    repeated_sections.add(section.id)
        eligible_selected = list(selected)
        visible_event_sections = select_visible_event_sections(
            specs, eligible_selected, target=event_section_target,
        )
        visible_event_ids = set(visible_event_sections)
        selected = [
            event for event in eligible_selected
            if event.category in visible_event_ids
        ]
        visible_specs = tuple(
            section for section in specs
            if section.kind != "event" or section.id in visible_event_ids
        )
        repeated_sections.intersection_update(visible_event_ids)
        suppressed_sections.intersection_update(visible_event_ids)
        state_before_publish = dict(current_state)
        state_before_publish["events"] = dict(current_state["events"])
        published_state = dict(state_before_publish)
        published_state["events"] = dict(state_before_publish["events"])
        _record_selected_events(published_state, selected, now)
        preserved = dict(preserved_sections or {})
        report_text = render_daily_report(
            selected,
            now,
            timezone_name=timezone_name,
            section_specs=visible_specs,
            preserved_sections=preserved,
            repeated_sections=repeated_sections,
            suppressed_sections=suppressed_sections,
        )
        return {
            "generated_at": now.isoformat(),
            "selected": selected,
            "eligible_selected": eligible_selected,
            "events": all_events,
            "state": published_state,
            "_state_before_publish": state_before_publish,
            "report_text": report_text,
            "diagnostics": [],
            "sections": [section.id for section in visible_specs],
            "visible_event_sections": list(visible_event_sections),
            "preserved_sections": sorted(preserved),
            "repeated_sections": sorted(repeated_sections),
            "suppressed_sections": sorted(suppressed_sections),
        }

    events = cluster_candidates(candidates)
    selected = select_events(
        events,
        current_state,
        now=now,
        per_category=default_per_category,
        cooldown_days=default_cooldown_days,
    )
    _record_selected_events(current_state, selected, now)
    return {
        "generated_at": now.isoformat(),
        "selected": selected,
        "events": events,
        "state": current_state,
        "report_text": render_daily_report(selected, now, timezone_name=timezone_name),
        "diagnostics": [],
    }


def write_report_files(result, *, report_file, json_file, state_file, status_file=None, save_state=True):
    """Atomically write TXT/JSON/state after a successful collection."""
    from tg_bot.file_io import atomic_write_json, atomic_write_text
    from tg_bot.storage import save_daily_report_state

    payload = {
        "schema_version": 1,
        "generated_at": result["generated_at"],
        "content_sha256": _report_content_hash(result),
        "events": [_serialize_event(event) for event in result["selected"]],
        "sections": list(result.get("sections") or []),
        "preserved_sections": list(result.get("preserved_sections") or []),
        "repeated_sections": list(result.get("repeated_sections") or []),
        "suppressed_sections": list(result.get("suppressed_sections") or []),
        "diagnostics": list(result.get("diagnostics") or []),
    }
    atomic_write_json(json_file, payload)
    atomic_write_text(report_file, result["report_text"])
    if save_state:
        save_daily_report_state(result["state"], state_file)
    if status_file:
        write_report_status(status_file, result)


def _report_content_hash(result):
    """Hash report meaning, excluding the generated timestamp."""
    report_text = str(result.get("report_text") or "")
    report_text = re.sub(
        r"\A(<b>🗞 午间日报</b>\r?\n)"
        r"<code>\d{4}-\d{2}-\d{2} \d{2}:\d{2} · [^<\r\n]+</code>"
        r"(?=\r?\n|$)",
        r"\1<code>[timestamp]</code>",
        report_text,
        count=1,
    )
    canonical = {
        "report_text": report_text,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_report_status(status_file, result, *, status="fresh", extra=None):
    """Write the machine-readable report status without touching report files."""
    from tg_bot.file_io import atomic_write_json

    payload = {
        "schema_version": 1,
        "status": status,
        "generated_at": result["generated_at"],
        "content_sha256": _report_content_hash(result),
        "event_count": len(result["selected"]),
        "sections": list(result.get("sections") or []),
        "repeated_sections": list(result.get("repeated_sections") or []),
        "suppressed_sections": list(result.get("suppressed_sections") or []),
        "diagnostics": list(result.get("diagnostics") or []),
    }
    if extra:
        payload.update(extra)
    atomic_write_json(status_file, payload)


def _load_report_status(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _same_report_day(left, right, timezone_name):
    try:
        zone = ZoneInfo(timezone_name)
        left_dt = _parse_timestamp(left)
        right_dt = _parse_timestamp(right)
        return bool(left_dt and right_dt and left_dt.astimezone(zone).date() == right_dt.astimezone(zone).date())
    except Exception:
        return False


@contextmanager
def _report_push_lock(status_file):
    """Serialize report/status writes and push decisions across invocations."""
    lock_path = f"{status_file}.push.lock"
    try:
        import fcntl
    except ImportError:  # pragma: no cover - deployment target is Unix
        yield
        return
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _publish_latest_markdown(report_text, path=None):
    """Write the report as Markdown for read-only consumers; never fail the run."""
    path = path or os.environ.get("DAILY_REPORT_MARKDOWN_FILE", "")
    if not path:
        return
    try:
        import html as _html
        text = str(report_text or "")
        text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"[\2](\1)", text, flags=re.S)
        text = re.sub(r"</?(?:b|strong)>", "**", text)
        text = re.sub(r"</?code>", "`", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = _html.unescape(text).strip() + "\n"
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception as exc:
        print(f"WARNING latest_markdown_failed: {type(exc).__name__}", file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the report without writing runtime files")
    parser.add_argument("--push", action="store_true", help="send the report to ALLOWED_CHAT when content changes")
    args = parser.parse_args(argv)

    import importlib
    config = importlib.import_module("tg_bot.config")
    load_daily_report_state = importlib.import_module("tg_bot.storage").load_daily_report_state
    status_before_collection = _load_report_status(config.DAILY_REPORT_STATUS_FILE)

    section_specs = resolve_sections(config.DAILY_REPORT_SECTIONS)
    if not config.DAILY_REPORT_SECTIONS_EXPLICIT and config.DAILY_REPORT_CATEGORIES_EXPLICIT:
        legacy_categories = set(config.DAILY_REPORT_CATEGORIES)
        section_specs = tuple(
            section for section in section_specs
            if section.kind == "snapshot" or section.id in legacy_categories
        )
    # 冷却期：只有环境变量显式设置时才全局覆盖，否则尊重各板块自己的值
    # （冷知识 60 天，其余默认 14 天）
    cooldown_explicit = getattr(config, "DAILY_REPORT_EVENT_COOLDOWN_EXPLICIT", True)
    section_specs = tuple(
        replace(
            section,
            items=config.DAILY_REPORT_ITEMS_PER_SECTION,
            cooldown_days=(
                config.DAILY_REPORT_EVENT_COOLDOWN_DAYS
                if cooldown_explicit else section.cooldown_days
            ),
        )
        for section in section_specs
    )
    # 冷知识板块标题带上当天轮盘主题（如「今日冷知识 · 深海生物」）
    section_specs = tuple(
        replace(section, title=f"{section.title} · {cold_topic_for()}")
        if section.id == "cold_knowledge" else section
        for section in section_specs
    )
    event_sections = [section.id for section in section_specs if section.kind == "event"]
    candidates, diagnostics = collect_candidates(event_sections)
    legacy_report_text = load_legacy_report(config.REPORT_FILE)
    register_builtin_snapshot_collectors(config.DAILY_REPORT_NATIVE_SNAPSHOTS)
    snapshot_diagnostics = []
    preserved_sections = collect_snapshot_sections(section_specs, legacy_report_text, snapshot_diagnostics)
    diagnostics.extend(snapshot_diagnostics)
    candidates, preserved_sections = localize_report_inputs(candidates, preserved_sections)
    try:
        preserved_sections = enrich_snapshot_sections(preserved_sections)
    except Exception as exc:
        print(f"WARNING snapshot_enrich: {type(exc).__name__}", file=sys.stderr)
    loaded_state = load_daily_report_state(config.DAILY_REPORT_STATE_FILE)
    result = build_report(
        candidates,
        state=loaded_state,
        timezone_name=config.DAILY_REPORT_TIMEZONE,
        section_specs=section_specs,
        legacy_report_text=legacy_report_text,
        preserved_sections=preserved_sections,
        event_section_target=config.DAILY_REPORT_EVENT_SECTION_TARGET,
    )
    result["diagnostics"] = diagnostics
    # Every published event must survive evidence collection, strict summary
    # parsing, and an independent verifier.  Never fall back to the draft.
    eligible_selected = list(result.get("eligible_selected") or result["selected"])
    try:
        from tg_bot.report_research import ResearchBudget
        from tg_bot.tools.search import execute_news_candidates_all
        from tg_bot.workers.gather_executor import execute_fetch_content

        budget = ResearchBudget(
            followup_queries=config.DAILY_REPORT_FOLLOWUP_QUERIES_PER_EVENT,
            fetches=config.DAILY_REPORT_FETCHES_PER_EVENT,
            min_sources=config.DAILY_REPORT_MIN_INDEPENDENT_SOURCES,
            max_chars=config.DAILY_REPORT_MAX_EVIDENCE_CHARS,
        )

        def _search_more_filtered(query):
            """深挖阶段的补充搜索同样过黑名单，堵住 ntdtv 等站点从研究链回流。"""
            items, provider_diagnostics = execute_news_candidates_all(query)
            kept = [
                item for item in (items or [])
                if not _is_blocked_news_domain(
                    str((item or {}).get("domain") or "")
                    or _url_domain(str((item or {}).get("url") or (item or {}).get("link") or ""))
                )
            ]
            return kept, provider_diagnostics

        working_selected = research_selected_events(
            eligible_selected,
            budget,
            _search_more_filtered,
            execute_fetch_content,
            _summary_call,
            _verify_call,
            evidence_top_k=config.DAILY_REPORT_EVIDENCE_TOP_K,
            summary_retries=config.DAILY_REPORT_SUMMARY_RETRIES,
            verify_retries=config.DAILY_REPORT_VERIFY_RETRIES,
            diagnostics=diagnostics,
        )
    except Exception as exc:
        diagnostics.append(f"research_pipeline_error:{type(exc).__name__}")
        working_selected = []

    visible_event_sections = select_visible_event_sections(
        section_specs, working_selected, target=config.DAILY_REPORT_EVENT_SECTION_TARGET,
    )
    visible_event_ids = set(visible_event_sections)
    working_selected = [
        event for event in working_selected if event.category in visible_event_ids
    ]
    visible_specs = tuple(
        section for section in section_specs
        if section.kind != "event" or section.id in visible_event_ids
    )
    state_before_publish = result.get("_state_before_publish") or loaded_state or {}
    working_state = dict(state_before_publish)
    working_state["schema_version"] = 1
    working_state["events"] = dict(working_state.get("events") or {})
    _record_selected_events(
        working_state, working_selected, _parse_timestamp(result["generated_at"]),
    )
    repeated_sections = set(result.get("repeated_sections") or []) & visible_event_ids
    suppressed_sections = set(result.get("suppressed_sections") or []) & visible_event_ids
    working_report_text = render_daily_report(
        working_selected,
        _parse_timestamp(result["generated_at"]),
        timezone_name=config.DAILY_REPORT_TIMEZONE,
        section_specs=visible_specs,
        preserved_sections=preserved_sections,
        repeated_sections=repeated_sections,
        suppressed_sections=suppressed_sections,
    )
    result.update(
        selected=working_selected,
        state=working_state,
        sections=[section.id for section in visible_specs],
        visible_event_sections=list(visible_event_sections),
        preserved_sections=sorted(preserved_sections),
        repeated_sections=sorted(repeated_sections),
        suppressed_sections=sorted(suppressed_sections),
        diagnostics=diagnostics,
        report_text=working_report_text,
    )
    if args.dry_run:
        print(result["report_text"])
        return 0
    if not str(result.get("report_text") or "").strip():
        status_outcome = "no_publishable_content"
        with _report_push_lock(config.DAILY_REPORT_STATUS_FILE):
            current_status = _load_report_status(config.DAILY_REPORT_STATUS_FILE)
            current_push = current_status.get("push")
            current_push_status = (
                current_push.get("status") if isinstance(current_push, dict) else None
            )
            same_day_sent = (
                current_push_status in {"sent", "skipped_unchanged"}
                and _same_report_day(
                    current_status.get("generated_at"),
                    result.get("generated_at"),
                    config.DAILY_REPORT_TIMEZONE,
                )
            )
            if same_day_sent:
                status_outcome = "same_day_sent_preserved"
            elif current_status == status_before_collection:
                write_report_status(
                    config.DAILY_REPORT_STATUS_FILE,
                    result,
                    status="no_publishable_content",
                )
            else:
                status_outcome = "concurrent_status_preserved"
        for item in diagnostics:
            if item.endswith(":dropped"):
                print(f"INFO {item}")
            else:
                print(f"WARNING {item}")
        if status_outcome == "same_day_sent_preserved":
            print("OK daily report unchanged; previous sent status kept")
        elif status_outcome == "concurrent_status_preserved":
            print("OK daily report has no publishable content; newer status kept")
        else:
            print("OK daily report has no publishable content; previous report kept")
        return 0
    push_requested = bool(args.push or config.DAILY_REPORT_PUSH)
    lock_context = _report_push_lock(config.DAILY_REPORT_STATUS_FILE)
    with lock_context:
        previous_status = _load_report_status(config.DAILY_REPORT_STATUS_FILE)
        content_hash = _report_content_hash(result)
        push_state = {"enabled": push_requested, "status": "disabled"}
        if push_requested:
            previous_push = previous_status.get("push")
            previous_push_status = previous_push.get("status") if isinstance(previous_push, dict) else None
            if previous_status.get("content_sha256") == content_hash and previous_push_status in {"sent", "skipped_unchanged"}:
                push_state["status"] = "skipped_unchanged"
            else:
                push_state["status"] = "pending"
            previous_push_sent = previous_push_status in {"sent", "skipped_unchanged"}
            if (
                previous_push_sent
                and not result.get("selected")
                and _same_report_day(previous_status.get("generated_at"), result.get("generated_at"), config.DAILY_REPORT_TIMEZONE)
            ):
                # A same-day rerun can suppress the already-published events
                # through the cooldown state and render a misleading empty
                # placeholder. Keep the last sent payload byte-for-byte.
                for item in result.get("diagnostics") or []:
                    if item.endswith(":dropped"):
                        print(f"INFO {item}")
                    else:
                        print(f"WARNING {item}")
                from tg_bot.file_io import atomic_write_json
                status_payload = dict(previous_status)
                push_payload = dict(previous_push) if isinstance(previous_push, dict) else {}
                push_payload.update({"enabled": True, "status": "skipped_unchanged"})
                status_payload["push"] = push_payload
                atomic_write_json(config.DAILY_REPORT_STATUS_FILE, status_payload)
                print("OK daily report unchanged; push skipped")
                return 0
        write_report_files(
            result,
            report_file=config.REPORT_FILE,
            json_file=config.DAILY_REPORT_JSON_FILE,
            state_file=config.DAILY_REPORT_STATE_FILE,
            status_file=config.DAILY_REPORT_STATUS_FILE,
            save_state=False,
        )
        _publish_latest_markdown(result["report_text"])
        if push_state["status"] == "pending":
            try:
                from tg_bot.bot_utils import send_report_html
                from tg_bot.daily_report import split_report_chunks
                if os.environ.get("DAILY_REPORT_TELEGRAM", "true").strip().lower() in {"0", "false", "no", "off"}:
                    delivered = True  # Telegram push disabled; report is published to the Markdown file instead
                else:
                    delivered = bool(send_report_html(
                        config.ALLOWED_CHAT,
                        split_report_chunks(result["report_text"]),
                    ))
                push_state["status"] = "sent" if delivered else "failed"
            except Exception as exc:
                push_state["status"] = "failed"
                push_state["error"] = type(exc).__name__
            if push_state["status"] == "sent":
                from tg_bot.storage import save_daily_report_state
                save_daily_report_state(result["state"], config.DAILY_REPORT_STATE_FILE)
                # 日报确实发出去了才记账，避免 --dry-run 或推送失败时污染
                # "最近推荐过"的名单，导致明天错误地排除掉本该出现的游戏。
                try:
                    from tg_bot.storage import append_novelty_entry
                    steam_text = preserved_sections.get("steam") or ""
                    for picked_name in re.findall(r"<b>([^<]+)</b>", steam_text):
                        append_novelty_entry("steam", picked_name.strip())
                except Exception as exc:
                    print(f"WARNING steam_novelty_record: {type(exc).__name__}", file=sys.stderr)
                # 冷知识同理：只有真正推送成功才记账，避免 --dry-run/推送失败
                # 时把没人看到的内容也算进"已经讲过"，导致明天错误地避开它。
                try:
                    from tg_bot.storage import append_novelty_entry
                    cold_text = preserved_sections.get("cold_knowledge") or ""
                    cold_body = cold_text.split("\n", 1)[1].strip() if "\n" in cold_text else ""
                    if cold_body:
                        append_novelty_entry(
                            "cold_knowledge",
                            cold_knowledge_novelty_label(cold_topic_for(), cold_body),
                        )
                except Exception as exc:
                    print(f"WARNING cold_knowledge_novelty_record: {type(exc).__name__}", file=sys.stderr)
        write_report_status(
            config.DAILY_REPORT_STATUS_FILE,
            result,
            extra={"push": push_state},
        )
        if push_state["status"] == "failed":
            print("ERROR daily_report_push_failed", file=sys.stderr)
            return 1
    for item in diagnostics:
        if item.endswith(":dropped"):
            print(f"INFO {item}")
        else:
            print(f"WARNING {item}")
    print(f"OK daily report generated; events={len(result['selected'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
