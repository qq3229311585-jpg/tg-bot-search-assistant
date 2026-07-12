"""Deterministic daily-news candidate clustering, ranking, and rendering."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


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
_CATEGORY_LABELS = {
    "china": "中国要闻",
    "global": "全球要闻",
    "ai_tech": "AI / 技术",
}


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _domain_from_url(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower().split("@")[-1].split(":", 1)[0]
    except Exception:
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
            key = item.domain or _domain_from_url(item.url) or item.url
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
    if len(domains) >= 2:
        return 70.0, "多源交叉"
    return 45.0, "普通来源"


def score_event(event: ReportEvent, now: datetime) -> tuple[float, tuple[str, ...]]:
    freshness, freshness_label = _freshness_score(event, now)
    domains = {item.domain or _domain_from_url(item.url) for item in event.sources if item.domain or item.url}
    coverage = min(5, len(domains)) / 5 * 100
    authority, authority_label = _authority_score(event)
    relevance_values = [item.relevance for item in event.sources if item.relevance > 0]
    relevance = (sum(relevance_values) / len(relevance_values) * 100) if relevance_values else 50.0
    explicit_values = [item.explicit_heat for item in event.sources if item.explicit_heat is not None]
    parts = [(freshness, 0.35), (coverage, 0.25), (authority, 0.15), (relevance, 0.15)]
    basis = [freshness_label, f"{len(domains)} 个独立来源", authority_label]
    if explicit_values:
        explicit = sum(explicit_values) / len(explicit_values)
        parts.append((explicit, 0.10))
        basis.append("显式热度")
        total_weight = 1.0
    else:
        basis.append("多源关注")
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
    official = any(item.domain.endswith((".gov", ".gov.cn")) or item.domain in _AUTHORITY_DOMAINS for item in event.sources)
    return official and (_token_jaccard(old_tokens, new_tokens) < 0.9 or bool(new_sources - old_sources))


def select_events(
    events: Iterable[ReportEvent],
    history: Mapping[str, object] | None,
    *,
    now: datetime | None = None,
    per_category: int = 4,
    cooldown_days: int = 14,
) -> list[ReportEvent]:
    now = now or datetime.now(timezone.utc)
    history_events = (history or {}).get("events", {}) or {}
    scored: list[ReportEvent] = []
    for event in events:
        score, basis = score_event(event, now)
        if score <= 0:
            continue
        previous = history_events.get(event.event_id)
        status = "new"
        if previous:
            last = _parse_datetime(previous.get("last_published"))
            age_days = ((now.astimezone(timezone.utc) - last.astimezone(timezone.utc)).total_seconds() / 86400) if last else 0
            if age_days < cooldown_days:
                if not _is_material_update(event, previous, now):
                    continue
                status = "update"
        scored.append(replace(event, heat_score=score, heat_basis=basis, status=status))

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
            domains = {item.domain for item in event.sources if item.domain}
            if any(domain_counts.get(domain, 0) >= 2 for domain in domains):
                continue
            selected.append(event)
            count += 1
            for domain in domains:
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
    return selected


def render_daily_report(events: Iterable[ReportEvent], generated_at: datetime) -> str:
    rows = list(events)
    stamp = generated_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    lines = [f"📰 今日热点日报 · {stamp}", ""]
    if not rows:
        return "\n".join(lines + ["今日新鲜事件不足，暂不填充旧闻。"])
    current = None
    for index, event in enumerate(rows, 1):
        if event.category != current:
            if current is not None:
                lines.append("")
            current = event.category
            lines.extend((f"【{_CATEGORY_LABELS.get(current, current)}】", ""))
        status = "（更新）" if event.status == "update" else ""
        lines.append(f"{index}. {event.title}{status}")
        lines.append(f"   发生了什么：{event.summary or '来源尚未提供足够摘要。'}")
        lines.append(f"   热度：{event.heat_score:.2f}/100（{'；'.join(event.heat_basis)}）")
        source_lines = []
        for source in event.sources[:3]:
            source_lines.append(f"{source.domain} — {source.url}")
        lines.append(f"   来源：{'；'.join(source_lines)}")
        lines.append("")
    return "\n".join(lines).rstrip()


__all__ = [
    "NewsCandidate", "ReportEvent", "cluster_candidates", "event_fingerprint",
    "normalize_candidate", "render_daily_report", "score_event", "select_events",
]
