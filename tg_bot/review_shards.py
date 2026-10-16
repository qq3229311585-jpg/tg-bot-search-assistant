#!/usr/bin/env python3
"""Deterministic, citation-aware helpers for reviewing long chat replies."""
from __future__ import annotations

from dataclasses import dataclass, replace
import re

from tg_bot.core.contracts import CriticReport, Issue, Source


_CITATION_RE = re.compile(r"\[来源\s*([A-Za-z0-9_-]+)\]")
_TITLE_RE = re.compile(r"(?:#{1,6}\s+\S.*|【[^】]+】)")
_SENTENCE_RE = re.compile(r".*?[。！？!?]+|.+$", re.DOTALL)
_PREFLIGHT_SENTENCE_RE = re.compile(
    r"[^。！？!?\n]+(?:[。！？!?]+(?:\s*\[来源\s*[A-Za-z0-9_-]+\])*)?|$"
)
_NUMERIC_FACT_RE = re.compile(
    r"(?:\b(?:19|20)\d{2}(?:[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?)?|"
    r"\d+(?:\.\d+)?\s*(?:%|％|元|美元|人民币|万|亿|倍|GB|MB|公里|km|岁|天|小时|分钟))",
    re.I,
)
_VERSION_RE = re.compile(r"(?:\b[vV]?\d+\.\d+(?:\.\d+)?\b|版本\s*\d+)")
_ENGLISH_ENTITY_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Za-z0-9]{1,}(?:[ -][A-Z][A-Za-z0-9]{1,})*")
_ADVICE_RE = re.compile(r"^(?:建议|可以|应当|最好|请|不妨|记得|推荐|避免)")
_STEP_RE = re.compile(r"^(?:第?\s*\d+\s*步|步骤\s*\d+|step\s*\d+)", re.I)
_FACT_PREDICATE_RE = re.compile(
    r"(?:发布|宣布|推出|位于|获得|夺冠|上涨|下降|成立|去世|死亡|通过|启用|发生|收购|任命|"
    r"更新|上线|停止|增长|减少|达到|成为|共有|published|released|announced|launched|"
    r"acquired|appointed|updated|stopped|grew|increased|decreased|reached|became)",
    re.I,
)
_CHINESE_ENTITY_STOP = frozenset({"我们", "你们", "他们", "大家", "本文", "这里", "下面", "上述", "本节"})
_CHINESE_NON_ENTITY_PREFIX_RE = re.compile(
    r"^(?:这个|该|此|本|一些|很多|如果|当|在|从|对|把|将|让|需要|可以|应当|建议|预计|可能)"
)
_PERSONAL_ACTION_RE = re.compile(
    r"^(?:我(?:们)?|你(?:们)?|咱们|本人)(?:已经|已|可以|能够|能|应该|建议|正在|将)?"
)
_EDITION_HEADING_RE = re.compile(r"第\s*\d+\s*(?:版|章|节|部分|篇|条|项)[^。！？!?\n:：]*$")
_THIRD_PERSON_RE = re.compile(r"^(?:他(?:们)?|她(?:们)?|它(?:们)?)")
_DEMONSTRATIVE_ENTITY_RE = re.compile(
    r"^(?:该|此)(?:公司|机构|产品|集团|企业|部门|平台|学校|医院|银行|品牌|球队|队)$"
)


@dataclass(frozen=True)
class ReviewShard:
    index: int
    text: str
    citation_numbers: tuple[str, ...]
    separator_before: str = ""


@dataclass(frozen=True)
class _Piece:
    text: str
    separator_before: str


@dataclass(frozen=True)
class _Chunk:
    pieces: tuple[_Piece, ...]

    @property
    def separator_before(self) -> str:
        return self.pieces[0].separator_before if self.pieces else ""

    @property
    def text(self) -> str:
        return "".join(
            (piece.separator_before if index else "") + piece.text
            for index, piece in enumerate(self.pieces)
        )


def _citation_numbers(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    numbers: list[str] = []
    for match in _CITATION_RE.finditer(text or ""):
        number = match.group(1)
        if number not in seen:
            seen.add(number)
            numbers.append(number)
    return tuple(numbers)


def should_shard_review(reply: str, sources_text_length: int, threshold: int = 1200) -> bool:
    """Keep short replies on the existing one-shot Critic path."""
    return len(reply or "") > threshold or sources_text_length > 8000 or (reply or "").count("\n\n") >= 4 and bool(_citation_numbers(reply))


def _paragraph_units(reply: str) -> list[_Piece]:
    tokens = re.split(r"(\n{2,})", reply or "")
    paragraphs: list[_Piece] = []
    separator = ""
    for token in tokens:
        if re.fullmatch(r"\n{2,}", token or ""):
            separator = token
        else:
            paragraphs.append(_Piece(token, separator))
            separator = ""
    units: list[_Piece] = []
    index = 0
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        if _TITLE_RE.fullmatch(paragraph.text.strip()) and index + 1 < len(paragraphs):
            following = paragraphs[index + 1]
            units.append(_Piece(paragraph.text + following.separator_before + following.text, paragraph.separator_before))
            index += 2
        else:
            units.append(paragraph)
            index += 1
    return units


def _sentence_pieces(piece: _Piece, threshold: int) -> list[_Piece]:
    text = piece.text
    title_prefix = re.match(r"(?s)^(.+?)(\n{2,})(.*)$", text)
    if title_prefix and _TITLE_RE.fullmatch(title_prefix.group(1).strip()):
        sentences = [match.group(0) for match in _SENTENCE_RE.finditer(title_prefix.group(3)) if match.group(0)]
        if sentences:
            sentences[0] = title_prefix.group(1) + title_prefix.group(2) + sentences[0]
        else:
            sentences = [text]
    else:
        sentences = [match.group(0) for match in _SENTENCE_RE.finditer(text) if match.group(0)]
    if not sentences:
        return [piece]
    for index in range(1, len(sentences)):
        citation = re.match(r"(?:\s*\[来源\s*[A-Za-z0-9_-]+\])+", sentences[index])
        if citation:
            sentences[index - 1] += citation.group(0)
            sentences[index] = sentences[index][len(citation.group(0)):]
    expanded: list[str] = []
    for sentence in sentences:
        if len(sentence) > threshold and not re.search(r"[。！？!?]", sentence):
            expanded.extend(_hard_boundary_pieces(sentence, threshold))
        else:
            expanded.append(sentence)

    pieces: list[_Piece] = []
    current = ""
    for sentence in expanded:
        if not sentence:
            continue
        if len(sentence) > threshold:
            if current:
                pieces.append(_Piece(current, piece.separator_before if not pieces else ""))
                current = ""
            pieces.append(_Piece(sentence, piece.separator_before if not pieces else ""))
            continue
        if current and len(current) + len(sentence) > threshold:
            pieces.append(_Piece(current, piece.separator_before if not pieces else ""))
            current = sentence
        else:
            current += sentence
    if current:
        pieces.append(_Piece(current, piece.separator_before if not pieces else ""))
    return pieces


def _hard_boundary_pieces(text: str, threshold: int) -> list[str]:
    """Split an unpunctuated overlong sentence without ever cutting a citation token."""
    pieces: list[str] = []
    offset = 0
    citations = tuple(_CITATION_RE.finditer(text))
    while len(text) - offset > threshold:
        cut = offset + threshold
        for citation in citations:
            if citation.start() < cut < citation.end():
                cut = citation.start()
                break
        if cut <= offset:
            for citation in citations:
                if citation.start() == offset:
                    cut = citation.end()
                    break
        if cut <= offset:
            cut = min(len(text), offset + threshold)
        pieces.append(text[offset:cut])
        offset = cut
    if offset < len(text):
        pieces.append(text[offset:])
    return pieces


def _merge_to_limit(chunks: list[_Chunk], max_chunks: int) -> list[_Chunk]:
    if len(chunks) <= max_chunks:
        return chunks
    merged: list[_Chunk] = []
    total = len(chunks)
    for group in range(max_chunks):
        start = group * total // max_chunks
        end = (group + 1) * total // max_chunks
        merged.append(_Chunk(tuple(piece for chunk in chunks[start:end] for piece in chunk.pieces)))
    return merged


def split_review_text(reply: str, threshold: int = 1200, max_chunks: int = 6) -> tuple[ReviewShard, ...]:
    """Split at paragraphs first, then sentence ends, without splitting citations."""
    text = reply or ""
    if not text:
        return ()
    threshold = max(1, threshold)
    max_chunks = max(1, max_chunks)
    chunks: list[_Chunk] = []
    current: list[_Piece] = []

    def _append(piece: _Piece) -> None:
        nonlocal current
        candidate = _Chunk(tuple(current + [piece]))
        if current and len(candidate.text) > threshold:
            chunks.append(_Chunk(tuple(current)))
            current = [piece]
        else:
            current.append(piece)

    for unit in _paragraph_units(text):
        if len(unit.text) <= threshold:
            _append(unit)
            continue

        if current:
            chunks.append(_Chunk(tuple(current)))
            current = []
        for piece in _sentence_pieces(unit, threshold):
            _append(piece)
    if current:
        chunks.append(_Chunk(tuple(current)))
    chunks = _merge_to_limit(chunks, max_chunks)
    return tuple(
        ReviewShard(
            index=index,
            text=chunk.text,
            citation_numbers=_citation_numbers(chunk.text),
            separator_before=chunk.separator_before,
        )
        for index, chunk in enumerate(chunks, start=1)
    )


def rebuild_review_text(shards: tuple[ReviewShard, ...] | list[ReviewShard]) -> str:
    """Rebuild the exact original text from boundary-aware shards."""
    return "".join(shard.separator_before + shard.text for shard in shards)


def preflight_shard_issues(shard: ReviewShard) -> tuple[Issue, ...]:
    """Fail closed for concrete factual sentences that lack their own citation."""
    issues: list[Issue] = []
    for paragraph in re.split(r"\n{2,}", shard.text or ""):
        stripped = paragraph.strip()
        if not stripped or _TITLE_RE.fullmatch(stripped):
            continue
        for match in _PREFLIGHT_SENTENCE_RE.finditer(paragraph):
            sentence = match.group(0).strip()
            if not sentence or _CITATION_RE.search(sentence):
                continue
            if (
                _ADVICE_RE.match(sentence)
                or _STEP_RE.match(sentence)
                or _PERSONAL_ACTION_RE.match(sentence)
                or _EDITION_HEADING_RE.fullmatch(sentence)
                or sentence.rstrip().endswith((":", "："))
            ):
                continue
            predicate = _FACT_PREDICATE_RE.search(sentence)
            has_special_numeric = bool(_NUMERIC_FACT_RE.search(sentence) or _VERSION_RE.search(sentence))
            has_generic_numeric_fact = bool(re.search(r"\d", sentence) and predicate)
            has_english_entity_fact = bool(_ENGLISH_ENTITY_RE.search(sentence) and predicate)
            has_chinese_entity_fact = _has_chinese_entity_fact(sentence, predicate)
            has_third_person_fact = bool(_THIRD_PERSON_RE.match(sentence) and predicate)
            if not (
                has_special_numeric
                or has_generic_numeric_fact
                or has_english_entity_fact
                or has_chinese_entity_fact
                or has_third_person_fact
            ):
                continue
            issues.append(Issue(
                sentence=sentence,
                source_ref="无引用",
                severity="HARD",
                reason="具体事实没有对应来源编号，无法核验。",
                suggested_fix="为该事实补充有效的 [来源N] 标注，或删除无法核验的表述。",
            ))
    return tuple(issues)


def _has_chinese_entity_fact(sentence: str, predicate: re.Match[str] | None) -> bool:
    """Recognize only a short entity-shaped Chinese subject before a factual predicate."""
    if not predicate:
        return False
    subject = sentence[:predicate.start()].strip(" ，、：:")
    subject = re.sub(r"(?:已经|已|正在|将)$", "", subject)
    if subject in _CHINESE_ENTITY_STOP or _CHINESE_NON_ENTITY_PREFIX_RE.match(subject):
        if _DEMONSTRATIVE_ENTITY_RE.fullmatch(subject):
            return True
        return False
    return bool(re.fullmatch(r"(?:某[\u4e00-\u9fff]{1,6}|[\u4e00-\u9fff]{2,16})", subject))


_GENERIC_EVIDENCE_NGRAMS = frozenset({
    "发布", "推出", "更新", "宣布", "公司", "产品", "手机", "新闻", "事实", "消息",
})


def _prefix_has_conservative_support(prefix: str, required_terms: tuple[str, ...]) -> bool:
    """Recognize time + entity/content overlap without pretending it is full semantic proof."""
    requested = " ".join(required_terms)
    if not requested or not prefix:
        return False
    requested_dates = set(re.findall(r"(?:19|20)\d{2}|\d{1,2}(?=月)", requested))
    prefix_dates = set(re.findall(r"(?:19|20)\d{2}|\d{1,2}(?=月)", prefix))

    def _ngrams(text: str) -> set[str]:
        result: set[str] = set()
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            result.update(run[index:index + 2] for index in range(len(run) - 1))
        return result

    shared = _ngrams(requested) & _ngrams(prefix)
    anchors = shared - _GENERIC_EVIDENCE_NGRAMS
    return bool((requested_dates & prefix_dates and anchors) or len(anchors) >= 2)


def _source_for_evidence(
    source: Source,
    allowed: int,
    citation_label: str,
    required_terms: tuple[str, ...] = (),
) -> tuple[Source, bool]:
    """Route a prefix or a deterministic matching window, plus auditability."""
    if allowed <= 0:
        return replace(source, full_content="", snippet="", citation_label=citation_label), False
    body = source.body
    prefix = body[:allowed]
    if any(term in prefix for term in required_terms) or _prefix_has_conservative_support(prefix, required_terms):
        selected, auditable = prefix, True
    else:
        match_at = next((body.find(term) for term in required_terms if body.find(term) >= 0), -1)
        if match_at >= 0:
            start = max(0, match_at - allowed // 2)
            end = min(len(body), start + allowed)
            start = max(0, end - allowed)
            selected, auditable = body[start:end], True
        else:
            selected, auditable = prefix, len(body) <= allowed
    if source.full_content:
        return replace(source, full_content=selected, snippet=source.snippet[:allowed], citation_label=citation_label), auditable
    return replace(source, snippet=selected, citation_label=citation_label), auditable


def _citation_terms(shard: ReviewShard, citation: str) -> tuple[str, ...]:
    """Extract deterministic, citation-local terms to find a deep evidence window."""
    marker = re.compile(r"\[来源\s*" + re.escape(citation) + r"\]")
    match = marker.search(shard.text or "")
    if not match:
        return ()
    start = max(
        (shard.text or "").rfind(boundary, 0, match.start())
        for boundary in ("。", "！", "？", "!", "?", "\n")
    ) + 1
    end_candidates = [
        position for boundary in ("。", "！", "？", "!", "?", "\n")
        if (position := (shard.text or "").find(boundary, match.end())) >= 0
    ]
    end = min(end_candidates) if end_candidates else len(shard.text or "")
    sentence = marker.sub("", (shard.text or "")[start:end])
    terms = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", sentence)
    return tuple(sorted(dict.fromkeys(terms), key=lambda item: (-len(item), item)))


def route_shard_sources(
    shard: ReviewShard,
    sources: list[Source],
    evidence_budget: int = 6000,
) -> tuple[list[Source], tuple[Issue, ...]]:
    """Return only the sources cited by one shard, in citation order."""
    by_id = {source.id: source for source in sources}
    resolved: list[tuple[str, Source]] = []
    issues: list[Issue] = []
    for citation in shard.citation_numbers:
        source = None
        if citation.isdigit():
            if 1 <= int(citation) <= len(sources):
                source = sources[int(citation) - 1]
        else:
            source = by_id.get(citation)
        if source is None:
            issues.append(Issue(
                sentence=shard.text.strip(),
                source_ref=f"来源{citation}",
                severity="HARD",
                reason="引用了不存在的来源编号，无法核验该事实。",
                suggested_fix="删除该引用或改为有效来源编号。",
            ))
            continue
        if any(item.id == source.id for _label, item in resolved):
            continue
        resolved.append((citation, source))

    # Keep a single cited source's visible prefix aligned with Writer.  This
    # preserves deep facts that Writer may cite from source 1 (4k) or source
    # 2/3 (3k), while the existing allocator still enforces the shard-wide
    # evidence budget when several sources are cited together.
    source_positions = {id(source): position for position, source in enumerate(sources, start=1)}

    def _writer_visible_cap(source: Source) -> int:
        position = source_positions.get(id(source), 0)
        if position == 1:
            return 4000
        if position in (2, 3):
            return 3000
        return 2500

    capacities = [min(_writer_visible_cap(source), len(source.body)) for _citation, source in resolved]
    allowances = [0] * len(resolved)
    remaining = max(0, evidence_budget)
    active = {index for index, capacity in enumerate(capacities) if capacity > 0}
    while remaining and active:
        share = max(1, remaining // len(active))
        for index in tuple(active):
            added = min(share, capacities[index] - allowances[index], remaining)
            allowances[index] += added
            remaining -= added
            if allowances[index] >= capacities[index]:
                active.remove(index)
            if not remaining:
                break

    routed = []
    for (citation, source), allowance in zip(resolved, allowances):
        routed_source, auditable = _source_for_evidence(
            source, allowance, citation, _citation_terms(shard, citation),
        )
        routed.append(routed_source)
        if allowance <= 0:
            issues.append(Issue(
                sentence=shard.text.strip(), source_ref=f"来源{citation}", severity="HARD",
                reason="该引用没有可分配的核验材料。", suggested_fix="补充可用来源或缩小本段引用范围。",
            ))
        elif not auditable:
            issues.append(Issue(
                sentence=shard.text.strip(), source_ref=f"来源{citation}", severity="HARD",
                reason="该引用的材料被预算截断，且无法定位草稿所述事实，不能可靠核查。",
                suggested_fix="补充可定位的来源摘录，或删除无法核验的表述。",
            ))
    return routed, tuple(issues)


def _issue_key(issue: Issue) -> tuple[str, str, str]:
    sentence = re.sub(r"\s+", " ", issue.sentence or "").strip()
    source_ref = re.sub(r"\s+", "", issue.source_ref or "")
    return sentence, source_ref, (issue.severity or "").upper()


def aggregate_critic_reports(
    reports: list[CriticReport],
    preflight_issues: tuple[Issue, ...] = (),
) -> CriticReport:
    """Fail closed when any shard is unknown and keep one copy of each issue."""
    issues: list[Issue] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in (*preflight_issues, *(issue for report in reports for issue in report.issues)):
        key = _issue_key(issue)
        if key not in seen:
            seen.add(key)
            issues.append(issue)

    unknown_count = sum(report.verdict == "unknown" for report in reports)
    hard_count = sum(issue.severity == "HARD" for issue in issues)
    if unknown_count:
        verdict = "unknown"
    elif hard_count >= 2:
        verdict = "rewrite"
    elif issues or any(report.verdict != "pass" for report in reports):
        verdict = "patch"
    else:
        verdict = "pass"
    return CriticReport(
        verdict=verdict,
        issues=issues,
        mode="sharded",
        chunks_total=len(reports),
        chunks_passed=sum(report.verdict == "pass" for report in reports),
        chunks_unknown=unknown_count,
    )
