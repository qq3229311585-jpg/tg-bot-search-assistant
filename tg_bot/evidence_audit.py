"""Pure contracts and hard gates for ordinary-chat evidence coverage.

This module deliberately has no model or network dependency.  It validates the
semantic audit returned by the later adapter against facts the program can prove
from the collected source dictionaries.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Mapping, Sequence
import re
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from tg_bot.direct_api import DIRECT_API_TOOLS, direct_api_failure_reason


_STATUS = {"supported", "partial", "conflicting", "missing"}
_REASON_CODES = {
    "direct_api_complete", "coverage_complete", "missing_requirements",
    "conflicting_evidence", "no_full_text", "no_novelty", "quota_exhausted",
    "invalid_audit",
}
_INSUFFICIENT_REASON_CODES = {
    "missing_requirements", "conflicting_evidence", "no_full_text",
    "no_novelty", "quota_exhausted",
}
_PRESENTATION_PREFIXES = ("www.", "m.", "mobile.", "amp.")
# Reuse Gather's narrow direct-result set.  In particular, a mutating tool must
# not become a complete evidence source merely because it returned text.
_DIRECT_API_TOOLS = DIRECT_API_TOOLS
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


@dataclass(frozen=True)
class EvidenceRequirement:
    id: str
    question: str
    required: bool
    current_or_disputed: bool


@dataclass(frozen=True)
class RequirementCoverage:
    requirement_id: str
    status: Literal["supported", "partial", "conflicting", "missing"]
    source_ids: tuple[str, ...]
    independent_domains: tuple[str, ...]
    has_full_text: bool
    gap_query: str


@dataclass(frozen=True)
class EvidenceAuditResult:
    sufficient: bool
    requirements: tuple[EvidenceRequirement, ...]
    coverage: tuple[RequirementCoverage, ...]
    conflicts: tuple[str, ...]
    reason_code: str


@dataclass(frozen=True)
class EvidenceStats:
    source_count: int
    canonical_urls: int
    independent_domains: int
    full_text_sources: int
    content_clusters: int = 0


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalize_source_domain(source_or_url: object) -> str:
    """Return a canonical host for a source, stripping display-only prefixes."""
    if isinstance(source_or_url, Mapping):
        url = _as_text(source_or_url.get("url"))
        if url.startswith("direct://"):
            return ""
        parsed = _safe_urlsplit(url)
        candidate = parsed.hostname if parsed else _as_text(source_or_url.get("domain"))
    else:
        candidate = _as_text(source_or_url)
    if not candidate or candidate.startswith("direct://"):
        return ""
    parsed = _safe_urlsplit(candidate if "://" in candidate else f"//{candidate}")
    if not parsed:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    while any(host.startswith(prefix) for prefix in _PRESENTATION_PREFIXES):
        for prefix in _PRESENTATION_PREFIXES:
            if host.startswith(prefix):
                host = host[len(prefix):]
                break
    return host


def _safe_urlsplit(value: str) -> SplitResult | None:
    try:
        return urlsplit(value)
    except ValueError:
        return None


def _canonical_url(source: Mapping) -> str:
    raw = _as_text(source.get("url"))
    if not raw:
        return ""
    if raw.startswith("direct://"):
        return raw.lower()
    parsed = _safe_urlsplit(raw)
    if not parsed or parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = normalize_source_domain(parsed.hostname)
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port and not ((parsed.scheme.lower() == "https" and port == 443) or (parsed.scheme.lower() == "http" and port == 80)):
        host = f"{host}:{port}"
    query = urlencode(sorted([
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]))
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", query, ""))


def _has_full_text(source: Mapping) -> bool:
    # Gather backfills search snippets into ``full_content`` for compatibility;
    # those are not a successfully fetched page body.
    full_content = _as_text(source.get("full_content"))
    if not full_content or source.get("error"):
        return False
    if _as_text(source.get("tool")) not in {"web_search", "serper_search"}:
        return True
    return full_content != _as_text(source.get("snippet"))


def _usable_source(source: object) -> bool:
    if not isinstance(source, Mapping) or not _as_text(source.get("id")) or source.get("error"):
        return False
    url = _as_text(source.get("url"))
    if url.startswith("direct://"):
        safe_url = True
    else:
        parsed = _safe_urlsplit(url)
        safe_url = bool(parsed and parsed.scheme.lower() in {"http", "https"} and parsed.hostname)
    return safe_url and bool(_as_text(source.get("snippet")) or _as_text(source.get("full_content")))


def _normalized_content(source: Mapping) -> str:
    """Stable, non-semantic fingerprint input for syndicated-content detection.

    A domain is not an independent confirmation when it republishes the same
    material.  Very short fragments deliberately produce no fingerprint: they
    are too generic to safely merge unrelated reports.
    """
    full_text = _as_text(source.get("full_content"))
    # A fetched body is the best mirror signal; differing headlines should not
    # disguise a verbatim syndicated article.
    text = full_text if len(full_text) >= 120 else " ".join(
        _as_text(source.get(key)) for key in ("title", "snippet")
    )
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text.lower())
    return text[:8000] if len(text) >= 120 else ""


def _content_similar(left: Mapping, right: Mapping) -> bool:
    a, b = _normalized_content(left), _normalized_content(right)
    if not a or not b:
        return False
    if a == b:
        return True
    width = 3
    a_grams = {a[index:index + width] for index in range(len(a) - width + 1)}
    b_grams = {b[index:index + width] for index in range(len(b) - width + 1)}
    if not a_grams or not b_grams:
        return False
    # Both guards are symmetric.  The size ratio blocks a composite/roundup
    # page (A+C) from bridging two otherwise unrelated articles.
    if min(len(a), len(b)) / max(len(a), len(b)) < 0.80:
        return False
    return len(a_grams & b_grams) / max(len(a_grams), len(b_grams)) >= 0.82


def _content_cluster_count(sources: Sequence[Mapping]) -> int:
    """Count pairwise-similarity components independent of input ordering."""
    parent = list(range(len(sources)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for left in range(len(sources)):
        for right in range(left + 1, len(sources)):
            if _content_similar(sources[left], sources[right]):
                union(left, right)
    return len({find(index) for index in range(len(sources))})


def evidence_stats(sources: Sequence[Mapping]) -> EvidenceStats:
    usable = [source for source in sources if _usable_source(source)]
    urls = {_canonical_url(source) for source in usable} - {""}
    domains = {normalize_source_domain(source) for source in usable} - {""}
    clusters = _content_cluster_count(usable)
    # Mirrored full articles on separate hosts do not become independent
    # corroboration.  Keep short fragments separate rather than guessing.
    independent_domains = len(domains) if clusters >= len(usable) else min(len(domains), clusters)
    return EvidenceStats(len(usable), len(urls), independent_domains,
                         sum(_has_full_text(source) for source in usable), clusters)


def _invalid_result(result: EvidenceAuditResult) -> EvidenceAuditResult:
    return replace(result, sufficient=False, reason_code="invalid_audit")


def parse_audit_payload(
    payload: object,
    *,
    prior_requirements: tuple[EvidenceRequirement, ...] = (),
    max_requirements: int = 5,
) -> EvidenceAuditResult:
    """Parse model JSON into immutable contracts; semantic proof happens later."""
    invalid = EvidenceAuditResult(False, (), (), (), "invalid_audit")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("sufficient"), bool):
        return invalid
    raw_requirements = payload.get("requirements")
    raw_coverage = payload.get("coverage")
    raw_conflicts = payload.get("conflicts", ())
    if not isinstance(raw_requirements, list) or not isinstance(raw_coverage, list) or not isinstance(raw_conflicts, list):
        return invalid
    requirements: list[EvidenceRequirement] = []
    for item in raw_requirements:
        if not isinstance(item, Mapping):
            return invalid
        identifier, question = _as_text(item.get("id")), _as_text(item.get("question"))
        if not identifier or not question or not isinstance(item.get("required"), bool) or not isinstance(item.get("current_or_disputed"), bool):
            return invalid
        requirements.append(EvidenceRequirement(identifier, question, item["required"], item["current_or_disputed"]))
    expected_ids = tuple(f"R{index}" for index in range(1, len(requirements) + 1))
    if (not requirements or len(requirements) > max_requirements
            or tuple(item.id for item in requirements) != expected_ids):
        return invalid
    if prior_requirements and tuple((item.id, item.question) for item in requirements) != tuple(
        (item.id, item.question) for item in prior_requirements
    ):
        return invalid
    coverage: list[RequirementCoverage] = []
    for item in raw_coverage:
        if not isinstance(item, Mapping):
            return invalid
        requirement_id, status = _as_text(item.get("requirement_id")), _as_text(item.get("status"))
        ids = item.get("source_ids", ())
        if not requirement_id or status not in _STATUS or not isinstance(ids, list) or not all(isinstance(x, str) and x.strip() for x in ids):
            return invalid
        if "independent_domains" not in item:
            return invalid
        domains = item["independent_domains"]
        if not isinstance(domains, list) or not all(isinstance(x, str) for x in domains):
            return invalid
        if "has_full_text" not in item or not isinstance(item["has_full_text"], bool):
            return invalid
        has_full_text = item["has_full_text"]
        coverage.append(RequirementCoverage(
            requirement_id, status, tuple(ids), tuple(domains), has_full_text, _as_text(item.get("gap_query")),
        ))
    reason_code = _as_text(payload.get("reason_code"))
    if reason_code not in _REASON_CODES:
        return invalid
    return EvidenceAuditResult(
        payload["sufficient"], tuple(requirements), tuple(coverage),
        tuple(_as_text(item) for item in raw_conflicts if _as_text(item)), reason_code,
    )


def direct_api_complete(sources: Sequence[Mapping]) -> bool:
    """A single successful direct API response is a complete simple-tool result."""
    usable = [source for source in sources if _usable_source(source)]
    if len(usable) != 1:
        return False
    source = usable[0]
    return bool(
        _as_text(source.get("full_content"))
        and _as_text(source.get("tool")) in _DIRECT_API_TOOLS
        and not direct_api_failure_reason(source.get("tool"), source.get("full_content"))
        and (_as_text(source.get("url")).startswith("direct://")
             or _as_text(source.get("domain")).startswith("direct://"))
    )


def enforce_sufficiency(result: EvidenceAuditResult, sources_by_id: Mapping[str, Mapping]) -> EvidenceAuditResult:
    """Apply source identity, independence, coverage and full-text hard gates."""
    if result.reason_code not in _REASON_CODES:
        return _invalid_result(result)
    requirements = result.requirements
    if not requirements or len({item.id for item in requirements}) != len(requirements):
        return _invalid_result(result)
    coverage_by_id: dict[str, RequirementCoverage] = {}
    known_ids = set(sources_by_id)
    for coverage in result.coverage:
        if coverage.requirement_id not in {item.id for item in requirements} or coverage.requirement_id in coverage_by_id:
            return _invalid_result(result)
        if (
            (coverage.status != "missing" and not coverage.source_ids)
            or len(set(coverage.source_ids)) != len(coverage.source_ids)
            or any(identifier not in known_ids for identifier in coverage.source_ids)
        ):
            return _invalid_result(result)
        matched = [sources_by_id[identifier] for identifier in coverage.source_ids]
        if any(
            not isinstance(source, Mapping)
            or _as_text(source.get("id")) != identifier
            or not _usable_source(source)
            for identifier, source in zip(coverage.source_ids, matched)
        ):
            return _invalid_result(result)
        domains = tuple(sorted({normalize_source_domain(source) for source in matched} - {""}))
        has_full = any(_has_full_text(source) for source in matched)
        coverage_by_id[coverage.requirement_id] = replace(coverage, independent_domains=domains, has_full_text=has_full)
    normalized = replace(result, coverage=tuple(coverage_by_id.get(item.id, RequirementCoverage(
        item.id, "missing", (), (), False, ""
    )) for item in requirements))
    if direct_api_complete(list(sources_by_id.values())):
        return replace(normalized, sufficient=True, reason_code="direct_api_complete")
    if not result.sufficient:
        reason_code = result.reason_code if result.reason_code in _INSUFFICIENT_REASON_CODES else "invalid_audit"
        return replace(normalized, sufficient=False, reason_code=reason_code)
    required = [item for item in requirements if item.required]
    if any(coverage_by_id.get(item.id, RequirementCoverage(item.id, "missing", (), (), False, "")).status != "supported" for item in required):
        return replace(normalized, sufficient=False, reason_code="missing_requirements")
    if result.conflicts or any(coverage.status == "conflicting" for coverage in normalized.coverage):
        return replace(normalized, sufficient=False, reason_code="conflicting_evidence")
    required_ids = {
        identifier for requirement in required
        for identifier in coverage_by_id[requirement.id].source_ids
    }
    if not any(_has_full_text(sources_by_id[identifier]) for identifier in required_ids):
        return replace(normalized, sufficient=False, reason_code="no_full_text")
    audited_ids = supported_source_ids(normalized)
    stats = evidence_stats([sources_by_id[identifier] for identifier in audited_ids])
    if stats.independent_domains < 2 or stats.content_clusters < 2:
        return replace(normalized, sufficient=False, reason_code="missing_requirements")
    for requirement in required:
        coverage = coverage_by_id[requirement.id]
        requirement_sources = [sources_by_id[identifier] for identifier in coverage.source_ids]
        if requirement.current_or_disputed and (
            len(coverage.independent_domains) < 2
            or _content_cluster_count(requirement_sources) < 2
            or not coverage.has_full_text
        ):
            return replace(normalized, sufficient=False, reason_code="missing_requirements")
    return replace(normalized, sufficient=True, reason_code="coverage_complete")


def supported_source_ids(result: EvidenceAuditResult) -> tuple[str, ...]:
    """Stable IDs that provide at least partial support to an audited requirement."""
    seen: set[str] = set()
    supported: list[str] = []
    for coverage in result.coverage:
        if coverage.status not in {"supported", "partial", "conflicting"}:
            continue
        for identifier in coverage.source_ids:
            if identifier not in seen:
                seen.add(identifier)
                supported.append(identifier)
    return tuple(supported)


def format_evidence_for_audit(sources: Sequence[Mapping], total_budget: int = 12000, per_source: int = 2500) -> str:
    """Give each usable source an identity/summary first, then fairly add full text."""
    usable = [source for source in sources if _usable_source(source)]
    if not usable or total_budget <= 0 or per_source <= 0:
        return ""
    separator = "\n\n"
    join_budget = len(separator) * (len(usable) - 1)
    available = max(0, total_budget - join_budget)
    source_cap = min(per_source, available // len(usable))
    if source_cap <= 0:
        return ""

    def compact(source: Mapping, cap: int) -> str:
        identifier = _as_text(source.get("id"))[:32] or "?"
        base = f"[{identifier}]"
        title = _as_text(source.get("title")) or "-"
        domain = normalize_source_domain(source) or _as_text(source.get("domain")) or "-"
        snippet = _as_text(source.get("snippet")) or _as_text(source.get("full_content")) or "-"
        skeleton = f"{base}\n标题：\n域名：\n摘要："
        # Default 100-source budgets still have room for one character in all
        # fields.  For pathological smaller budgets, retain identity safely.
        if cap < len(skeleton) + 3:
            return base[:cap]
        field_values = (title, domain, snippet)
        sizes = [1, 1, 1]
        remaining = cap - len(skeleton) - sum(sizes)
        caps = (120, 80, 180)
        # Round-robin distribution is deterministic and prevents a giant
        # title from consuming the domain/snippet slots.
        while remaining:
            advanced = False
            for index, value in enumerate(field_values):
                limit = min(len(value), caps[index])
                if sizes[index] < limit and remaining:
                    sizes[index] += 1
                    remaining -= 1
                    advanced = True
            if not advanced:
                break
        return (
            f"{base}\n标题：{title[:sizes[0]]}\n域名：{domain[:sizes[1]]}"
            f"\n摘要：{snippet[:sizes[2]]}"
        )[:cap].rstrip()

    # Reserve a compact identity/summary for every source first.  This is a
    # hard per-source cap; bodies only consume that source's remaining share.
    identity_cap = max(1, min(source_cap, 360))
    headers = [compact(source, identity_cap) for source in usable]
    free = available - sum(len(header) for header in headers)
    pieces: list[str] = []
    remaining = len(usable)
    for source, header in zip(usable, headers):
        body_prefix = "\n正文："
        room = max(0, source_cap - len(header))
        fair_share = free // remaining if remaining else 0
        body_size = min(room, fair_share - len(body_prefix))
        body = _as_text(source.get("full_content"))[:max(0, body_size)]
        piece = header + (body_prefix + body if body else "")
        pieces.append(piece[:source_cap])
        free -= max(0, len(piece) - len(header))
        remaining -= 1
    return separator.join(pieces)[:total_budget]


def needs_evidence_audit(*, user_text: str, source_count: int, tool_call_count: int,
                         suggested_length: str, high_risk: bool = False) -> bool:
    """Pure fast-path guard; only potentially complex searches enter the audit."""
    text = (user_text or "").lower()
    complex_language = any(token in text for token in (
        "比较", "对比", "优缺点", "多个", "分别", "最新", "近期", "争议",
        "compare", "comparison", "versus", " vs ", "latest", "current", "recent", "today", "disputed", "controvers",
    ))
    return bool(
        source_count >= 4 or tool_call_count >= 4 or (suggested_length or "").lower() in {"long", "detailed"}
        or high_risk or complex_language
    )


def evidence_novelty(before_sources: Sequence[Mapping], after_sources: Sequence[Mapping]) -> tuple[int, int, int]:
    """Return newly gained canonical URLs, domains and successful full-text sources."""
    def urls(sources: Sequence[Mapping]) -> set[str]:
        return {_canonical_url(source) for source in sources if _usable_source(source)} - {""}
    def domains(sources: Sequence[Mapping]) -> set[str]:
        return {normalize_source_domain(source) for source in sources if _usable_source(source)} - {""}
    def full_text(sources: Sequence[Mapping]) -> set[str]:
        return {_canonical_url(source) or _as_text(source.get("id")) for source in sources if _usable_source(source) and _has_full_text(source)}
    return (
        len(urls(after_sources) - urls(before_sources)),
        len(domains(after_sources) - domains(before_sources)),
        len(full_text(after_sources) - full_text(before_sources)),
    )
