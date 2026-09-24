#!/usr/bin/env python3
"""workers/claim_check.py — 确定性事实核查（纯函数，无网络）

职责：把"这句话有没有来源支撑"这类可以用代码判断的问题，从模型手里拿回来。

提供：
    split_sentences      — 按标点/换行切句，保留原文位置
    extract_hard_facts   — 从一句话里抽取数字/日期/百分比等硬事实
    fact_supported       — 判断一个 Fact 是否能在素材原文里找到支撑
    check_claims         — 对整篇回复做规则核查，输出 Issue 列表
    remove_sentences     — 按位置精确删除指定句子并清理残留空行/孤立标题
    compute_verdict      — 只由代码根据 issues 计算 pass/patch/rewrite
    validate_patch       — 校验 Patcher 输出是否越界
    deterministic_patch  — Patcher 模型不可用/越界时的确定性兜底修补
    finalize_reply       — 发送前最后一次规则核查 + 兜底

不做的事：不判断“说得好不好”，不判断素材是否可信，不调用模型。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from tg_bot.core.contracts import Issue, Source

# ── 通用正则 ────────────────────────────────────────────────────────────────

_SENT_DELIM_RE = re.compile(r'[^。！？!?；;]*[。！？!?；;]')
_REF_RE = re.compile(r'[\[【]\s*来源\s*(\d+)\s*[\]】]')
_VAGUE_WORDS = ("约", "近", "超过", "逾", "左右", "大约", "将近", "多")
_HEADING_LEN_LIMIT = 24
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "☀-➿"
    "←-⇿"
    "⬀-⯿"
    "]"
)
_SOURCE_HEADING_RE = re.compile(r'^[\W\d]{0,3}来源[：:]?\s*$')
_URL_RE = re.compile(r'https?://[^\s，,、）)】\]]+')
_BARE_DOMAIN_RE = re.compile(
    r'(?:^|[\s（(【\[·\-])([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,})(?=[\s）)】\]，,。]|$)'
)
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^)]+)\)')
_LIST_PREFIX_RE = re.compile(r'^\s*(?:[①②③④⑤⑥⑦⑧⑨⑩]|\d+[.、)]|[（(]\d+[）)])\s*')

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９．，", "0123456789.,")

_CN_DIGIT = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}
_CN_BIG_UNIT = {"万": 10000, "亿": 100000000}


# ── 数据结构 ─────────────────────────────────────────────────────────────────

@dataclass
class Sentence:
    text: str
    start: int
    end: int
    refs: list[int] = field(default_factory=list)
    is_heading: bool = False
    is_source_block: bool = False


@dataclass
class Fact:
    raw: str
    value: float
    kind: str   # year / date / number / percent / money
    start: int  # 在句子文本里的相对位置
    end: int


# ── 分句 ─────────────────────────────────────────────────────────────────────

def _extract_refs(text: str) -> list[int]:
    refs = []
    for m in _REF_RE.finditer(text or ""):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n not in refs:
            refs.append(n)
    return refs


def _is_heading_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if len(s) > _HEADING_LEN_LIMIT:
        return False
    if re.search(r'[。！？!?]', s):
        return False
    if _SOURCE_HEADING_RE.match(s):
        return True
    if _EMOJI_RE.search(s[:4]):
        return True
    if s.endswith(("：", ":")):
        return True
    # 短纯标签行（结论/关键依据/下一步等），不含逗号、不含来源标注
    if len(s) <= 10 and not re.search(r'[，,\[【]', s):
        return True
    return False


def split_sentences(text: str) -> list[Sentence]:
    """按 。！？!?；; 和换行切句，保留原文位置，标注标题/来源段。"""
    text = text or ""
    sentences: list[Sentence] = []
    in_source_block = False
    offset = 0
    for line in text.split("\n"):
        line_len = len(line)
        stripped = line.strip()
        if not stripped:
            offset += line_len + 1
            continue

        heading = _is_heading_line(line)
        if heading and _SOURCE_HEADING_RE.match(stripped):
            in_source_block = True
        elif heading:
            # 任何其他标题行（新分组开始）都退出来源段
            in_source_block = False

        if heading:
            sentences.append(Sentence(
                text=line, start=offset, end=offset + line_len,
                refs=_extract_refs(line), is_heading=True,
                is_source_block=in_source_block,
            ))
            offset += line_len + 1
            continue

        pos = 0
        found_any = False
        for m in _SENT_DELIM_RE.finditer(line):
            seg = m.group(0)
            if seg.strip():
                found_any = True
                sentences.append(Sentence(
                    text=seg, start=offset + m.start(), end=offset + m.end(),
                    refs=_extract_refs(seg), is_heading=False,
                    is_source_block=in_source_block,
                ))
            pos = m.end()
        rest = line[pos:]
        if rest.strip():
            sentences.append(Sentence(
                text=rest, start=offset + pos, end=offset + line_len,
                refs=_extract_refs(rest), is_heading=False,
                is_source_block=in_source_block,
            ))
        elif not found_any:
            pass
        offset += line_len + 1
    return sentences


# ── 文本归一化 ────────────────────────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    """去 markdown 链接、全角转半角、去千分位逗号，供数字比对使用。"""
    text = text or ""
    text = _MD_LINK_RE.sub(r'\1', text)
    text = text.translate(_FULLWIDTH_DIGITS)
    # 去掉数字千分位分隔符（不影响小数点，因为上面已转换为 .）；
    # 不用 \b：中文单位（亿/万）也是单词字符，"1,234亿" 会匹配不上
    text = re.sub(r'(?<=\d),(?=\d{3}(?!\d))', '', text)
    return text


def _cn_number_to_value(s: str) -> Optional[float]:
    """把常见中文数字（两千/三万/十二/一百二十等）转成数值，失败返回 None。"""
    if not s:
        return None
    if s == "十":
        return 10.0
    total = 0
    section = 0
    num = 0
    has_digit = False
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
            has_digit = True
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if num == 0:
                num = 1
            section += num * unit
            num = 0
            has_digit = True
        else:
            return None
    section += num
    total += section
    return float(total) if has_digit else None


# ── 数值抽取（可复用于句子和素材原文） ─────────────────────────────────────────

# 按优先级排列，避免重复捕获（如年份不应再被当作普通数字）
_MONEY_RE = re.compile(
    r'[¥￥$€£]\s*\d[\d.]*|(?:\d[\d.]*)\s*(?:元|美元|人民币|欧元|英镑|日元|港元|港币)'
)
_PERCENT_RE = re.compile(r'\d+(?:\.\d+)?\s*%|百分之\s*\d+(?:\.\d+)?')
_DATE_ISO_RE = re.compile(r'\d{4}-\d{1,2}-\d{1,2}')
_DATE_CN_RE = re.compile(r'\d{4}年\d{1,2}月(?:\d{1,2}日)?|\d{1,2}月\d{1,2}日')
_YEAR_RE = re.compile(r'(?:1[5-9]|20)\d{2}年?(?!\d)')
_THOUSANDS_RE = re.compile(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?')
_CN_UNIT_NUM_RE = re.compile(
    r'(?:\d+(?:\.\d+)?|[一二三四五六七八九十两]+)\s*[万亿千]'
)
_PLAIN_NUM_RE = re.compile(r'\d+(?:\.\d+)?')


def _iter_raw_matches(text: str):
    """在归一化文本上按优先级抽取 (raw, kind, value, start, end)，避免重叠。"""
    text = text or ""
    claimed: list[tuple[int, int]] = []

    def _overlaps(s, e):
        return any(not (e <= a or s >= b) for a, b in claimed)

    def _mark(s, e):
        claimed.append((s, e))

    results = []

    for m in _MONEY_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        num_m = re.search(r'\d[\d.]*', m.group(0))
        if not num_m:
            continue
        try:
            val = float(num_m.group(0))
        except ValueError:
            continue
        _mark(m.start(), m.end())
        results.append((m.group(0), "money", val, m.start(), m.end()))

    for m in _PERCENT_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        raw = m.group(0)
        if raw.startswith("百分之"):
            num_m = re.search(r'\d+(?:\.\d+)?', raw)
            val = float(num_m.group(0)) if num_m else 0.0
        else:
            val = float(re.sub(r'[^\d.]', '', raw))
        _mark(m.start(), m.end())
        results.append((raw, "percent", val, m.start(), m.end()))

    for m in _DATE_ISO_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        _mark(m.start(), m.end())
        parts = re.split(r'-', m.group(0))
        norm = "-".join(p.zfill(2) if len(p) < 3 else p for p in parts)
        results.append((m.group(0), "date", norm, m.start(), m.end()))

    for m in _DATE_CN_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        _mark(m.start(), m.end())
        nums = re.findall(r'\d+', m.group(0))
        results.append((m.group(0), "date", "-".join(n.zfill(2) for n in nums), m.start(), m.end()))

    for m in _YEAR_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        _mark(m.start(), m.end())
        year = int(re.sub(r'\D', '', m.group(0)))
        results.append((m.group(0), "year", float(year), m.start(), m.end()))

    for m in _THOUSANDS_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        _mark(m.start(), m.end())
        val = float(m.group(0).replace(",", ""))
        results.append((m.group(0), "number", val, m.start(), m.end()))

    for m in _CN_UNIT_NUM_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        raw = m.group(0)
        unit_char = raw[-1]
        head = raw[:-1].strip()
        base = None
        try:
            base = float(head)
        except ValueError:
            base = _cn_number_to_value(head)
        if base is None:
            continue
        val = base * _CN_BIG_UNIT.get(unit_char, 1000)
        _mark(m.start(), m.end())
        results.append((raw, "number", val, m.start(), m.end()))

    for m in _PLAIN_NUM_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        _mark(m.start(), m.end())
        try:
            val = float(m.group(0))
        except ValueError:
            continue
        results.append((m.group(0), "number", val, m.start(), m.end()))

    results.sort(key=lambda r: r[3])
    return results


def extract_hard_facts(sentence) -> list[Fact]:
    """从一句话（Sentence 或字符串）抽取硬事实。"""
    text = sentence.text if isinstance(sentence, Sentence) else str(sentence or "")
    # 去掉行首列表序号、来源标注，避免误抽（[来源3] 里的 3）
    body = _LIST_PREFIX_RE.sub('', text)
    body = _REF_RE.sub('', body)
    norm = _normalize_text(body)
    facts = []
    for raw, kind, value, start, end in _iter_raw_matches(norm):
        facts.append(Fact(raw=raw, value=value, kind=kind, start=start, end=end))
    return facts


def _values_of_kind(norm_text: str, kind: str):
    return [v for (_, k, v, _, _) in _iter_raw_matches(norm_text) if k == kind]


def fact_supported(fact: Fact, source_text: str, fuzzy: bool = False) -> bool:
    """判断 fact 是否能在 source_text（单条或拼接后的素材原文）里找到支撑。"""
    if not source_text:
        return False
    norm_src = _normalize_text(source_text)
    if fact.raw and (fact.raw in source_text or fact.raw in norm_src):
        return True
    candidates = _values_of_kind(norm_src, fact.kind)
    for v in candidates:
        if fact.kind in ("year", "date"):
            if v == fact.value:
                return True
            continue
        if v == fact.value:
            return True
        if fuzzy and isinstance(v, (int, float)) and isinstance(fact.value, (int, float)) and fact.value:
            if abs(v - fact.value) <= abs(fact.value) * 0.01:
                return True
    return False


def _sentence_is_vague(text: str) -> bool:
    return any(w in (text or "") for w in _VAGUE_WORDS)


# ── 来源段（URL/域名伪造）检查 ─────────────────────────────────────────────────

def _source_urls_and_domains(sources: list[Source]) -> tuple[set, set]:
    urls, domains = set(), set()
    for s in sources or []:
        u = (getattr(s, "url", "") or "").strip()
        d = (getattr(s, "domain", "") or "").strip().lower()
        if u:
            urls.add(u)
        if d:
            domains.add(d)
    return urls, domains


def _domain_of(url: str) -> str:
    try:
        return url.split("//", 1)[-1].split("/", 1)[0].lower()
    except Exception:
        return ""


# ── 主检查入口 ───────────────────────────────────────────────────────────────

def check_claims(reply: str, sources: list[Source], user_query: str = "") -> list[Issue]:
    """对草稿做规则核查，返回 Issue 列表（origin="rule"）。"""
    reply = reply or ""
    sources = sources or []
    n_sources = len(sources)
    bodies = [(getattr(s, "body", "") or "") for s in sources]
    all_text = "\n".join(bodies)
    known_urls, known_domains = _source_urls_and_domains(sources)

    user_query = user_query or ""
    norm_query = _normalize_text(user_query)

    sentences = split_sentences(reply)
    issues: list[Issue] = []

    for sent in sentences:
        if sent.is_heading:
            continue

        if sent.is_source_block:
            suspects = list(_URL_RE.findall(sent.text))
            bad = False
            for url in suspects:
                if url not in known_urls and _domain_of(url) not in known_domains:
                    bad = True
                    break
            if not bad:
                for dom in _BARE_DOMAIN_RE.findall(sent.text):
                    dom_low = dom.lower()
                    if dom_low not in known_domains and not any(
                        d.endswith("." + dom_low) or dom_low.endswith("." + d)
                        for d in known_domains
                    ):
                        bad = True
                        break
            if bad:
                issues.append(Issue(
                    sentence=sent.text.strip(),
                    source_ref="",
                    severity="HARD",
                    reason="来源段列出的链接/域名未出现在素材里，疑似伪造来源",
                    origin="rule",
                ))
            continue

        # 引用越界
        bad_refs = [r for r in sent.refs if r < 1 or r > n_sources]
        if bad_refs:
            issues.append(Issue(
                sentence=sent.text.strip(),
                source_ref=",".join(f"来源{r}" for r in bad_refs),
                severity="HARD",
                reason="引用了不存在的来源编号",
                origin="rule",
            ))
            continue  # 引用本身就坏了，不再做数字比对

        facts = [
            f for f in extract_hard_facts(sent)
            if not (f.raw and f.raw in user_query)
            and not (f.raw and f.raw in norm_query)
        ]
        if not facts:
            continue

        fuzzy = _sentence_is_vague(sent.text)
        valid_refs = [r for r in sent.refs if 1 <= r <= n_sources]

        if valid_refs:
            ref_bodies = "\n".join(bodies[r - 1] for r in valid_refs)
            unsupported = [f for f in facts if not fact_supported(f, ref_bodies, fuzzy=fuzzy)]
            if not unsupported:
                continue
            # 是否在其他素材里能找到
            found_other = None
            for f in unsupported:
                for idx, body in enumerate(bodies, 1):
                    if idx in valid_refs:
                        continue
                    if fact_supported(f, body, fuzzy=fuzzy):
                        found_other = idx
                        break
                if found_other:
                    break
            if found_other:
                issues.append(Issue(
                    sentence=sent.text.strip(),
                    source_ref=",".join(f"来源{r}" for r in valid_refs),
                    severity="SOFT",
                    reason=f"来源标注错误，应为来源{found_other}",
                    suggested_fix=f"改为 [来源{found_other}]",
                    origin="rule",
                ))
            else:
                # 找所引来源里同类型数字所在句作为证据片段
                evidence = ""
                for r in valid_refs:
                    m_vals = _iter_raw_matches(_normalize_text(bodies[r - 1]))
                    same_kind = [raw for raw, k, *_ in m_vals if k == unsupported[0].kind]
                    if same_kind:
                        idx_pos = bodies[r - 1].find(same_kind[0])
                        if idx_pos >= 0:
                            evidence = bodies[r - 1][max(0, idx_pos - 40):idx_pos + 60]
                        break
                issues.append(Issue(
                    sentence=sent.text.strip(),
                    source_ref=",".join(f"来源{r}" for r in valid_refs),
                    severity="HARD",
                    reason="数字/日期与来源不符",
                    evidence_excerpt=evidence,
                    origin="rule",
                ))
        else:
            found_at = None
            for f in facts:
                for idx, body in enumerate(bodies, 1):
                    if fact_supported(f, body, fuzzy=fuzzy):
                        found_at = idx
                        break
                if found_at:
                    break
            if found_at:
                issues.append(Issue(
                    sentence=sent.text.strip(),
                    source_ref="",
                    severity="SOFT",
                    reason="缺少来源标注",
                    suggested_fix=f"在句末补 [来源{found_at}]",
                    origin="rule",
                ))
            else:
                if fact_supported(facts[0], all_text, fuzzy=fuzzy):
                    continue
                issues.append(Issue(
                    sentence=sent.text.strip(),
                    source_ref="",
                    severity="HARD",
                    reason="无来源支撑的数字/日期",
                    origin="rule",
                ))

    return issues


# ── 删句 ─────────────────────────────────────────────────────────────────────

def remove_sentences(text: str, sentences_to_remove: list) -> str:
    """按位置精确删除指定句子，清理残留空行/孤立标题。"""
    text = text or ""
    spans = sorted(
        {(s.start, s.end) for s in sentences_to_remove if isinstance(s, Sentence)},
        key=lambda p: p[0], reverse=True,
    )
    for start, end in spans:
        text = text[:start] + text[end:]

    lines = text.split("\n")
    cleaned_lines = [ln.rstrip() for ln in lines]

    # 去掉孤立标题：标题行后面直到下一个标题/文本结尾都没有实质内容
    out = []
    i = 0
    while i < len(cleaned_lines):
        line = cleaned_lines[i]
        if _is_heading_line(line):
            j = i + 1
            has_content = False
            while j < len(cleaned_lines):
                nxt = cleaned_lines[j].strip()
                if not nxt:
                    j += 1
                    continue
                if _is_heading_line(cleaned_lines[j]):
                    break
                has_content = True
                break
            if not has_content:
                i += 1
                continue
        out.append(line)
        i += 1

    result = "\n".join(out)
    result = re.sub(r'[ \t]+\n', '\n', result)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


# ── 结论计算（只由代码算） ─────────────────────────────────────────────────────

def compute_verdict(issues: list[Issue]) -> str:
    """0 issues → pass；HARD>=2 或 HARD+SOFT>=5 → rewrite；其余 → patch。"""
    issues = issues or []
    if not issues:
        return "pass"
    hard = sum(1 for i in issues if i.severity == "HARD")
    total = len(issues)
    if hard >= 2 or total >= 5:
        return "rewrite"
    return "patch"


# ── Patch 越界校验 + 确定性修补 ────────────────────────────────────────────────

_PATCH_ARTIFACT_RE = re.compile(r'已修改|原文|改为|←|修改说明')


def _normalize_for_compare(text: str) -> str:
    return re.sub(r'\s+', '', _normalize_text(text or ""))


def normalize_sentence(text: str) -> str:
    """句子级归一化：去 [来源N] 标注 + 通用文本归一化，供跨模块比较句子用。"""
    return _normalize_for_compare(_REF_RE.sub('', text or ""))


_canon = normalize_sentence


def _sentence_texts(text: str, *, exclude: Optional[set] = None) -> list[str]:
    exclude = exclude or set()
    out = []
    for s in split_sentences(text):
        if s.is_heading:
            continue
        norm = _canon(s.text)
        if not norm or norm in exclude:
            continue
        out.append(norm)
    return out


def validate_patch(
    original: str,
    patched: str,
    issues: list[Issue],
    sources: Optional[list[Source]] = None,
) -> tuple[bool, str]:
    """校验 Patcher 输出是否越界。失败时返回 (False, 原因)。"""
    patched = (patched or "").strip()
    original = original or ""
    if not patched:
        return False, "补丁为空"
    if len(patched) < len(original) * 0.4:
        return False, "补丁过度删减（少于原文 40%）"
    if _PATCH_ARTIFACT_RE.search(patched):
        return False, "补丁包含修改痕迹标注"

    flagged_norm = {_canon(iss.sentence) for iss in (issues or [])}
    unflagged = _sentence_texts(original, exclude=set())
    unflagged = [s for s in unflagged if s not in flagged_norm]
    if unflagged:
        patched_norm_blob = _canon(patched)
        kept = sum(1 for s in unflagged if s and s in patched_norm_blob)
        if kept / len(unflagged) < 0.9:
            return False, "未被点名的句子改动过多"

    if sources is not None:
        original_hard = {
            _canon(iss.sentence)
            for iss in (issues or []) if iss.severity == "HARD"
        }
        new_issues = check_claims(patched, sources)
        for iss in new_issues:
            if iss.severity != "HARD":
                continue
            if _canon(iss.sentence) in original_hard:
                continue
            return False, f"补丁引入了新的 HARD 问题: {iss.reason}"

    return True, ""


def deterministic_patch(original: str, issues: list[Issue], sources: Optional[list[Source]] = None) -> str:
    """确定性修补：删 HARD 句；有明确目标来源的 SOFT 引用错误改引用；其余不动。"""
    sentences = split_sentences(original or "")
    by_norm: dict[str, Sentence] = {}
    for s in sentences:
        if s.is_heading:
            continue
        by_norm.setdefault(_canon(s.text), s)

    to_remove: list[Sentence] = []
    replacements: dict[tuple[int, int], str] = {}

    for iss in issues or []:
        key = _canon(iss.sentence)
        target = by_norm.get(key)
        if not target:
            continue
        if iss.severity == "HARD":
            to_remove.append(target)
            continue
        # SOFT：缺少来源标注 / 来源标注错误 且有明确目标来源 K
        m = re.search(r'来源\s*(\d+)', iss.suggested_fix or iss.reason or "")
        if not m:
            continue
        k = m.group(1)
        new_text = target.text
        if "缺少来源标注" in (iss.reason or ""):
            stripped = new_text.rstrip()
            trailing = new_text[len(stripped):]
            if stripped and stripped[-1] in "。！？!?；;":
                new_text = stripped[:-1] + f"[来源{k}]" + stripped[-1] + trailing
            else:
                new_text = stripped + f"[来源{k}]" + trailing
            replacements[(target.start, target.end)] = new_text
        elif "来源标注错误" in (iss.reason or ""):
            new_text = _REF_RE.sub(f"[来源{k}]", new_text, count=1)
            replacements[(target.start, target.end)] = new_text

    text = original or ""
    # 先做替换（不改变整体删除位置计算，替换段与删除段不会重叠，因为 severity 不同）
    for (start, end), new_text in sorted(replacements.items(), key=lambda kv: kv[0][0], reverse=True):
        text = text[:start] + new_text + text[end:]

    if to_remove:
        text = remove_sentences(text, to_remove)
    else:
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


# ── 终检 ─────────────────────────────────────────────────────────────────────

_FALLBACK_REPLY = (
    "搜到的资料不足以可靠回答这个问题。可以换个问法，或发送「再查一下」让我换角度重新搜索。"
)


def finalize_reply(reply: str, sources: Optional[list[Source]], user_query: str = "") -> tuple[str, dict]:
    """发送前最后一次规则核查：删剩余 HARD 句，删空则回退固定文案。

    返回 (final_reply, info)，info 含 removed_count / final_hard_issues / used_fallback。
    """
    sources = sources or []
    issues = check_claims(reply, sources, user_query)
    hard = [iss for iss in issues if iss.severity == "HARD"]

    final_text = reply or ""
    removed_count = 0
    used_fallback = False
    if hard:
        sentences = split_sentences(final_text)
        hard_norm = {_canon(iss.sentence) for iss in hard}
        to_remove = [
            s for s in sentences
            if not s.is_heading and _canon(s.text) in hard_norm
        ]
        if to_remove:
            final_text = remove_sentences(final_text, to_remove)
            removed_count = len(to_remove)

        # 只在确实删过句子之后才检查是否被删空——原本就很短但没有问题的
        # 回复（比如一句话答案）不应该被兜底文案顶替。
        if removed_count:
            effective_len = len(re.sub(r'\s+', '', final_text))
            if effective_len < 20:
                final_text = _FALLBACK_REPLY
                used_fallback = True

    return final_text, {
        "removed_count": removed_count,
        "final_hard_issues": len(hard),
        "used_fallback": used_fallback,
    }
