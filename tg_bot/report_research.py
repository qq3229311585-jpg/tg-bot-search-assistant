"""Bounded, deterministic full-text evidence collection for report events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_KEYS = {
    "dclid", "fbclid", "gbraid", "gclid", "mc_cid", "mc_eid",
    "msclkid", "ttclid", "twclid", "wbraid", "yclid",
}
_FETCH_FAILURE_PREFIX = "正文抓取失败"
_CONTENT_SOURCE_HEADER = re.compile(r"^\[正文来源[：:]\s*[^\]]+\]\s*", re.IGNORECASE)
# 官方/权威域名：允许单一独立信源通过核查，不强制凑够两个独立域名。
_AUTHORITY_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.com", "ft.com", "who.int", "un.org",
    "gov.cn", "gov.uk", "whitehouse.gov", "europa.eu", "openai.com",
    "anthropic.com", "deepmind.com", "github.com", "news.ycombinator.com",
}


def _is_authority_domain(domain: str) -> bool:
    domain = (domain or "").lower()
    if not domain:
        return False
    return (
        domain.endswith((".gov", ".gov.cn", ".edu"))
        or domain in _AUTHORITY_DOMAINS
        or any(domain.endswith(f".{suffix}") for suffix in _AUTHORITY_DOMAINS)
    )


_SITE_PREFIXES = ("www.", "m.", "k.", "mobile.", "wap.", "3g.")
_MULTI_PART_SUFFIXES = (
    ".com.cn", ".net.cn", ".org.cn", ".gov.cn", ".com.tw", ".com.hk",
    ".com.sg", ".co.uk", ".co.jp",
)
_SITE_ALIASES = {"sina.cn": "sina.com.cn", "sina.com": "sina.com.cn"}


def site_key(domain: str) -> str:
    """Collapse subdomains and aliases of one website to a single site key."""
    host = (domain or "").strip().lower().split(":", 1)[0]
    if not host:
        return ""
    stripped = True
    while stripped:
        stripped = False
        for prefix in _SITE_PREFIXES:
            if host.startswith(prefix) and len(host) > len(prefix):
                host = host[len(prefix):]
                stripped = True
    parts = host.split(".")
    if host.endswith(_MULTI_PART_SUFFIXES):
        main = ".".join(parts[-3:])
    else:
        main = ".".join(parts[-2:])
    return _SITE_ALIASES.get(main, main)


_TRADITIONAL_ONLY_CHARS = frozenset(
    "擬級對軍動慮奪島據員傾擴選項襲轟鎬隧設終決變開討視壓談輸願"
    "發後裡這個為與從時會來還過應將於國門間實體現點數處學業產萬億"
)


def _contains_traditional_chinese(value: str) -> bool:
    return any(char in _TRADITIONAL_ONLY_CHARS for char in value)


@dataclass(frozen=True)
class ResearchBudget:
    followup_queries: int = 2
    fetches: int = 3
    min_sources: int = 2
    max_chars: int = 18000

    def __post_init__(self) -> None:
        values = (
            ("followup_queries", self.followup_queries, 1, 3),
            ("fetches", self.fetches, 1, 5),
            ("min_sources", self.min_sources, 1, 3),
            ("max_chars", self.max_chars, 4000, 40000),
        )
        for name, value, minimum, maximum in values:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if self.min_sources > self.fetches:
            raise ValueError("min_sources must not exceed fetches")


@dataclass(frozen=True)
class EvidenceSource:
    source_id: str
    provider: str
    query: str
    title: str
    url: str
    domain: str
    published_at: str | None
    snippet: str
    full_content: str
    fetch_status: str


@dataclass(frozen=True)
class EvidenceBundle:
    event_id: str
    category: str
    seed_title: str
    sources: tuple[EvidenceSource, ...]


@dataclass(frozen=True)
class VerifiedSummary:
    event_id: str
    title: str
    summary: str
    supporting_source_ids: tuple[str, ...]
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class DroppedSummary:
    event_id: str


@dataclass(frozen=True)
class _Candidate:
    provider: str
    query: str
    title: str
    url: str
    domain: str
    published_at: str | None
    snippet: str


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _followup_queries(event: object, limit: int) -> list[str]:
    title = _text(getattr(event, "title", ""))
    summary = _text(getattr(event, "summary", ""))
    subject_fact = _text(f"{title} {summary[:240]} official 官方")
    recent = _text(f"{title} latest update past 24 hours 近24小时 更新")
    candidates = (title, subject_fact, recent)
    queries: list[str] = []
    seen: set[str] = set()
    for query in candidates:
        key = query.casefold()
        if query and key not in seen:
            queries.append(query)
            seen.add(key)
        if len(queries) >= limit:
            break
    return queries


def _canonical_http_url(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        return "", ""
    raw = value.strip()
    if not raw or "\\" in raw or any(ord(char) < 32 or char.isspace() for char in raw):
        return "", ""
    try:
        parts = urlsplit(raw)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or not parts.netloc.isascii()
            or not parts.hostname.isascii()
            or parts.username is not None
            or parts.password is not None
        ):
            return "", ""
        port = parts.port
        host = parts.hostname.rstrip(".").lower()
    except ValueError:
        return "", ""
    if not host or len(host) > 253:
        return "", ""
    domain = host[4:] if host.startswith("www.") else host
    if any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in host.split(".")
    ):
        return "", ""
    netloc = host
    if port is not None and not (
        parts.scheme.lower() == "http" and port == 80
        or parts.scheme.lower() == "https" and port == 443
    ):
        netloc = f"{host}:{port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_KEYS
    ]
    path = parts.path or "/"
    canonical = urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))
    return canonical, domain


def _candidate_from(value: object, *, query: str, seed: bool = False) -> _Candidate | None:
    if seed:
        raw_url = getattr(value, "url", "")
        provider = _text(getattr(value, "source", ""))
        title = _text(getattr(value, "title", ""))
        snippet = _text(getattr(value, "summary", ""))
        published = getattr(value, "published_at", None)
    elif isinstance(value, Mapping):
        raw_url = value.get("url") or value.get("link")
        provider = _text(value.get("source") or value.get("provider"))
        title = _text(value.get("title") or value.get("headline") or value.get("name"))
        snippet = _text(
            value.get("summary") or value.get("description")
            or value.get("snippet") or value.get("content")
        )
        published = (
            value.get("published_at") or value.get("published_date")
            or value.get("date") or value.get("published")
        )
    else:
        return None
    url, domain = _canonical_http_url(raw_url)
    if not url:
        return None
    return _Candidate(
        provider=provider,
        query=query,
        title=title,
        url=url,
        domain=domain,
        published_at=_text(published) or None,
        snippet=snippet,
    )


def _clean_content(value: object) -> str:
    if not isinstance(value, str):
        return ""
    content = value.strip()
    if not content or content.startswith(_FETCH_FAILURE_PREFIX):
        return ""
    return _CONTENT_SOURCE_HEADER.sub("", content, count=1).strip()


def _fair_parts(values: list[str], budget: int) -> list[str]:
    if not values or budget <= 0:
        return ["" for _ in values]
    if sum(map(len, values)) <= budget:
        return values
    share, remainder = divmod(budget, len(values))
    return [value[:share + (index < remainder)] for index, value in enumerate(values)]


def _fit_evidence(sources: list[EvidenceSource], max_chars: int) -> list[EvidenceSource]:
    total = sum(len(source.snippet) + len(source.full_content) for source in sources)
    if total <= max_chars:
        return sources
    snippets = [source.snippet for source in sources]
    snippet_budget = min(sum(map(len, snippets)), max_chars // 3)
    fitted_snippets = _fair_parts(snippets, snippet_budget)
    used = sum(map(len, fitted_snippets))
    fitted_content = _fair_parts(
        [source.full_content for source in sources], max_chars - used
    )
    return [
        replace(source, snippet=fitted_snippets[index], full_content=fitted_content[index])
        for index, source in enumerate(sources)
    ]


def deepen_event(
    event: object,
    search_more: Callable[[str], object],
    fetch: Callable[[str], object],
    budget: ResearchBudget,
) -> EvidenceBundle | None:
    """Collect usable evidence from independent domains within strict call budgets."""
    if not isinstance(budget, ResearchBudget):
        raise TypeError("budget must be ResearchBudget")

    candidates: list[_Candidate] = []
    for source in tuple(getattr(event, "sources", ()) or ()):
        candidate = _candidate_from(source, query="", seed=True)
        if candidate is not None:
            candidates.append(candidate)

    for query in _followup_queries(event, budget.followup_queries):
        try:
            result = search_more(query)
            if not isinstance(result, tuple) or len(result) != 2:
                continue
            items, _diagnostics = result
            if not isinstance(items, (list, tuple)):
                continue
        except Exception:
            continue
        for item in items:
            try:
                candidate = _candidate_from(item, query=query)
            except Exception:
                candidate = None
            if candidate is not None:
                candidates.append(candidate)

    distinct: list[_Candidate] = []
    seen_urls: set[str] = set()
    seen_domains: set[str] = set()
    for candidate in candidates:
        if candidate.url in seen_urls or site_key(candidate.domain) in seen_domains:
            continue
        seen_urls.add(candidate.url)
        seen_domains.add(site_key(candidate.domain))
        distinct.append(candidate)

    usable: list[EvidenceSource] = []
    fetch_calls = 0
    for candidate in distinct:
        if fetch_calls >= budget.fetches:
            break
        fetch_calls += 1
        try:
            content = _clean_content(fetch(candidate.url))
        except Exception:
            continue
        if not content:
            continue
        usable.append(EvidenceSource(
            source_id=f"S{len(usable) + 1}",
            provider=candidate.provider,
            query=candidate.query,
            title=candidate.title,
            url=candidate.url,
            domain=candidate.domain,
            published_at=candidate.published_at,
            snippet=candidate.snippet,
            full_content=content,
            fetch_status="ok",
        ))

    if len(usable) < budget.min_sources:
        return None
    fitted = _fit_evidence(usable, budget.max_chars)
    return EvidenceBundle(
        event_id=_text(getattr(event, "event_id", "")),
        category=_text(getattr(event, "category", "")),
        seed_title=_text(getattr(event, "title", "")),
        sources=tuple(fitted),
    )


def _source_payload(source: EvidenceSource) -> dict[str, object]:
    return {
        "source_id": source.source_id,
        "provider": source.provider,
        "query": source.query,
        "title": source.title,
        "url": source.url,
        "domain": source.domain,
        "published_at": source.published_at,
        "snippet": source.snippet,
        "full_content": source.full_content,
        "fetch_status": source.fetch_status,
    }


def summary_payload(bundle: EvidenceBundle) -> dict[str, object]:
    """Return all collected evidence fields and no editorial/opinion channel."""
    return {
        "id": bundle.event_id,
        "category": bundle.category,
        "seed_title": bundle.seed_title,
        "sources": [_source_payload(source) for source in bundle.sources],
    }


def parse_summary(bundle: EvidenceBundle, raw: object) -> VerifiedSummary | DroppedSummary | None:
    """Parse the exact summary JSON contract, rejecting every ambiguity."""
    allowed = {
        "id", "title", "summary", "supporting_source_ids", "conflicts", "drop",
    }
    if not isinstance(raw, Mapping) or set(raw) != allowed:
        return None
    if raw.get("id") != bundle.event_id:
        return None
    drop = raw.get("drop")
    if not isinstance(drop, bool):
        return None
    if drop is True:
        drop_title = raw.get("title")
        drop_summary = raw.get("summary")
        if not isinstance(drop_title, str) or not drop_title.strip():
            return None
        if not isinstance(drop_summary, str) or not drop_summary.strip():
            return None
        return DroppedSummary(bundle.event_id)
    title = raw.get("title")
    summary = raw.get("summary")
    source_ids = raw.get("supporting_source_ids")
    conflicts = raw.get("conflicts")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(summary, str) or not summary.strip():
        return None
    title = title.strip()
    summary = summary.strip()
    if (
        len(title) > 24
        or not re.search(r"[\u3400-\u9fff]", title)
        or _contains_traditional_chinese(title)
    ):
        return None
    if (
        not re.search(r"[\u3400-\u9fff]", summary)
        or _contains_traditional_chinese(summary)
    ):
        return None
    if (
        not isinstance(source_ids, list)
        or len(source_ids) < 1
        or any(not isinstance(item, str) or not item for item in source_ids)
        or len(source_ids) != len(set(source_ids))
    ):
        return None
    if not isinstance(conflicts, list) or any(not isinstance(item, str) for item in conflicts):
        return None
    sources_by_id = {source.source_id: source for source in bundle.sources}
    if any(source_id not in sources_by_id for source_id in source_ids):
        return None
    cited_domains = {sources_by_id[source_id].domain for source_id in source_ids}
    cited_sites = {site_key(domain) for domain in cited_domains}
    # 至少两个独立站点互证（同站不同域名不算）；单一信源时，只有权威/官方域名才放行。
    if len(cited_sites) < 2 and not any(_is_authority_domain(domain) for domain in cited_domains):
        return None
    if conflicts and not re.search(r"分歧|不一致|冲突|来源说法不同|不同", summary):
        return None
    return VerifiedSummary(
        event_id=bundle.event_id,
        title=title,
        summary=summary,
        supporting_source_ids=tuple(source_ids),
        conflicts=tuple(conflicts),
    )


def _retry_count(retries: object) -> int:
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 1:
        raise ValueError("retries must be a positive integer")
    return retries


def summarize_bundle(
    bundle: EvidenceBundle,
    call: Callable[[dict[str, object]], object],
    retries: int = 2,
) -> VerifiedSummary | DroppedSummary | None:
    """Call the summarizer at most ``retries`` total times and parse strictly."""
    attempts = _retry_count(retries)
    payload = summary_payload(bundle)
    for _attempt in range(attempts):
        try:
            parsed = parse_summary(bundle, call(payload))
        except Exception:
            parsed = None
        if parsed is not None:
            return parsed
    return None


def verification_payload(
    bundle: EvidenceBundle, summary: VerifiedSummary
) -> dict[str, object]:
    """Return the proposed summary plus only the evidence it cites."""
    cited = set(summary.supporting_source_ids)
    return {
        "summary": {
            "id": summary.event_id,
            "title": summary.title,
            "summary": summary.summary,
            "supporting_source_ids": list(summary.supporting_source_ids),
            "conflicts": list(summary.conflicts),
        },
        "sources": [
            _source_payload(source) for source in bundle.sources
            if source.source_id in cited
        ],
    }


def verify_summary(
    bundle: EvidenceBundle,
    summary: VerifiedSummary,
    call: Callable[[dict[str, object]], object],
    retries: int = 1,
) -> bool:
    """Accept only an exact, well-formed pass verdict; otherwise fail closed."""
    attempts = _retry_count(retries)
    payload = verification_payload(bundle, summary)
    legal_ids = set(summary.supporting_source_ids)
    allowed = {"verdict", "unsupported_claims", "supporting_source_ids"}
    for _attempt in range(attempts):
        try:
            raw = call(payload)
        except Exception:
            continue
        if not isinstance(raw, Mapping) or set(raw) != allowed:
            continue
        unsupported = raw.get("unsupported_claims")
        source_ids = raw.get("supporting_source_ids")
        if (
            raw.get("verdict") == "pass"
            and isinstance(unsupported, list)
            and not unsupported
            and isinstance(source_ids, list)
            and len(source_ids) >= 1
            and len(source_ids) == len(set(source_ids))
            and set(source_ids) == legal_ids
        ):
            return True
    return False
