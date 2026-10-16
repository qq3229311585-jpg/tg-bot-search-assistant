#!/usr/bin/env python3
"""muse_search.py — Muse 按需查询的纯函数积木（不含网络、命令行与模型调用）"""

from datetime import datetime, timedelta, timezone
import json, re, unicodedata
from urllib.parse import urlparse

from tg_bot.report_research import site_key, _is_authority_domain

MAX_STDIN_BYTES = 4096
MIN_Q_CHARS = 2
MAX_Q_CHARS = 300

_TIME_RANGES = {"day", "week", "month", "any"}
_WINDOW_SECONDS = {"day": 86400, "week": 168 * 3600, "month": 720 * 3600}
_REL_EN = re.compile(r"^(\d+)\s*(minute|min|hour|hr|day|week|month)s?\s+ago$", re.IGNORECASE)
_REL_ZH = re.compile(r"^(\d+)\s*(分钟|小时|天|周|个月|月)前$")
_EN_UNIT = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600,
            "day": 86400, "week": 604800, "month": 2592000}
_ZH_UNIT = {"分钟": 60, "小时": 3600, "天": 86400, "周": 604800,
            "个月": 2592000, "月": 2592000}


def validate_request(raw):
    """校验 stdin 请求，返回 (q, "") 或 (None, 错误信息)。"""
    if isinstance(raw, bytes):
        if len(raw) > MAX_STDIN_BYTES:
            return None, "输入超过 4096 字节"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "输入不是合法 UTF-8"
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_STDIN_BYTES:
            return None, "输入超过 4096 字节"
        text = raw
    else:
        return None, "输入类型不支持"
    try:
        obj = json.loads(text)
    except ValueError:
        return None, "输入不是合法 JSON"
    if not isinstance(obj, dict) or set(obj.keys()) != {"q"}:
        return None, "输入必须是只含 q 字段的 JSON 对象"
    q = obj["q"]
    if not isinstance(q, str):
        return None, "q 必须是字符串"
    q = unicodedata.normalize("NFKC", q).strip()
    if not (MIN_Q_CHARS <= len(q) <= MAX_Q_CHARS):
        return None, f"q 长度须在 {MIN_Q_CHARS}-{MAX_Q_CHARS} 字之间"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in q):
        return None, "q 含控制字符"
    return q, ""


def parse_rewrite(raw, original_q):
    """解析模型返回的改写结果，原始问题始终排第一。"""
    if not isinstance(raw, dict):
        raw = {}
    valid = []
    queries_in = raw.get("queries")
    if isinstance(queries_in, list):
        for item in queries_in:
            if not isinstance(item, str):
                continue
            s = item.strip()
            if s and len(s) <= 120 and s not in valid:
                valid.append(s)
    rest = [s for s in valid if s != original_q]
    queries = [original_q] + rest[:3]

    time_range = raw.get("time_range")
    if time_range not in _TIME_RANGES:
        time_range = "any"

    must_terms = []
    terms_in = raw.get("must_terms")
    if isinstance(terms_in, list):
        for item in terms_in:
            if not isinstance(item, str):
                continue
            s = item.strip()
            if s and len(s) <= 30 and s not in must_terms:
                must_terms.append(s)
            if len(must_terms) >= 6:
                break
    return {"queries": queries, "time_range": time_range, "must_terms": must_terms}


def _parse_published(value, now):
    """解析发布时间：ISO 日期/时间或相对时间文本；无法解析返回 None。"""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    m = _REL_EN.match(s)
    if m:
        return now - timedelta(seconds=int(m.group(1)) * _EN_UNIT[m.group(2).lower()])
    m = _REL_ZH.match(s)
    if m:
        return now - timedelta(seconds=int(m.group(1)) * _ZH_UNIT[m.group(2)])
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _freshness(published_at, time_range, now):
    """按时间窗口计算新鲜度：未知 0.2，any 固定 0.5，窗口内线性衰减。"""
    dt = _parse_published(published_at, now)
    if dt is None:
        return 0.2
    if time_range not in _WINDOW_SECONDS:
        return 0.5
    age = max(0.0, (now - dt).total_seconds())
    window = _WINDOW_SECONDS[time_range]
    if age > window:
        return 0.0
    return 1.0 - age / window


def rank_candidates(cands, must_terms, time_range, now=None, per_site=2, top=6):
    """按权威度、关键词命中与新鲜度打分排序，同站点最多保留 per_site 条。"""
    if now is None:
        now = datetime.now(timezone.utc)
    terms = [t.lower() for t in (must_terms or []) if t]
    scored = []
    for cand in cands:
        url = cand.get("url") or ""
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            host = ""
        authority = 1 if _is_authority_domain(host) else 0
        text = f"{cand.get('title') or ''} {cand.get('summary') or ''}".lower()
        if terms:
            hit_ratio = sum(1 for t in terms if t in text) / len(terms)
        else:
            hit_ratio = 0.5
        freshness = _freshness(cand.get("published_at"), time_range, now)
        entry = dict(cand)
        entry["score"] = float(3 * authority + 2 * hit_ratio + freshness)
        entry["site"] = site_key(host)
        scored.append(entry)
    scored.sort(key=lambda e: -e["score"])
    result = []
    counts = {}
    for entry in scored:
        if len(result) >= top:
            break
        site = entry["site"]
        if counts.get(site, 0) >= per_site:
            continue
        counts[site] = counts.get(site, 0) + 1
        result.append(entry)
    return result


def normalize_ws(text):
    """把所有空白（含全角空格与换行）压成单个空格并去掉首尾空白。"""
    return " ".join(str(text or "").split())


def verify_quotes(quotes, pages):
    """只保留能在对应正文中逐字找到（空白归一化后）的引文，并编号 Q1、Q2…"""
    kept = []
    for item in quotes:
        if not isinstance(item, dict):
            continue
        sid = item.get("sid")
        if not isinstance(sid, str) or sid not in pages:
            continue
        quote = item.get("quote")
        if not isinstance(quote, str):
            continue
        page = pages[sid]
        if not isinstance(page, str):
            continue
        nq = normalize_ws(quote)
        if len(nq) < 8:
            continue
        if nq not in normalize_ws(page):
            continue
        kept.append({"sid": sid, "quote": nq, "qid": f"Q{len(kept) + 1}"})
    return kept


def corroborate(claims, quotes, sources):
    """按站点互证或权威来源判定结论是否通过，返回 (accepted, rejected)。"""
    qid_map = {q["qid"]: q for q in quotes}
    accepted, rejected = [], []
    for claim in claims:
        text = claim.get("text") if isinstance(claim, dict) else None
        if not isinstance(text, str) or not text.strip():
            rejected.append({"text": text, "reason": "结论文本无效"})
            continue
        quote_ids = claim.get("quote_ids")
        if (not isinstance(quote_ids, list) or not quote_ids
                or not all(isinstance(qid, str) and qid in qid_map for qid in quote_ids)):
            rejected.append({"text": text, "reason": "引用无效"})
            continue
        sids = {qid_map[qid]["sid"] for qid in quote_ids}
        present = [sources[sid] for sid in sids if sid in sources]
        sites = {src.get("site") for src in present if src.get("site")}
        if len(sites) >= 2:
            credibility = f"互证 {len(sites)} 站 ({', '.join(sorted(sites))})"
        else:
            authority = sorted(src.get("domain") for src in present
                               if _is_authority_domain(src.get("domain") or ""))
            if authority:
                credibility = f"权威 {authority[0]}"
            else:
                rejected.append({"text": text, "reason": "仅单一非权威来源"})
                continue
        accepted.append({"text": text, "quote_ids": list(quote_ids), "credibility": credibility})
    return accepted, rejected


def render_markdown(q, time_range, generated_at_iso, queries, accepted, quotes,
                    sources, conflicts, rejected):
    """把核查结果渲染为 Markdown 报告文本。"""
    lines = [
        f"# 查询: {q}",
        f"时间范围: {time_range} | 生成: {generated_at_iso} | 变体: {'; '.join(queries)}",
        "",
    ]
    if not accepted and not conflicts:
        lines.append("## 没有找到")
        for item in rejected:
            lines.append(f"- {item.get('text')}: {item.get('reason')}")
        if not rejected:
            lines.append("- 未找到可互证的结论")
        return "\n".join(lines) + "\n"

    qid_map = {qu["qid"]: qu for qu in quotes}
    lines.append("## 结论")
    if not accepted:
        lines.append("- 无通过核查的结论")
    for idx, claim in enumerate(accepted, 1):
        lines.append(f"### C{idx} {claim['text']}")
        lines.append(f"可信度: {claim['credibility']}")
        seen_sids = []
        for qid in claim["quote_ids"]:
            sid = qid_map[qid]["sid"]
            if sid not in seen_sids:
                seen_sids.append(sid)
        for sid in seen_sids:
            src = sources.get(sid, {})
            lines.append(f"- [{sid}] {src.get('title', '')} | {src.get('site', '')} | "
                         f"{src.get('date') or '未知'} | {src.get('url', '')}")
            for qid in claim["quote_ids"]:
                if qid_map[qid]["sid"] == sid:
                    lines.append(f'  > "{qid_map[qid]["quote"]}"')
        lines.append("")

    if conflicts:
        lines.append("## 来源说法不一致")
        for conflict in conflicts:
            lines.append(f"- {conflict.get('point')}:")
            for pos in conflict.get("positions", []):
                sid = pos.get("sid")
                url = sources.get(sid, {}).get("url", "")
                lines.append(f'  - [{sid}] "{pos.get("quote")}" | {url}')
        lines.append("")

    if rejected:
        lines.append("## 没有找到 / 未通过核查")
        for item in rejected:
            lines.append(f"- {item.get('text')}: {item.get('reason')}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
