"""Deterministic daily-news candidate clustering, ranking, and rendering."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import html
import re
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from tg_bot.report_research import site_key

_TRACKING_KEYS = {"gclid", "fbclid", "ref", "source", "from"}
_STOPWORDS = {
    "a", "an", "and", "are", "for", "from", "in", "into", "is", "of", "on", "or", "the", "to", "with",
    "breaking", "latest", "news", "update", "updates", "report", "reports",
    "报道", "消息", "快讯", "最新", "更新", "官方", "据", "称", "回应", "发布",
}
_AUTHORITY_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.com", "ft.com", "who.int", "un.org",
    "gov.cn", "gov.uk", "whitehouse.gov", "europa.eu", "openai.com",
    "anthropic.com", "deepmind.com", "github.com", "news.ycombinator.com",
}
_OFFICIAL_UPDATE_DOMAINS = {
    "gov.cn", "gov.uk", "whitehouse.gov", "europa.eu", "openai.com",
    "anthropic.com", "deepmind.com", "github.com",
}
_CATEGORY_LABELS = {
    "china": "中国要闻",
    "global": "全球要闻",
    "ai_tech": "AI 速报",
    "security_privacy": "安全与隐私",
    "open_source": "开源与开发工具",
    "science_space": "科学与太空",
    "product_updates": "产品与服务更新",
    "proxy": "代理圈动态",
    "hackernews": "圈子在聊",
    "github": "GitHub 热榜",
    "steam": "Steam 特价推荐",
    "cold_knowledge": "今日冷知识",
}
_CATEGORY_EMOJI = {
    "china": "📰",
    "global": "🌍",
    "ai_tech": "🤖",
    "security_privacy": "🛡",
    "open_source": "🧰",
    "science_space": "🔭",
    "product_updates": "📦",
    "proxy": "🔒",
    "hackernews": "🗣️",
    "github": "🛠",
    "steam": "🎮",
    "cold_knowledge": "🧠",
}
_WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_SECTION_SEPARATOR = "┄" * 14
_REPORT_CHUNK_LIMIT = 4000
_MAX_ESCAPED_HREF_LENGTH = _REPORT_CHUNK_LIMIT // 2
_EVENT_START_RE = re.compile(r"(?m)^\d+\. <b>")
_BLOCKQUOTE_RE = re.compile(
    r"<blockquote(?: expandable)?>.*?</blockquote>", re.DOTALL
)
_HTML_TAG_RE = re.compile(
    r"(<(?:b|code|a|blockquote)(?:\s[^<>]*)?>|</(?:b|code|a|blockquote)>)"
)
_CLOSE_TAG = {
    "b": "</b>",
    "code": "</code>",
    "a": "</a>",
    "blockquote": "</blockquote>",
}


def _h(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _report_heading(stamp: str, weekday: str) -> list[str]:
    return ["<b>🗞 午间日报</b>", f"<code>{_h(f'{stamp} · {weekday}')}</code>", ""]


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _domain_from_url(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower().split("@")[-1].split(":", 1)[0]
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _safe_http_domain(url: str) -> str:
    try:
        raw_authority = re.split(r"[/?#]", url.partition("://")[2], maxsplit=1)[0]
        parts = urlsplit(url)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or not raw_authority.isascii()
            or "\\" in raw_authority
            or parts.username is not None
            or parts.password is not None
            or any(
                char.isspace() or ord(char) < 32 or ord(char) == 127
                for char in raw_authority
            )
        ):
            return ""
        parts.port
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return ""
    labels = host.split(".")
    if len(host) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        return ""
    return host[4:] if host.startswith("www.") else host


def _canonical_url(url: str) -> str:
    url = _clean_text(url)
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if not parts.netloc:
            return url.lower().rstrip("/")
        host = _domain_from_url(url)
        query = [
            (key.lower(), value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
        ]
        path = re.sub(r"/{2,}", "/", parts.path or "/").rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower() or "https", host, path, urlencode(sorted(query)), ""))
    except Exception:
        return url.lower().rstrip("/")


def _stem_token(token: str) -> str:
    if len(token) > 5 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 5 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def _title_tokens(title: str) -> frozenset[str]:
    text = re.sub(r"\b(?:19|20)\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2})?日?\b", " ", title.casefold())
    raw = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text)
    return frozenset(
        token
        for raw_token in raw
        for token in (_stem_token(raw_token),)
        if token and token not in _STOPWORDS and not token.isdigit()
    )


def _token_jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    lhs, rhs = set(left), set(right)
    if not lhs or not rhs:
        return 0.0
    return len(lhs & rhs) / len(lhs | rhs)


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_signal(value: object, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    number = _number(value, -1.0)
    if number < 0:
        return default
    if number <= 1:
        number *= 100
    return max(0.0, min(100.0, number))


@dataclass(frozen=True)
class NewsCandidate:
    category: str
    title: str
    summary: str
    url: str
    domain: str
    published_at: str | None = None
    relevance: float = 0.0
    explicit_heat: float | None = None
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "category", _clean_text(self.category).lower() or "global")
        object.__setattr__(self, "title", _clean_text(self.title))
        object.__setattr__(self, "summary", _clean_text(self.summary))
        object.__setattr__(self, "url", _clean_text(self.url))
        object.__setattr__(self, "domain", _clean_text(self.domain).lower() or _domain_from_url(self.url))
        object.__setattr__(self, "relevance", max(0.0, min(1.0, _number(self.relevance, 0.0) if _number(self.relevance, 0.0) <= 1 else _number(self.relevance) / 100)))
        object.__setattr__(self, "explicit_heat", _normalize_signal(self.explicit_heat))
        object.__setattr__(self, "source", _clean_text(self.source))


@dataclass(frozen=True)
class ReportEvent:
    event_id: str
    category: str
    title: str
    summary: str
    sources: tuple[NewsCandidate, ...]
    heat_score: float = 0.0
    heat_basis: tuple[str, ...] = ()
    status: str = "new"


def normalize_candidate(raw: Mapping[str, object], category: str, source: str) -> NewsCandidate:
    """Convert Brave/Tavily/Serper-like result dictionaries to one model."""
    url = _clean_text(raw.get("url") or raw.get("link"))
    published = raw.get("published_at") or raw.get("published_date") or raw.get("date") or raw.get("published")
    explicit = raw.get("explicit_heat")
    if explicit is None:
        explicit = raw.get("trending_score") or raw.get("heat") or raw.get("traffic_score")
    return NewsCandidate(
        category=category,
        title=_clean_text(raw.get("title") or raw.get("headline") or raw.get("name")),
        summary=_clean_text(raw.get("summary") or raw.get("description") or raw.get("snippet") or raw.get("content")),
        url=url,
        domain=_clean_text(raw.get("domain")) or _domain_from_url(url),
        published_at=_clean_text(published) or None,
        relevance=_number(raw.get("relevance") if raw.get("relevance") is not None else raw.get("score"), 0.0),
        explicit_heat=explicit,
        source=source,
    )


def event_fingerprint(candidate: NewsCandidate) -> str:
    canonical = _canonical_url(candidate.url)
    path = urlsplit(canonical).path if canonical else ""
    title_key = " ".join(sorted(_title_tokens(candidate.title)))
    seed = f"{candidate.category}|{title_key}|{path}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _same_event(left: NewsCandidate, right: NewsCandidate) -> bool:
    if left.category != right.category:
        return False
    left_url, right_url = _canonical_url(left.url), _canonical_url(right.url)
    if left_url and right_url and left_url == right_url:
        return True
    return _token_jaccard(_title_tokens(left.title), _title_tokens(right.title)) >= 0.65


def cluster_candidates(candidates: Iterable[NewsCandidate]) -> list[ReportEvent]:
    groups: list[list[NewsCandidate]] = []
    for candidate in candidates:
        if not isinstance(candidate, NewsCandidate) or not candidate.title:
            continue
        for group in groups:
            if any(_same_event(candidate, item) for item in group):
                group.append(candidate)
                break
        else:
            groups.append([candidate])

    result = []
    for group in groups:
        primary = max(group, key=lambda item: (len(item.summary), len(item.title)))
        unique_sources: list[NewsCandidate] = []
        seen_domains: set[str] = set()
        for item in group:
            key = site_key(item.domain or _domain_from_url(item.url)) or item.url
            if key in seen_domains:
                continue
            seen_domains.add(key)
            unique_sources.append(item)
            if len(unique_sources) >= 5:
                break
        result.append(ReportEvent(
            event_id=event_fingerprint(primary),
            category=primary.category,
            title=primary.title,
            summary=primary.summary,
            sources=tuple(unique_sources),
        ))
    return result


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _latest_published(event: ReportEvent) -> datetime | None:
    values = [_parse_datetime(item.published_at) for item in event.sources]
    values = [value for value in values if value]
    return max(values) if values else None


def _freshness_score(event: ReportEvent, now: datetime) -> tuple[float, str]:
    published = _latest_published(event)
    if not published:
        return 40.0, "时间未知"
    age_hours = max(0.0, (now.astimezone(timezone.utc) - published.astimezone(timezone.utc)).total_seconds() / 3600)
    if age_hours <= 6:
        return 100.0, "新鲜"
    if age_hours <= 24:
        return round(100.0 - (age_hours - 6) * 40.0 / 18.0, 2), "近24小时"
    return 0.0, "超过24小时"


def _authority_score(event: ReportEvent) -> tuple[float, str]:
    domains = {item.domain or _domain_from_url(item.url) for item in event.sources}
    if any(domain.endswith((".gov", ".gov.cn", ".edu")) or domain in _AUTHORITY_DOMAINS for domain in domains):
        return 100.0, "权威来源"
    if len({site_key(domain) for domain in domains}) >= 2:
        return 70.0, "多源交叉"
    return 45.0, "普通来源"


def score_event(event: ReportEvent, now: datetime) -> tuple[float, tuple[str, ...]]:
    freshness, freshness_label = _freshness_score(event, now)
    domains = {item.domain or _domain_from_url(item.url) for item in event.sources if item.domain or item.url}
    sites = {site_key(domain) for domain in domains}
    coverage = min(5, len(sites)) / 5 * 100
    authority, authority_label = _authority_score(event)
    relevance_values = [item.relevance for item in event.sources if item.relevance > 0]
    relevance = (sum(relevance_values) / len(relevance_values) * 100) if relevance_values else 50.0
    explicit_values = [item.explicit_heat for item in event.sources if item.explicit_heat is not None]
    parts = [(freshness, 0.35), (coverage, 0.25), (authority, 0.15), (relevance, 0.15)]
    basis = [freshness_label, f"{len(sites)} 个独立来源", authority_label]
    if explicit_values:
        explicit = sum(explicit_values) / len(explicit_values)
        parts.append((explicit, 0.10))
        basis.append("显式热度")
        total_weight = 1.0
    else:
        basis.append("多源关注" if len(sites) >= 2 else "未提供显式热度")
        total_weight = 0.9
    score = round(sum(value * weight for value, weight in parts) / total_weight, 2)
    return score, tuple(basis)


def _is_material_update(event: ReportEvent, previous: Mapping[str, object], now: datetime) -> bool:
    last = _parse_datetime(previous.get("last_published"))
    if not last or (now.astimezone(timezone.utc) - last.astimezone(timezone.utc)).total_seconds() < 24 * 3600:
        return False
    old_tokens = _title_tokens(str(previous.get("title") or ""))
    new_tokens = _title_tokens(event.title)
    old_sources = {str(item).lower() for item in previous.get("sources", []) or []}
    new_sources = {item.domain for item in event.sources if item.domain}
    if not old_tokens and not old_sources:
        return False
    old_numbers = set(re.findall(r"\d+(?:\.\d+)?", str(previous.get("summary") or "")))
    new_numbers = set(re.findall(r"\d+(?:\.\d+)?", event.summary))
    fact_changed = bool(old_numbers or new_numbers) and old_numbers != new_numbers
    official = any(
        item.domain.endswith((".gov", ".gov.cn")) or item.domain in _OFFICIAL_UPDATE_DOMAINS
        for item in event.sources
    )
    return official and (
        _token_jaccard(old_tokens, new_tokens) < 0.9
        or bool(new_sources - old_sources)
        or fact_changed
    )


def _find_history_match(event: ReportEvent, history_events: Mapping[str, object], *, strict_category: bool = False):
    """Match rewritten headlines to a prior event before applying cooldown."""
    direct = history_events.get(event.event_id)
    if strict_category and direct is not None:
        direct_category = direct.get("category") if isinstance(direct, Mapping) else None
        if str(direct_category or "").lower() != event.category:
            direct = None
    if direct is not None:
        return event.event_id, direct
    best = None
    best_score = 0.0
    current_tokens = _title_tokens(event.title)
    # 冷知识类内容常换标题不换事实，额外用摘要相似度判重
    match_summary = event.category == "cold_knowledge"
    current_summary_tokens = _title_tokens(event.summary) if match_summary else frozenset()
    if not current_tokens and not current_summary_tokens:
        return None, None
    for event_id, previous in history_events.items():
        if not isinstance(previous, Mapping):
            continue
        raw_category = previous.get("category")
        if strict_category and not raw_category:
            continue
        category = str(raw_category or event.category).lower()
        if category != event.category:
            continue
        old_tokens = _title_tokens(str(previous.get("title") or ""))
        similarity = _token_jaccard(current_tokens, old_tokens)
        if match_summary:
            old_summary_tokens = _title_tokens(str(previous.get("summary") or ""))
            summary_similarity = _token_jaccard(current_summary_tokens, old_summary_tokens)
            if summary_similarity >= 0.60:
                similarity = max(similarity, 0.65 + summary_similarity - 0.60)
        if similarity >= 0.65 and similarity > best_score:
            best = (event_id, previous)
            best_score = similarity
    return best if best else (None, None)


def select_events(
    events: Iterable[ReportEvent],
    history: Mapping[str, object] | None,
    *,
    now: datetime | None = None,
    per_category: int = 4,
    cooldown_days: int = 14,
    strict_category: bool = False,
    require_dated: bool = False,
) -> list[ReportEvent]:
    now = now or datetime.now(timezone.utc)
    history_events = (history or {}).get("events", {}) or {}
    scored: list[ReportEvent] = []
    for event in events:
        # 要闻类板块要求有真实发布时间：无日期的常青内容不再给保底分入选
        if require_dated and _latest_published(event) is None:
            continue
        freshness, _freshness_label = _freshness_score(event, now)
        if freshness <= 0:
            continue
        score, basis = score_event(event, now)
        matched_id, previous = _find_history_match(
            event, history_events, strict_category=strict_category
        )
        status = "new"
        if previous:
            last = _parse_datetime(previous.get("last_published"))
            age_days = ((now.astimezone(timezone.utc) - last.astimezone(timezone.utc)).total_seconds() / 86400) if last else None
            if age_days is not None and age_days < cooldown_days:
                if not _is_material_update(event, previous, now):
                    continue
                status = "update"
        event_id = matched_id or event.event_id
        scored.append(replace(event, event_id=event_id, heat_score=score, heat_basis=basis, status=status))

    order = {"china": 0, "global": 1, "ai_tech": 2}
    buckets: dict[str, list[ReportEvent]] = {}
    for event in scored:
        buckets.setdefault(event.category, []).append(event)
    selected: list[ReportEvent] = []
    domain_counts: dict[str, int] = {}
    for category in sorted(buckets, key=lambda value: (order.get(value, 99), value)):
        count = 0
        for event in sorted(buckets[category], key=lambda item: (-item.heat_score, item.event_id)):
            if count >= max(0, int(per_category)):
                break
            domains = {site_key(item.domain) for item in event.sources if item.domain}
            if any(domain_counts.get(domain, 0) >= 2 for domain in domains):
                continue
            selected.append(event)
            count += 1
            for domain in domains:
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
    return selected


def _report_zone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return timezone(timedelta(hours=8))


def select_visible_event_sections(section_specs, events, target=3) -> tuple[str, ...]:
    """Choose populated primaries first, then the hottest reserve lanes."""
    registry = list(section_specs)
    rows = list(events)
    populated = {getattr(event, "category", "") for event in rows}
    limit = min(6, max(1, int(target)))
    primary = [
        section.id for section in registry
        if section.kind == "event" and not section.reserve and section.id in populated
    ]
    reserve_heat = {
        section_id: max(
            (_number(getattr(event, "heat_score", 0)) for event in rows
             if getattr(event, "category", "") == section_id),
            default=0.0,
        )
        for section_id in populated
    }
    reserve = sorted(
        (
            (index, section.id)
            for index, section in enumerate(registry)
            if section.kind == "event" and section.reserve and section.id in populated
        ),
        key=lambda item: (-reserve_heat.get(item[1], 0.0), item[0]),
    )
    return tuple((primary + [section_id for _, section_id in reserve])[:limit])


def render_daily_report(
    events: Iterable[ReportEvent],
    generated_at: datetime,
    timezone_name: str = "Asia/Shanghai",
    *,
    section_specs: Iterable[object] | None = None,
    preserved_sections: Mapping[str, str] | None = None,
    repeated_sections: Iterable[str] | None = None,
    suppressed_sections: Iterable[str] | None = None,
    event_comments: Mapping[str, str] | None = None,
) -> str:
    """Render a deterministic Telegram-safe HTML daily report."""
    rows = list(events)
    local_now = generated_at.astimezone(_report_zone(timezone_name))
    stamp = local_now.strftime("%Y-%m-%d %H:%M")
    weekday = _WEEKDAYS_ZH[local_now.weekday()]

    def _heading(section_id: str, section=None) -> str:
        title = getattr(section, "title", None) or _CATEGORY_LABELS.get(section_id, section_id)
        emoji = getattr(section, "emoji", None) or _CATEGORY_EMOJI.get(section_id, "📌")
        return f"<b>{_h(f'{emoji} {title}')}</b>"

    def _links_block(event: ReportEvent) -> str:
        link_lines: list[str] = []
        seen_domains: set[str] = set()
        for source in event.sources:
            url = str(source.url or "").strip()
            domain = _safe_http_domain(url)
            domain_key = domain.casefold()
            if not url or not domain_key or domain_key in seen_domains:
                continue
            seen_domains.add(domain_key)
            escaped_url = _h(url)
            escaped_domain = _h(domain)
            if len(escaped_url) <= _MAX_ESCAPED_HREF_LENGTH:
                source_text = f'<a href="{escaped_url}">{escaped_domain}</a>'
            else:
                source_text = escaped_domain
            link_lines.append(f"{len(link_lines) + 1}. {source_text}")
            if len(link_lines) == 3:
                break
        if not link_lines:
            return ""
        return "<blockquote expandable><b>来源</b>\n" + "\n".join(link_lines) + "</blockquote>"

    def _event_block(event: ReportEvent, index: int) -> str:
        status = "（更新）" if event.status == "update" else ""
        lines = [f"{index}. <b>{_h(event.title)}</b>{status}"]
        if event.summary:
            lines.append(_h(event.summary))
        links = _links_block(event)
        if links:
            lines.append(links)
        return "\n".join(lines)

    def _event_section(section_id: str, section_events: list[ReportEvent], section=None) -> str:
        blocks = [_event_block(event, index) for index, event in enumerate(section_events, 1)]
        return f"{_heading(section_id, section)}\n\n" + "\n\n".join(blocks)

    def _snapshot_body(section, value: object) -> str:
        lines = str(value or "").strip().splitlines()
        if not lines:
            return ""
        labels = (getattr(section, "title", ""), *getattr(section, "aliases", ()))
        if any(label and label in lines[0] for label in labels):
            lines = lines[1:]
        # 采集器正文自带的 "📍 xxx" 定位行和板块标题重复（如天气板块出现
        # "🌤 天气预报" + "📍 安阳天气" 双标题），渲染时去掉。
        if lines and lines[0].lstrip().startswith("📍"):
            lines = lines[1:]
        # rstrip 而非 strip：避免把第一行的 "  • " 缩进也一并削掉。
        return "\n".join(lines).rstrip()

    def _compose(section_chunks: list[str]) -> str:
        if not section_chunks:
            return ""
        heading = "\n".join(_report_heading(stamp, weekday))
        return heading + "\n" + f"\n{_SECTION_SEPARATOR}\n".join(section_chunks)

    if section_specs is not None:
        specs = tuple(section_specs)
        preserved = dict(preserved_sections or {})
        buckets: dict[str, list[ReportEvent]] = {}
        for event in rows:
            buckets.setdefault(event.category, []).append(event)
        section_chunks: list[str] = []
        for section in specs:
            section_id = getattr(section, "id", "")
            section_kind = getattr(section, "kind", "event")
            matched = buckets.get(section_id, [])
            if matched:
                section_chunks.append(_event_section(section_id, matched, section))
                continue
            if section_kind == "snapshot":
                body = _snapshot_body(section, preserved.get(section_id, ""))
                if body:
                    escaped_body = _h(body)
                    # hackernews/steam 快照会在正文里嵌入真实的 <blockquote> 引用块
                    # （原文链接/商店链接），需要把它从整体转义里解出来。
                    if section_id in ("hackernews", "steam"):
                        escaped_body = escaped_body.replace(
                            "&lt;blockquote expandable&gt;", "<blockquote expandable>",
                        ).replace("&lt;/blockquote&gt;", "</blockquote>")
                    # Steam 游戏名/GitHub 仓库名的加粗来自我们自己的采集器
                    # （Steam 官方 API 名称、正则约束的 owner/repo），放行 <b>；
                    # HN 标题是不可信网页内容，保持全转义。
                    if section_id in ("steam", "github"):
                        escaped_body = escaped_body.replace(
                            "&lt;b&gt;", "<b>",
                        ).replace("&lt;/b&gt;", "</b>")
                    section_chunks.append(f"{_heading(section_id, section)}\n\n{escaped_body}")
        return _compose(section_chunks)

    section_order: list[str] = []
    buckets: dict[str, list[ReportEvent]] = {}
    for event in rows:
        if event.category not in buckets:
            section_order.append(event.category)
            buckets[event.category] = []
        buckets[event.category].append(event)
    return _compose([
        _event_section(section_id, buckets[section_id])
        for section_id in section_order
    ])


def _safe_text_cut(text: str, available: int) -> int:
    if len(text) <= available:
        return len(text)
    cut = available
    newline = text.rfind("\n", 0, available + 1)
    space = text.rfind(" ", 0, available + 1)
    boundary = max(newline, space)
    if boundary >= available // 2:
        cut = boundary + 1
    amp = text.rfind("&", 0, cut)
    semicolon = text.rfind(";", 0, cut)
    if amp > semicolon and amp > 0:
        cut = amp
    return max(1, cut)


def _split_html_core(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    stack: list[tuple[str, str]] = []
    suppressed_closings: list[str] = []

    def closing_tags() -> str:
        return "".join(_CLOSE_TAG[name] for name, _tag in reversed(stack))

    def reopened_tags() -> str:
        return "".join(tag for _name, tag in stack)

    def flush() -> None:
        nonlocal current
        chunk = (current + closing_tags()).strip()
        if chunk:
            chunks.append(chunk)
        current = reopened_tags()

    for token in _HTML_TAG_RE.split(text):
        if not token:
            continue
        if token.startswith("</"):
            name = token[2:-1]
            if suppressed_closings and suppressed_closings[-1] == name:
                suppressed_closings.pop()
                continue
            current += token
            if stack and stack[-1][0] == name:
                stack.pop()
            continue
        if token.startswith("<"):
            match = re.match(r"<(b|code|a|blockquote)(?:\s|>)", token)
            if match:
                name = match.group(1)
                required = len(current) + len(token) + len(closing_tags()) + len(_CLOSE_TAG[name])
                if required > limit and current.strip():
                    flush()
                required = len(current) + len(token) + len(closing_tags()) + len(_CLOSE_TAG[name])
                if required > limit:
                    suppressed_closings.append(name)
                    continue
                current += token
                stack.append((name, token))
                continue
        remaining = token
        while remaining:
            available = limit - len(current) - len(closing_tags())
            if available <= 0:
                flush()
                available = limit - len(current) - len(closing_tags())
            cut = _safe_text_cut(remaining, available)
            current += remaining[:cut]
            remaining = remaining[cut:]
            if remaining:
                flush()
    flush()
    return chunks


def _append_html_piece(chunks: list[str], piece: str, limit: int) -> None:
    piece = piece.strip()
    if not piece:
        return
    if chunks and len(chunks[-1]) + 2 + len(piece) <= limit:
        chunks[-1] += "\n\n" + piece
    else:
        chunks.append(piece)


def _split_html_safe(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    cursor = 0
    for match in _BLOCKQUOTE_RE.finditer(text):
        for piece in _split_html_core(text[cursor:match.start()], limit):
            _append_html_piece(chunks, piece, limit)
        blockquote = match.group(0)
        pieces = [blockquote] if len(blockquote) <= limit else _split_html_core(blockquote, limit)
        for piece in pieces:
            _append_html_piece(chunks, piece, limit)
        cursor = match.end()
    for piece in _split_html_core(text[cursor:], limit):
        _append_html_piece(chunks, piece, limit)
    return chunks


def _split_oversized_section(text: str, limit: int) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    starts = [match.start() for match in _EVENT_START_RE.finditer(text)]
    if not starts:
        return _split_html_safe(text, limit)
    prefix = text[:starts[0]].strip()
    event_blocks = [
        text[start:end].strip()
        for start, end in zip(starts, starts[1:] + [len(text)])
    ]
    chunks: list[str] = []
    current = prefix
    for block in event_blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.extend(
                [current] if len(current) <= limit else _split_html_safe(current, limit)
            )
        if len(block) <= limit:
            current = block
        else:
            chunks.extend(_split_html_safe(block, limit))
            current = ""
    if current:
        chunks.extend([current] if len(current) <= limit else _split_html_safe(current, limit))
    return chunks


def split_report_chunks(report_text: str) -> list[str]:
    """Split safe HTML by section, event, then balanced text boundaries."""
    text = str(report_text or "").strip()
    if not text:
        return []
    section_chunks: list[str] = []
    current: list[str] = []
    blockquote_depth = 0
    for line in text.splitlines(keepends=True):
        if line.rstrip("\r\n") == _SECTION_SEPARATOR and blockquote_depth == 0:
            chunk = "".join(current).strip()
            if chunk:
                section_chunks.append(chunk)
            current = []
            continue
        current.append(line)
        blockquote_depth += len(re.findall(r"<blockquote(?: expandable)?>", line))
        blockquote_depth -= line.count("</blockquote>")
        blockquote_depth = max(0, blockquote_depth)
    chunk = "".join(current).strip()
    if chunk:
        section_chunks.append(chunk)
    return [
        piece
        for section in section_chunks
        for piece in _split_oversized_section(section, _REPORT_CHUNK_LIMIT)
    ]


__all__ = [
    "NewsCandidate", "ReportEvent", "cluster_candidates", "event_fingerprint",
    "normalize_candidate", "render_daily_report", "score_event", "select_events",
    "split_report_chunks",
]
