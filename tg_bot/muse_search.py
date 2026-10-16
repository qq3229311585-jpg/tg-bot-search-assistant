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
    dt = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y年%m月%d日", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_PAGE_DATE_RE = re.compile(r"(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})")


def _infer_page_date(text, now):
    """从正文开头 1500 字里找第一个合理日期(2000 年至今天),找不到返回 None。"""
    for m in _PAGE_DATE_RE.finditer((text or "")[:1500]):
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            continue
        if datetime(2000, 1, 1, tzinfo=timezone.utc) <= dt <= now + timedelta(days=2):
            return dt
    return None


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
        if time_range in _WINDOW_SECONDS:
            published = _parse_published(cand.get("published_at"), now)
            if published is not None and (now - published).total_seconds() > 2 * _WINDOW_SECONDS[time_range]:
                continue  # 明确过期的旧闻直接剔除(日期未知的保留,输出里会标“未知”)
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


COPY_RUN_CHARS = 40
_COPY_STRIP_RE = re.compile(r'[\s，。,.、；;：:"“”\'‘’()（）\[\]【】《》]+')


def _shares_long_run(a, b, n=COPY_RUN_CHARS):
    """两段引文是否含有长度 >= n 的完全相同片段（去空白和常见标点后比较）。"""
    import difflib
    a = _COPY_STRIP_RE.sub("", normalize_ws(a))
    b = _COPY_STRIP_RE.sub("", normalize_ws(b))
    if len(a) < n or len(b) < n:
        return False
    match = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return match.size >= n


def _origin_groups(sids, quotes_by_sid, site_of):
    """把同站的来源先合并，再把不同站之间引文有长段相同的来源合并，返回独立来源数。"""
    parent = {sid: sid for sid in sids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    ordered = sorted(sids)
    for i, x in enumerate(ordered):
        for y in ordered[i + 1:]:
            if site_of.get(x) and site_of.get(x) == site_of.get(y):
                union(x, y)
    for i, x in enumerate(ordered):
        for y in ordered[i + 1:]:
            if find(x) == find(y) or site_of.get(x) == site_of.get(y):
                continue
            if any(_shares_long_run(p, q)
                   for p in quotes_by_sid.get(x, []) for q in quotes_by_sid.get(y, [])):
                union(x, y)
    return len({find(sid) for sid in ordered})


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
        quotes_by_sid = {}
        for qid in quote_ids:
            qu = qid_map[qid]
            quotes_by_sid.setdefault(qu["sid"], []).append(qu["quote"])
        site_sids = {sid for sid in sids if sources.get(sid, {}).get("site")}
        site_of = {sid: sources[sid].get("site") for sid in site_sids}
        groups = _origin_groups(site_sids, quotes_by_sid, site_of) if site_sids else 0
        if groups >= 2:
            credibility = f"互证 {groups} 站 ({', '.join(sorted(sites))})"
            if groups < len(sites):
                credibility += ";其中有转载同源"
        else:
            authority = sorted(src.get("domain") for src in present
                               if _is_authority_domain(src.get("domain") or ""))
            if authority:
                credibility = f"权威 {authority[0]}"
            else:
                rejected.append({"text": text, "reason": "仅单一非权威来源", "quote_ids": list(quote_ids)})
                continue
        accepted.append({"text": text, "quote_ids": list(quote_ids), "credibility": credibility})
    return accepted, rejected


def _rejected_lines(rejected, quotes, sources):
    """未通过项的文字行;若带 quote_ids,附上所引来源网址,方便复查。"""
    qid_to_sid = {qu.get("qid"): qu.get("sid") for qu in quotes}
    out = []
    for item in rejected:
        urls = []
        for qid in item.get("quote_ids") or []:
            url = sources.get(qid_to_sid.get(qid), {}).get("url")
            if url and url not in urls:
                urls.append(url)
        tail = f" (来源: {'; '.join(urls)})" if urls else ""
        out.append(f"- {item.get('text')}: {item.get('reason')}{tail}")
    return out


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
        lines.extend(_rejected_lines(rejected, quotes, sources))
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
        lines.extend(_rejected_lines(rejected, quotes, sources))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# ---- 模型调用层 ----

REWRITE_PROMPT = """你是搜索查询改写器。输入 JSON 含 question。只输出 JSON 对象，字段恰为：queries（2 到 4 条搜索词字符串，至少 1 条与原问题同语言；若问题涉及外文人名、赛事、公司，再加 1 条英文查询；每条要加上时间或实体限定，不得加入问题里没有的事实）、time_range（只能是 day、week、month、any；问题问"最近/今天/最新"用 day 或 week，没有时间含义用 any）、must_terms（1 到 5 个应当字面出现在相关新闻里的关键词）。"""

EXTRACT_PROMPT = """你是证据摘录器。输入 JSON 含 question 和 pages（每项 sid、title、text）。只输出 JSON：{"quotes":[{"sid":..., "quote":...}]}。每个页面最多摘 3 句与问题直接相关的原文，必须从 text 中逐字复制，不得改写、拼接、翻译、省略号缩写；每条 quote 长度 8 到 200 字；页面与问题无关就不要摘；不得编造 sid。"""

CLAIMS_PROMPT = """你是结论归纳器。输入 JSON 含 question 和 quotes（每项 qid、sid、quote）。只输出 JSON：{"claims":[{"text":..., "quote_ids":[...]}], "conflicts":[{"point":..., "qids":[...]}]}。只能依据给出的引文归纳，不得补充引文以外的任何信息；每条结论一句话，quote_ids 列出支撑它的引文编号；不同引文对同一事实说法不一致时，写入 conflicts（point 用一句话概括分歧点，qids 列出各方引文），不要自己选边；但时区不同（如北京时间与美国时间）、单位换算、同一事实的不同写法或表述详略不同，都不算冲突；只有数字或事实本身互相矛盾才写入 conflicts。得不出结论就返回空数组。"""

VERIFY_PROMPT = """你是独立核查器。输入 JSON 含 claim 和 quotes（每项 qid、quote）。只判断：仅凭这些引文，claim 是否被明确支持。只输出 JSON：{"verdict":"pass"|"fail"|"unknown"}；不确定或引文只是碰巧包含关键词而没有回答 claim 时不得输出 pass。"""


def call_model(system_prompt, payload, *, timeout=60, post=None):
    """调用模型，返回解析后的 dict；任何失败返回 None，不抛出。"""
    try:
        import tg_bot.config as config
        if post is None:
            from tg_bot.tools.fetch import http_post as post

        key = config._next_ds_key()
        model = getattr(config, "DEEPSEEK_MODEL", "deepseek-flash")
        base = getattr(config, "DEEPSEEK_API_BASE", "https://api.deepseek.com")
        response = post(
            f"{base}/chat/completions",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                "max_tokens": 2000,
                "temperature": 0.0,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
            },
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        content = ""
        if isinstance(response, dict) and response.get("choices"):
            content = response["choices"][0]["message"].get("content") or ""
        raw = content.strip()
        if not raw:
            return None
        result = json.loads(raw)
        return result if isinstance(result, dict) else None
    except Exception as exc:
        import sys
        print(f"WARNING muse_model_call:{type(exc).__name__}", file=sys.stderr)
        return None


def parse_extract(raw):
    """解析摘录结果为 [{"sid", "quote"}]；每 sid 最多 3 条，总数最多 30 条。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("quotes"), list):
        return []
    per_sid = {}
    out = []
    for item in raw["quotes"]:
        if len(out) >= 30:
            break
        if not isinstance(item, dict):
            continue
        sid, quote = item.get("sid"), item.get("quote")
        if not (isinstance(sid, str) and sid.strip()
                and isinstance(quote, str) and quote.strip()):
            continue
        if per_sid.get(sid, 0) >= 3:
            continue
        per_sid[sid] = per_sid.get(sid, 0) + 1
        out.append({"sid": sid, "quote": quote})
    return out


def _clean_str_list(values):
    """去掉非空 str 以外的项，去重保序。"""
    seen = []
    for value in values:
        if isinstance(value, str) and value.strip() and value not in seen:
            seen.append(value)
    return seen


def parse_claims(raw):
    """解析结论归纳结果，返回 (claims, conflicts)；不合法的项丢弃。"""
    if not isinstance(raw, dict):
        return [], []
    claims, conflicts = [], []
    if isinstance(raw.get("claims"), list):
        for item in raw["claims"]:
            if len(claims) >= 5:
                break
            if not isinstance(item, dict):
                continue
            text, ids = item.get("text"), item.get("quote_ids")
            if not (isinstance(text, str) and text.strip() and isinstance(ids, list)):
                continue
            quote_ids = _clean_str_list(ids)
            if not quote_ids:
                continue
            claims.append({"text": text, "quote_ids": quote_ids})
    if isinstance(raw.get("conflicts"), list):
        for item in raw["conflicts"]:
            if len(conflicts) >= 3:
                break
            if not isinstance(item, dict):
                continue
            point, qids = item.get("point"), item.get("qids")
            if not (isinstance(point, str) and point.strip() and isinstance(qids, list)):
                continue
            clean = _clean_str_list(qids)
            if len(clean) < 2:
                continue
            conflicts.append({"point": point, "qids": clean})
    return claims, conflicts


def parse_verify(raw):
    """仅当 verdict 明确为 pass 时返回 True。"""
    return isinstance(raw, dict) and raw.get("verdict") == "pass"


# ---- 调度层 ----

def _default_dep(name):
    """延迟导入真实实现，测试时通过 deps 注入假函数。"""
    if name == "web":
        from tg_bot.tools.search import web_candidates_all
        return web_candidates_all
    if name == "news":
        from tg_bot.tools.search import execute_news_candidates_all
        return execute_news_candidates_all
    if name == "web_brave":
        from tg_bot.tools.search import web_candidates_all, _web_from_brave
        return lambda q: web_candidates_all(q, providers=(("brave", _web_from_brave),))
    if name == "news_brave":
        from tg_bot.tools.search import _news_from_brave
        return _news_from_brave
    if name == "web_serper":
        from tg_bot.tools.search import web_candidates_all, _web_from_serper
        return lambda q: web_candidates_all(q, providers=(("serper", _web_from_serper),))
    if name == "web_tavily":
        from tg_bot.tools.search import web_candidates_all, _web_from_tavily
        return lambda q: web_candidates_all(q, providers=(("tavily", _web_from_tavily),))
    if name == "fetch":
        from tg_bot.tools.fetch import execute_fetch_content
        return execute_fetch_content
    if name == "canon":
        from tg_bot.tools.search import _canonical_news_url
        return _canonical_news_url
    return call_model


def _safe_call(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def _rows_from(result):
    rows = result[0] if isinstance(result, tuple) and result else result
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _not_found(q, time_range, stamp, queries, reason):
    return render_markdown(q, time_range, stamp, queries, [], [], {}, [],
                           [{"text": q, "reason": reason}])


def quota_status(counts, limits, pct=80):
    """各搜索提供方是否还可用于按需查询:用量 < 额度的 pct% 为 True;额度未知(<=0)为 True。"""
    if not isinstance(counts, dict):
        counts = {}
    if not isinstance(limits, dict):
        limits = {}

    def ok(used, limit):
        if limit <= 0:
            return True
        return used < limit * pct / 100

    return {
        "brave": ok(counts.get("brave", 0), limits.get("brave", 0)),
        "serper": ok(counts.get("serper", 0), limits.get("serper", 0)),
        "tavily": ok(counts.get("tavily_0", 0) + counts.get("tavily_1", 0),
                     limits.get("tavily_0", 0) + limits.get("tavily_1", 0)),
    }


def _load_quota_status():
    """读取本月额度并判断各提供方是否可用;任何异常都视为可用,不阻断查询。"""
    try:
        from tg_bot.storage import load_quota
        from tg_bot.config import API_FREE_LIMITS
        return quota_status(load_quota().get("counts", {}), API_FREE_LIMITS)
    except Exception:
        return {"brave": True, "serper": True, "tavily": True}


ENOUGH_SITES = 6
LAST_SEARCH_CALLS = 0


def run_query(q, *, deps=None, now=None):
    """执行完整查询流程并返回 Markdown；任何内部错误降级为"没有找到"，不抛出。"""
    global LAST_SEARCH_CALLS
    if now is None:
        now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    try:
        from concurrent.futures import ThreadPoolExecutor
        from tg_bot.report_research import _clean_content

        injected = deps or {}

        def dep(name):
            fn = injected.get(name)
            return fn if fn is not None else _default_dep(name)

        model = dep("model")
        rewrite = parse_rewrite(_safe_call(model, REWRITE_PROMPT, {"question": q}), q)
        time_range, queries = rewrite["time_range"], rewrite["queries"]

        fetch, canon = dep("fetch"), dep("canon")
        rows, seen = [], set()

        def merge(results):
            for result in results:
                for row in _rows_from(result):
                    url = row.get("url") or ""
                    if not url:
                        continue
                    key = canon(url)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(row)

        search_calls = 0
        legacy = bool(injected) and ("web" in injected or "news" in injected) and "web_brave" not in injected
        if legacy:
            web, news = dep("web"), dep("news")
            jobs = [(web, variant) for variant in queries[:4]]
            if time_range in ("day", "week"):
                jobs += [(news, variant) for variant in queries[:4]]
            search_calls = len(jobs)
            with ThreadPoolExecutor(max_workers=4) as pool:
                merge(list(pool.map(lambda job: _safe_call(job[0], job[1]), jobs)))
        else:
            variants = queries[:3]

            # 额度保护:某提供方本月用量 >= 80% 时按需查询不再使用它
            quota_fn = injected.get("quota")
            if quota_fn is None:
                quota_fn = _load_quota_status if deps is None else (
                    lambda: {"brave": True, "serper": True, "tavily": True})
            allowed = _safe_call(quota_fn)
            if not isinstance(allowed, dict):
                allowed = {"brave": True, "serper": True, "tavily": True}
            if not any(allowed.get(p, True) for p in ("brave", "serper", "tavily")):
                LAST_SEARCH_CALLS = 0
                return _not_found(q, time_range, stamp, queries,
                                  "本月搜索额度已用到 80% 以上,按需查询已暂停以保护日报,下月自动恢复")

            def run_stage(jobs):
                nonlocal search_calls
                search_calls += len(jobs)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    merge(list(pool.map(lambda job: _safe_call(job[0], job[1]), jobs)))

            def enough():
                hosts = {urlparse(row.get("url") or "").hostname for row in rows}
                return len({site_key(h) for h in hosts if h}) >= ENOUGH_SITES

            # 可用提供方按固定优先级 brave -> serper -> tavily 排成阶段
            stages = []
            if allowed.get("brave", True):
                jobs = [(dep("web_brave"), v) for v in variants]
                if time_range in ("day", "week"):
                    jobs += [(dep("news_brave"), v) for v in variants]
                stages.append(jobs)
            if allowed.get("serper", True) and variants:
                stages.append([(dep("web_serper"), v) for v in variants])
            if allowed.get("tavily", True) and variants:
                stages.append([(dep("web_tavily"), variants[0])])
            for index, jobs in enumerate(stages):
                if index > 0 and enough():
                    break
                run_stage(jobs)
        LAST_SEARCH_CALLS = search_calls

        ranked = rank_candidates(rows, rewrite["must_terms"], time_range, now)
        with ThreadPoolExecutor(max_workers=4) as pool:
            texts = list(pool.map(lambda cand: _safe_call(fetch, cand.get("url") or ""), ranked))
        pages, sources = {}, {}
        for cand, raw in zip(ranked, texts):
            text = _clean_content(raw)
            if not text or len(text) < 200:
                continue
            url = cand.get("url") or ""
            domain = (urlparse(url).hostname or "").lower()
            date_str = str(cand["published_at"]) if cand.get("published_at") else "未知"
            if _parse_published(cand.get("published_at"), now) is None:
                inferred = _infer_page_date(text, now)
                if inferred is not None:
                    if (time_range in _WINDOW_SECONDS
                            and (now - inferred).total_seconds() > 2 * _WINDOW_SECONDS[time_range]):
                        continue  # 搜索结果没给日期,但正文里的日期明显过期
                    date_str = inferred.date().isoformat() + "(正文内日期)"
            sid = f"S{len(pages) + 1}"
            pages[sid] = text[:6000]
            sources[sid] = {
                "url": url,
                "title": cand.get("title") or "",
                "site": site_key(domain) if domain else "",
                "date": date_str,
                "domain": domain,
            }
        if not pages:
            return _not_found(q, time_range, stamp, queries, "未能获取任何可用页面")

        extract_raw = _safe_call(model, EXTRACT_PROMPT, {
            "question": q,
            "pages": [{"sid": sid, "title": sources[sid]["title"], "text": pages[sid]}
                      for sid in pages],
        })
        quotes = verify_quotes(parse_extract(extract_raw), pages)
        if not quotes:
            return _not_found(q, time_range, stamp, queries, "没有找到可核对的原文引文")

        claims_raw = _safe_call(model, CLAIMS_PROMPT, {
            "question": q,
            "quotes": [{"qid": qu["qid"], "sid": qu["sid"], "quote": qu["quote"]}
                       for qu in quotes],
        })
        claims, raw_conflicts = parse_claims(claims_raw)
        accepted, rejected = corroborate(claims, quotes, sources)

        checked = accepted[:5]

        def check(claim):
            subset = [{"qid": qu["qid"], "quote": qu["quote"]}
                      for qu in quotes if qu["qid"] in claim["quote_ids"]]
            verdict = _safe_call(model, VERIFY_PROMPT, {"claim": claim["text"], "quotes": subset})
            return parse_verify(verdict)

        with ThreadPoolExecutor(max_workers=4) as pool:
            verdicts = list(pool.map(check, checked))
        kept = []
        for claim, ok in zip(checked, verdicts):
            if ok:
                kept.append(claim)
            else:
                rejected.append({"text": claim["text"], "reason": "核查未通过", "quote_ids": list(claim["quote_ids"])})
        accepted = kept

        qid_map = {qu["qid"]: qu for qu in quotes}
        conflicts = []
        for conflict in raw_conflicts:
            positions, sids = [], set()
            for qid in conflict["qids"]:
                qu = qid_map.get(qid)
                if qu is None:
                    continue
                positions.append({"sid": qu["sid"], "quote": qu["quote"]})
                sids.add(qu["sid"])
            if len(sids) < 2:
                continue
            conflicts.append({"point": conflict["point"], "positions": positions})

        return render_markdown(q, time_range, stamp, queries, accepted, quotes,
                               sources, conflicts, rejected)
    except Exception as exc:
        return _not_found(q, "any", stamp, [q], f"内部错误: {type(exc).__name__}")


def load_env_file(path, environ):
    """读取 KEY=VALUE 形式的环境文件(同 systemd EnvironmentFile 的简单子集),不覆盖已有变量。"""
    import re as _re
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        environ.setdefault(key, value)
        count += 1
    return count


class _QueryTimeout(BaseException):
    """超时信号专用:继承 BaseException,避免被内部 `except Exception` 吞掉。"""


def _audit(log_path, status, q, started, result_chars, extra=None):
    """追加一行 JSON 审计日志；任何异常都吞掉，不影响主流程。"""
    try:
        import hashlib
        import time
        from datetime import datetime as _dt, timezone as _tz
        q = q or ""
        record = {
            "ts": _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": status,
            "q_sha256": hashlib.sha256(q.encode("utf-8")).hexdigest(),
            "q": q[:300],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "result_chars": result_chars,
        }
        if extra:
            record.update(extra)
        line = json.dumps(record, ensure_ascii=False)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def main(argv=None, *, stdin_bytes=None, runner=None, env=None):
    """命令行入口：返回退出码 0 成功 / 1 查询失败 / 2 输入无效 / 3 已暂停 / 4 忙 / 5 超时。"""
    import os
    import sys
    import time
    import signal
    import fcntl

    if env is None:
        env = os.environ
        env_file = env.get("MUSE_SEARCH_ENV_FILE")
        if env_file:
            load_env_file(env_file, os.environ)
    if runner is None:
        runner = run_query
    started = time.monotonic()
    disabled_file = env.get("MUSE_SEARCH_DISABLED_FILE", "/etc/muse-search.disabled")
    lock_dir = env.get("MUSE_SEARCH_LOCK_DIR", "/var/lib/morning-report/muse-search")
    log_path = env.get("MUSE_SEARCH_LOG", "/var/log/muse-search.log")
    try:
        timeout = int(env.get("MUSE_SEARCH_TIMEOUT", "180"))
    except (TypeError, ValueError):
        timeout = 180

    def emit(text):
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if os.path.exists(disabled_file):
        emit("查询工具已被管理员暂停。")
        _audit(log_path, "disabled", "", started, 0)
        return 3

    if stdin_bytes is None:
        stdin_bytes = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    q, err = validate_request(stdin_bytes)
    if q is None:
        emit(f"输入无效: {err}")
        _audit(log_path, "invalid", "", started, 0)
        return 2

    slot = None
    try:
        os.makedirs(lock_dir, mode=0o700, exist_ok=True)
        for name in ("slot0", "slot1"):
            fh = open(os.path.join(lock_dir, name), "a+")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fh.close()
                continue
            except OSError:
                fh.close()
                break  # 锁机制异常：放行，不阻断查询
            slot = fh
            break
        else:
            emit("服务正忙,请稍后再试。")
            _audit(log_path, "busy", q, started, 0)
            return 4
    except OSError:
        slot = None  # 目录或文件异常：放行，不阻断查询

    def _on_alarm(signum, frame):
        raise _QueryTimeout()

    try:
        if timeout <= 0:
            timeout = 180
        try:
            old_handler = signal.signal(signal.SIGALRM, _on_alarm)
        except (ValueError, OSError):
            old_handler = None  # 非主线程等情况：无超时保护
        try:
            if old_handler is not None:
                signal.alarm(timeout)
            try:
                result = runner(q)
            finally:
                if old_handler is not None:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)
        except (_QueryTimeout, TimeoutError):
            emit(f"查询超时({timeout} 秒),请缩小问题范围后重试。")
            _audit(log_path, "timeout", q, started, 0)
            return 5
        except Exception as exc:
            emit(f"查询失败: {type(exc).__name__}")
            _audit(log_path, "error", q, started, 0)
            return 1
        emit(result)
        _audit(log_path, "ok", q, started, len(result),
               extra={"search_calls": LAST_SEARCH_CALLS})
        return 0
    finally:
        if slot is not None:
            try:
                fcntl.flock(slot.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            slot.close()


if __name__ == "__main__":
    import os as _os
    import sys as _sys
    _code = main()
    _sys.stdout.flush()
    _sys.stderr.flush()
    _os._exit(_code)  # 硬退出:超时后不等仍在跑的网络线程
