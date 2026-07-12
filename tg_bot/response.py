"""Stable user-facing reply envelopes and rendering.

The pipeline may still receive plain text from older writers.  This module
normalizes that text without inventing facts, then renders only the fields
that are appropriate for the selected response mode.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from collections.abc import Iterable, Mapping


_ALLOWED_CONFIDENCE = {"high", "medium", "low", "unknown"}
_VISIBLE_SOURCE_MODES = {"search", "news", "report"}
_SOURCE_MARK_RE = re.compile(r"\[来源\d+\]")
_INTERNAL_LINE_RE = re.compile(r"^(?:reasoning|思考(?:过程|链)?|analysis)\s*[:：]", re.I)
_HEADING_RE = re.compile(
    r"^\s*[【\[]?(结论|核心结论|关键依据|依据|证据|下一步|行动建议|来源|参考来源)"
    r"[】\]]?\s*[:：]?\s*$"
)


def _clean(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _as_text_tuple(values: Iterable[object] | None) -> tuple[str, ...]:
    if not values:
        return ()
    result = []
    for value in values:
        text = _clean(value)
        if text:
            result.append(text)
    return tuple(result)


def _as_sources(values: Iterable[Mapping[str, object]] | None) -> tuple[dict, ...]:
    if not values:
        return ()
    result = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        item = {
            key: _clean(raw)
            for key, raw in value.items()
            if raw is not None and _clean(raw)
        }
        if item:
            result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class ReplyEnvelope:
    conclusion: str
    evidence: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    sources: tuple[dict, ...] = ()
    confidence: str = "unknown"
    mode: str = "answer"

    def __post_init__(self) -> None:
        object.__setattr__(self, "conclusion", _clean(self.conclusion))
        object.__setattr__(self, "evidence", _as_text_tuple(self.evidence))
        object.__setattr__(self, "actions", _as_text_tuple(self.actions))
        object.__setattr__(self, "sources", _as_sources(self.sources))
        confidence = str(self.confidence or "unknown").lower()
        object.__setattr__(
            self,
            "confidence",
            confidence if confidence in _ALLOWED_CONFIDENCE else "unknown",
        )
        object.__setattr__(self, "mode", str(self.mode or "answer").lower())


def _safe_conclusion(text: str) -> str:
    return _clean(text) or "目前资料不足，无法确认。"


def _paragraphs(text: str) -> list[str]:
    text = _clean(text)
    if not text:
        return []
    return [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]


def _parse_sections(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    sections: dict[str, list[str]] = {"conclusion": [], "evidence": [], "actions": [], "sources": []}
    current = "conclusion"
    found_heading = False
    for raw_line in _clean(text).splitlines():
        line = raw_line.strip()
        if not line or _INTERNAL_LINE_RE.match(line):
            continue
        match = _HEADING_RE.match(line)
        if match:
            found_heading = True
            label = match.group(1)
            if label in {"结论", "核心结论"}:
                current = "conclusion"
            elif label in {"关键依据", "依据", "证据"}:
                current = "evidence"
            elif label in {"下一步", "行动建议"}:
                current = "actions"
            elif label in {"来源", "参考来源"}:
                current = "sources"
            else:
                current = "evidence"
            continue
        if current != "sources":
            sections[current].append(line)

    if found_heading:
        conclusion = "\n".join(sections["conclusion"])
        evidence = tuple(sections["evidence"])
        actions = tuple(sections["actions"])
        return conclusion, evidence, actions

    paragraphs = _paragraphs(text)
    if not paragraphs:
        return "", (), ()
    return paragraphs[0], tuple(paragraphs[1:]), ()


def normalize_reply(
    text: str,
    *,
    sources: Iterable[Mapping[str, object]] | None = (),
    mode: str = "answer",
) -> ReplyEnvelope:
    """Normalize legacy plain text without adding unsupported content."""

    conclusion, evidence, actions = _parse_sections(text or "")
    return ReplyEnvelope(
        conclusion=conclusion,
        evidence=evidence,
        actions=actions,
        sources=tuple(sources or ()),
        mode=mode,
    )


def _bullet(value: str) -> str:
    value = _clean(value)
    if value.startswith(("- ", "• ", "· ")):
        return value
    return f"• {value}"


def _display_text(value: str, *, keep_source_markers: bool) -> str:
    text = _clean(value)
    return text if keep_source_markers else _SOURCE_MARK_RE.sub("", text)


def _source_line(index: int, source: Mapping[str, object]) -> str:
    title = _clean(source.get("title") or source.get("name") or "未命名来源")
    domain = _clean(source.get("domain") or "")
    url = _clean(source.get("url") or "")
    details = " · ".join(part for part in (title, domain, url) if part)
    return f"[来源{index}] {details}".rstrip()


def _clip(text: str, max_chars: int) -> str:
    if max_chars < 32:
        raise ValueError("max_chars must be at least 32")
    if len(text) <= max_chars:
        return text
    marker = "…"
    limit = max_chars - len(marker)
    boundary = text.rfind("\n", 0, limit)
    if boundary >= max(1, int(limit * 0.55)):
        return text[:boundary].rstrip() + marker
    return text[:limit].rstrip() + marker


def render_reply(envelope: ReplyEnvelope, *, max_chars: int = 3800) -> str:
    """Render a safe user-facing reply while retaining a stable section order."""

    if not isinstance(envelope, ReplyEnvelope):
        raise TypeError("render_reply expects ReplyEnvelope")

    keep_source_markers = envelope.mode in _VISIBLE_SOURCE_MODES
    lines = [_display_text(_safe_conclusion(envelope.conclusion), keep_source_markers=keep_source_markers)]
    if envelope.confidence != "unknown":
        lines.extend(("", f"把握：{envelope.confidence}"))
    if envelope.evidence:
        lines.extend(("", "【关键依据】"))
        lines.extend(_bullet(_display_text(item, keep_source_markers=keep_source_markers)) for item in envelope.evidence[:4])
    if envelope.actions:
        lines.extend(("", "【下一步】"))
        lines.extend(_bullet(_display_text(item, keep_source_markers=keep_source_markers)) for item in envelope.actions[:4])
    if envelope.mode in _VISIBLE_SOURCE_MODES and envelope.sources:
        lines.extend(("", "【来源】"))
        lines.extend(_source_line(index, item) for index, item in enumerate(envelope.sources[:5], 1))
    return _clip("\n".join(lines).strip(), max_chars)


__all__ = ["ReplyEnvelope", "normalize_reply", "render_reply"]
