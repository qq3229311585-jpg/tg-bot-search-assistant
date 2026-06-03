#!/usr/bin/env python3
"""facts.py — facts_json builder for verifier and source tracing."""

import re


_FACT_RE = re.compile(r"^\[(F\d{3})\]\s*(.+?)\s*$")
_SOURCE_RE = re.compile(r"^\s*来源：\s*(.+?)\s*$")
_QUOTE_RE = re.compile(r'^\s*原文片段：\s*["“](.+?)["”]\s*$')


def _norm(s):
    return (s or "").strip()


def _domain_from_source(src):
    src = _norm(src)
    if not src:
        return ""
    if src.startswith("直接API-"):
        return src
    return src.split("（", 1)[0].strip()


def _short(text, n):
    text = _norm(text)
    return text[:n]


def _match_evidence(domains, quote, source_index):
    evidence = []
    quote_l = quote.lower()
    for entry in source_index or []:
        domain = _norm(entry.get("domain"))
        full = _norm(entry.get("full_content"))
        snippet = _norm(entry.get("snippet"))
        hay = (domain + " " + full + " " + snippet).lower()
        domain_hit = any(d and d.lower() in hay for d in domains)
        quote_hit = bool(quote_l and quote_l[:20] in hay)
        if not domain_hit and not quote_hit:
            continue
        evidence.append({
            "source_id": entry.get("id", ""),
            "domain": domain,
            "title": _short(entry.get("title", ""), 120),
            "url": entry.get("url", ""),
            "quote": quote,
            "material_excerpt": _short(full or snippet, 1500),
        })
        if len(evidence) >= 3:
            break
    return evidence


def build_facts_json(fact_list, source_index=None):
    """Convert the text facts sheet into a minimal compatible facts_json."""
    facts = []
    current = None
    section = ""

    for raw in (fact_list or "").splitlines():
        line = raw.rstrip()
        if line.startswith("【") and line.endswith("】"):
            section = line.strip("【】")
            continue

        m = _FACT_RE.match(line)
        if m:
            current = {
                "id": m.group(1),
                "claim": _norm(m.group(2)),
                "section": section,
                "source_text": "",
                "source_domains": [],
                "quote": "",
                "evidence": [],
            }
            facts.append(current)
            continue

        if not current:
            continue
        sm = _SOURCE_RE.match(line)
        if sm:
            current["source_text"] = _norm(sm.group(1))
            dom = _domain_from_source(current["source_text"])
            if dom:
                current["source_domains"].append(dom)
            continue
        qm = _QUOTE_RE.match(line)
        if qm:
            current["quote"] = _norm(qm.group(1))

    for fact in facts:
        fact["source_domains"] = list(dict.fromkeys(fact.get("source_domains") or []))
        fact["evidence"] = _match_evidence(
            fact["source_domains"],
            fact.get("quote", ""),
            source_index or [],
        )

    return {
        "schema_version": 1,
        "fact_count": len(facts),
        "facts": facts,
    }
