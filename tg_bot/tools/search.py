#!/usr/bin/env python3
"""tools/search.py — 搜索相关工具函数"""

from collections.abc import Mapping
import json, logging
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen, Request

from tg_bot.config import (
    BRAVE_KEY, TAVILY_KEYS, SERPER_KEYS,
    _ctx,
    _reserve_serper_key,
    _reserve_tavily_key,
    API_FREE_LIMITS,
)
from tg_bot.storage import inc_quota

log = logging.getLogger(__name__)

# ── 优先域名列表（搜索结果来自这些域名的排到前面） ────────────────────
_PREFERRED_DOMAINS = {
    # 国际综合新闻
    "reuters.com","apnews.com","bbc.com","theguardian.com",
    "ft.com","economist.com","nytimes.com","wsj.com",
    "bloomberg.com","time.com","newsweek.com","theatlantic.com",
    "axios.com","npr.org","aljazeera.com","dw.com",
    "france24.com","euronews.com","politico.com","foreignpolicy.com",
    # 科技
    "techcrunch.com","theverge.com","wired.com","arstechnica.com",
    "engadget.com","cnet.com","zdnet.com","venturebeat.com",
    "thenextweb.com","9to5mac.com","macrumors.com",
    "androidauthority.com","tomshardware.com",
    # AI / 学术
    "nature.com","science.org","scientificamerican.com",
    "newscientist.com","arxiv.org","openai.com","anthropic.com",
    "deepmind.com","huggingface.co","mit.edu",
    "the-decoder.com","simonwillison.net",
    "platformer.news","paperswithcode.com",
    # 财经 / 实时数据
    "marketwatch.com","cnbc.com","businessinsider.com",
    "fortune.com","investopedia.com","coindesk.com","cointelegraph.com",
    "coinmarketcap.com","coingecko.com","xe.com",
    "investing.com","finance.yahoo.com","tradingeconomics.com",
    # 亚洲英文媒体
    "scmp.com","nikkei.com","kyodonews.net","straitstimes.com",
    "bangkokpost.com","hindustantimes.com",
    # HackerNews / 开发者圈
    "news.ycombinator.com","github.com","lobste.rs",
    # 羽毛球
    "bwfbadminton.com","bwf.tournamentsoftware.com",
    "badmintonworld.tv","badmintoneurope.com","badmintonasia.org",
    "badmintoncn.com","aiyuke.com",
    # 中文资讯
    "36kr.com","qbitai.com","jiqizhixin.com",
    "ithome.com","sspai.com","zhihu.com",
    # 科普 / 趣闻冷知识
    "quantamagazine.org","atlasobscura.com","phys.org",
    "smithsonianmag.com","mentalfloss.com",
    # 代理 / 安全
    "torrentfreak.com","theregister.com","krebsonsecurity.com",
    "darkreading.com","bleepingcomputer.com",
    # 其他视角
    "restofworld.org",
    # 百科 / 权威参考
    "wikipedia.org","britannica.com","snopes.com","factcheck.org",
}

def _preferred_rank(url: str) -> int:
    """返回 0（优先域名）或 1（普通域名），用于 sorted() key"""
    try:
        host = url.split("/")[2].lstrip("www.")
    except Exception:
        return 1
    # 精确匹配或子域名匹配（如 bwf.tournamentsoftware.com）
    if host in _PREFERRED_DOMAINS:
        return 0
    for d in _PREFERRED_DOMAINS:
        if host.endswith("." + d):
            return 0
    return 1


# ── Tavily key 轮换 ───────────────────────────────────────────────────
def _tavily_request(endpoint, payload):
    """轮换三个 Tavily key，超限自动切下一个"""
    if not TAVILY_KEYS:
        return None
    start_index, _start_key = _reserve_tavily_key()
    for offset in range(len(TAVILY_KEYS)):
        key_index = (start_index + offset) % len(TAVILY_KEYS)
        key = TAVILY_KEYS[key_index]
        payload["api_key"] = key
        try:
            body = json.dumps(payload).encode()
            req  = Request(endpoint, data=body,
                           headers={"Content-Type": "application/json"})
            with urlopen(req, context=_ctx, timeout=25) as r:
                data = json.loads(r.read())
            # 成功，下次从下一个 key 开始（均匀分摊）
            inc_quota(f"tavily_{key_index}")
            return data
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower() or "limit" in err.lower():
                log.warning(f"Tavily key[{key_index}] 超限，切换下一个")
            else:
                log.warning(f"Tavily 请求失败: {e}")
                return None
    return None


# ── 主搜索：Brave 优先，Tavily 降级，Serper 兜底 ─────────────────────
def execute_search(query, search_type="general"):
    from urllib.parse import quote_plus as _qp
    log.info(f"🔍 Brave [{search_type}]: {query}")
    try:
        if search_type == "news":
            url = (f"https://api.search.brave.com/res/v1/news/search"
                   f"?q={_qp(query)}&count=5&freshness=pd")
        else:
            url = (f"https://api.search.brave.com/res/v1/web/search"
                   f"?q={_qp(query)}&count=5")
        from tg_bot.tools.fetch import http_get
        raw = http_get(url, headers={"Accept":"application/json",
                                      "X-Subscription-Token":BRAVE_KEY})
        if not raw:
            return _tavily_search_fallback(query, search_type)
        inc_quota("brave")
        d = json.loads(raw)
        items = (d.get("results", []) if search_type == "news"
                 else d.get("web", {}).get("results", []))
        if not items:
            return _tavily_search_fallback(query, search_type)
        items = sorted(items, key=lambda r: _preferred_rank(r.get("url", "")))
        lines = []
        for r in items[:5]:
            # 发布时间随素材一路传给审计/写作，时效数据回答才能标明数据日期
            age = r.get("page_age") or r.get("age") or r.get("published")
            age_line = f"  📅 {str(age)[:40]}\n" if age else ""
            lines.append(f"• {r.get('title','')}\n"
                         f"  {r.get('description','')[:200]}\n"
                         f"{age_line}"
                         f"  {r.get('url','')}")
        return "\n\n".join(lines)
    except Exception as e:
        log.warning(f"execute_search Brave 异常: {e}")
        return _tavily_search_fallback(query, search_type)


def _validated_news_items(items, provider):
    """Keep mapping rows and report malformed provider rows once."""
    valid = []
    malformed = False
    for item in items:
        if isinstance(item, Mapping):
            valid.append(item)
        else:
            malformed = True
    diagnostics = [f"{provider}: provider_error:TypeError"] if malformed else []
    return valid, diagnostics


_GNEWS_CACHE = {}


def _news_from_gnews(query, time_range="day"):
    """Return free Google News RSS rows; this provider never consumes quota."""
    from tg_bot.gnews import default_get, default_post, gnews_rows

    try:
        raw_rows = gnews_rows(
            query,
            time_range,
            max_items=8,
            get=default_get,
            post=default_post,
            cache=_GNEWS_CACHE,
        )
        rows = [
            {
                "title": row.get("title", ""),
                "summary": row.get("snippet", ""),
                "url": row.get("url", ""),
                "published_at": row.get("published_at"),
                "relevance": None,
                "source": "gnews",
            }
            for row in raw_rows
            if row.get("title") and row.get("url")
        ]
        if rows:
            return rows, []
        return [], ["gnews: empty"]
    except Exception as exc:
        return [], [f"gnews: provider_error:{type(exc).__name__}"]


def _news_from_brave(query):
    """Return structured Brave news rows and provider-local diagnostics."""
    from tg_bot.tools.fetch import http_get
    from urllib.parse import quote_plus

    if not BRAVE_KEY:
        return [], ["brave: unavailable"]
    try:
        raw = http_get(
            "https://api.search.brave.com/res/v1/news/search"
            f"?q={quote_plus(query)}&count=9&freshness=pd",
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_KEY},
        )
        data = json.loads(raw) if raw else {}
        items = data.get("results", [])
        valid_items, diagnostics = _validated_news_items(items, "brave")
        rows = [
            {
                "title": item.get("title", ""),
                "summary": item.get("description", ""),
                "url": item.get("url", ""),
                "published_at": item.get("page_age") or item.get("age") or item.get("published"),
                "relevance": item.get("score"),
                "source": "brave",
            }
            for item in valid_items
            if item.get("title") and item.get("url")
        ]
        if items:
            inc_quota("brave")
            return rows, diagnostics
        return [], ["brave: empty"]
    except Exception as exc:
        return [], [f"brave: provider_error:{type(exc).__name__}"]


def _news_from_tavily(query):
    """Return structured Tavily news rows and provider-local diagnostics."""
    if not TAVILY_KEYS:
        return [], ["tavily: unavailable"]
    try:
        data = _tavily_request("https://api.tavily.com/search", {
            "query": query,
            "search_depth": "basic",
            "topic": "news",
            "time_range": "day",
            "max_results": 8,
        }) or {}
        items = data.get("results", [])
        valid_items, diagnostics = _validated_news_items(items, "tavily")
        rows = [
            {
                "title": item.get("title", ""),
                "summary": item.get("content", ""),
                "url": item.get("url", ""),
                "published_at": item.get("published_date"),
                "relevance": item.get("score"),
                "source": "tavily",
            }
            for item in valid_items
            if item.get("title") and item.get("url")
        ]
        if items:
            return rows, diagnostics
        return [], ["tavily: empty"]
    except Exception as exc:
        return [], [f"tavily: provider_error:{type(exc).__name__}"]


def _news_from_serper(query):
    """Return structured Serper news rows while preserving key rotation."""
    import tg_bot.config as _cfg

    if not _cfg.SERPER_KEYS:
        return [], ["serper: unavailable"]
    diagnostics = []
    endpoint = "https://google.serper.dev/news"
    keys = tuple(_cfg.SERPER_KEYS)
    start_index, _start_key = _reserve_serper_key()
    for attempt in range(len(keys)):
        key_index = (start_index + attempt) % len(keys)
        key = keys[key_index]
        try:
            body = json.dumps({"q": query, "num": 8}).encode()
            req = Request(endpoint, data=body, headers={"Content-Type": "application/json", "X-API-KEY": key})
            with urlopen(req, context=_ctx, timeout=15) as response:
                data = json.loads(response.read())
            items = data.get("news", [])
            valid_items, response_diagnostics = _validated_news_items(items, "serper")
            rows = [
                {
                    "title": item.get("title", ""),
                    "summary": item.get("snippet", ""),
                    "url": item.get("link", ""),
                    "published_at": item.get("date"),
                    "relevance": item.get("score"),
                    "source": "serper",
                }
                for item in valid_items
                if item.get("title") and item.get("link")
            ]
            if items:
                diagnostics.extend(response_diagnostics)
                with _cfg._provider_rotation_lock:
                    _cfg._serper_idx = key_index
                    _cfg.SERPER_KEY = key
                inc_quota("serper")
                return rows, diagnostics
            diagnostics.append("serper: empty")
            break
        except Exception as exc:
            diagnostics.append(f"serper[{attempt}]: provider_error:{type(exc).__name__}")
            # The next loop iteration reserves the next key atomically.
    return [], diagnostics


def execute_news_candidates(query):
    """Return the first successful structured provider response.

    This legacy fallback boundary intentionally keeps its first-success
    behavior for existing callers.  Daily reports use the aggregate entrypoint
    below instead.
    """
    diagnostics = []
    for _name, provider in (
        ("brave", _news_from_brave),
        ("tavily", _news_from_tavily),
        ("serper", _news_from_serper),
    ):
        items, notes = provider(query)
        diagnostics.extend(notes)
        if items or not notes:
            return items, diagnostics
    return [], diagnostics


_NEWS_TRACKING_PARAMS = {
    "dclid", "fbclid", "gclid", "mc_cid", "mc_eid", "msclkid",
    "yclid",
}


def _canonical_news_url(url):
    """Normalize a news URL for stable cross-provider deduplication."""
    try:
        parsed = urlsplit(str(url or "").strip())
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if scheme not in {"http", "https"} or not host:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = (parsed.path or "").rstrip("/")
    query = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not name.lower().startswith("utm_")
        and name.lower() not in _NEWS_TRACKING_PARAMS
    ]
    return urlunsplit((scheme, host, path, urlencode(query, doseq=True), ""))


def execute_news_candidates_all(query, providers=None):
    """Aggregate every news provider for daily reports in stable order."""
    use_default = providers is None
    if use_default:
        providers = (
            ("gnews", _news_from_gnews),
            ("brave", _news_from_brave),
            ("tavily", _news_from_tavily),
            ("serper", _news_from_serper),
        )
    merged = []
    diagnostics = []
    seen = set()
    for name, provider in providers:
        try:
            items, notes = provider(query)
            diagnostics.extend(notes or [])
            malformed = False
            for item in items or []:
                if not isinstance(item, Mapping):
                    malformed = True
                    continue
                key = _canonical_news_url(item.get("url", ""))
                if key:
                    if key in seen:
                        continue
                    seen.add(key)
                merged.append(item)
            if malformed:
                diagnostics.append(f"{name}: provider_error:TypeError")
        except Exception as exc:
            diagnostics.append(f"{name}: provider_error:{type(exc).__name__}")
        # 默认路径：免费 gnews 已够 12 条时，跳过其后的付费提供方
        if use_default and name == "gnews" and len(merged) >= 12:
            diagnostics.append("gnews: enough_skip_paid")
            break
    return merged, diagnostics


def _web_from_brave(query):
    """返回 Brave 通用网页搜索的结构化结果与提供方诊断。"""
    from tg_bot.tools.fetch import http_get
    from urllib.parse import quote_plus

    if not BRAVE_KEY:
        return [], ["brave: unavailable"]
    try:
        raw = http_get(
            "https://api.search.brave.com/res/v1/web/search"
            f"?q={quote_plus(query)}&count=10",
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_KEY},
        )
        data = json.loads(raw) if raw else {}
        items = data.get("web", {}).get("results", [])
        valid_items, diagnostics = _validated_news_items(items, "brave")
        rows = [
            {
                "title": item.get("title", ""),
                "summary": item.get("description", ""),
                "url": item.get("url", ""),
                "published_at": item.get("page_age") or item.get("age"),
                "source": "brave",
            }
            for item in valid_items
            if item.get("title") and item.get("url")
        ]
        if items:
            inc_quota("brave")
            return rows, diagnostics
        return [], ["brave: empty"]
    except Exception as exc:
        return [], [f"brave: provider_error:{type(exc).__name__}"]


def _web_from_serper(query):
    """返回 Serper 通用网页搜索的结构化结果，保留 key 轮换逻辑。"""
    import tg_bot.config as _cfg

    if not _cfg.SERPER_KEYS:
        return [], ["serper: unavailable"]
    diagnostics = []
    endpoint = "https://google.serper.dev/search"
    keys = tuple(_cfg.SERPER_KEYS)
    start_index, _start_key = _reserve_serper_key()
    for attempt in range(len(keys)):
        key_index = (start_index + attempt) % len(keys)
        key = keys[key_index]
        try:
            body = json.dumps({"q": query, "num": 10}).encode()
            req = Request(endpoint, data=body, headers={"Content-Type": "application/json", "X-API-KEY": key})
            with urlopen(req, context=_ctx, timeout=15) as response:
                data = json.loads(response.read())
            items = data.get("organic", [])
            valid_items, response_diagnostics = _validated_news_items(items, "serper")
            rows = [
                {
                    "title": item.get("title", ""),
                    "summary": item.get("snippet", ""),
                    "url": item.get("link", ""),
                    "published_at": item.get("date"),
                    "source": "serper",
                }
                for item in valid_items
                if item.get("title") and item.get("link")
            ]
            if items:
                diagnostics.extend(response_diagnostics)
                with _cfg._provider_rotation_lock:
                    _cfg._serper_idx = key_index
                    _cfg.SERPER_KEY = key
                inc_quota("serper")
                return rows, diagnostics
            diagnostics.append("serper: empty")
            break
        except Exception as exc:
            diagnostics.append(f"serper[{attempt}]: provider_error:{type(exc).__name__}")
            # The next loop iteration reserves the next key atomically.
    return [], diagnostics


def _web_from_tavily(query):
    """返回 Tavily 通用网页搜索的结构化结果与提供方诊断。"""
    if not TAVILY_KEYS:
        return [], ["tavily: unavailable"]
    try:
        data = _tavily_request("https://api.tavily.com/search", {
            "query": query,
            "search_depth": "basic",
            "topic": "general",
            "max_results": 10,
        }) or {}
        items = data.get("results", [])
        valid_items, diagnostics = _validated_news_items(items, "tavily")
        rows = [
            {
                "title": item.get("title", ""),
                "summary": item.get("content", ""),
                "url": item.get("url", ""),
                "published_at": item.get("published_date"),
                "source": "tavily",
            }
            for item in valid_items
            if item.get("title") and item.get("url")
        ]
        if items:
            return rows, diagnostics
        return [], ["tavily: empty"]
    except Exception as exc:
        return [], [f"tavily: provider_error:{type(exc).__name__}"]


def web_candidates_all(query, providers=None):
    """聚合通用网页搜索的多个提供方，结果顺序稳定、按 URL 去重。"""
    if providers is None:
        providers = (
            ("brave", _web_from_brave),
            ("tavily", _web_from_tavily),
            ("serper", _web_from_serper),
        )
    merged = []
    diagnostics = []
    seen = set()
    for name, provider in providers:
        try:
            items, notes = provider(query)
            diagnostics.extend(notes or [])
            malformed = False
            for item in items or []:
                if not isinstance(item, Mapping):
                    malformed = True
                    continue
                key = _canonical_news_url(item.get("url", ""))
                if key:
                    if key in seen:
                        continue
                    seen.add(key)
                merged.append(item)
            if malformed:
                diagnostics.append(f"{name}: provider_error:TypeError")
        except Exception as exc:
            diagnostics.append(f"{name}: provider_error:{type(exc).__name__}")
    return merged, diagnostics


def _tavily_search_fallback(query, search_type):
    """搜索降级：Tavily → Serper"""
    log.info(f"🔍 Tavily 降级 [{search_type}]: {query}")
    try:
        payload = {
            "query": query,
            "search_depth": "basic",
            "topic": search_type,
            "max_results": 5,
            "days": 3,
        }
        d = _tavily_request("https://api.tavily.com/search", payload)
        if not d:
            return _serper_fallback(query, search_type)
        items = d.get("results", [])
        if not items:
            return _serper_fallback(query, search_type)
        items = sorted(items, key=lambda it: _preferred_rank(it.get("url", "")))
        lines = []
        for it in items[:5]:
            title    = it.get("title", "")
            content  = (it.get("content","") or it.get("snippet",""))[:200]
            url_     = it.get("url", "")
            pub      = it.get("published_date","")[:10] if it.get("published_date") else ""
            # 日期用独立行：标题行里的 [日期] 会被条目解析器剥掉丢失
            date_line = f"  📅 {pub}\n" if pub else ""
            lines.append(f"• {title}\n  {content}\n{date_line}  {url_}")
        return "\n\n".join(lines)
    except Exception as e:
        log.warning(f"Tavily 降级搜索异常: {e}")
        return _serper_fallback(query, search_type)

def _serper_fallback(query, search_type):
    """web_search 的第三备用：Serper.dev（自动回退用，不消耗 serper 配额）"""
    return _execute_serper(query, search_type)

def _execute_serper(query, search_type):
    """Serper.dev 实际执行函数（被备用链和独立工具共用）"""
    import tg_bot.config as _cfg
    if not _cfg.SERPER_KEYS:
        return "Serper 未配置"
    log.info(f"🔍 Serper [{search_type}]: {query}")
    endpoint = "https://google.serper.dev/news" if search_type == "news" else "https://google.serper.dev/search"
    payload = {"q": query, "num": 5}
    last_error = None
    for _attempt in range(len(_cfg.SERPER_KEYS)):
        key_index, key = _reserve_serper_key()
        if not key:
            break
        try:
            body = json.dumps(payload).encode()
            req = Request(endpoint, data=body, headers={
                "Content-Type": "application/json",
                "X-API-KEY": key,
            })
            with urlopen(req, context=_ctx, timeout=15) as response:
                data = json.loads(response.read())
            inc_quota("serper")
            items = data.get("news", data.get("organic", []))
            if not items:
                return "Serper 未找到相关结果。"
            items = sorted(items, key=lambda it: _preferred_rank(it.get("link", "")))
            lines = []
            for item in items[:5]:
                title = item.get("title", "")
                snippet = item.get("snippet", "")[:200]
                link = item.get("link", "")
                date = item.get("date", "")
                # 日期用独立行：标题行里的 [日期] 会被条目解析器剥掉丢失
                date_line = f"  📅 {date}\n" if date else ""
                lines.append(f"• {title}\n  {snippet}\n{date_line}  {link}")
            return "\n\n".join(lines)
        except HTTPError as exc:
            if getattr(exc, "code", None) == 400:
                return f"Serper 调用失败: HTTP 400 ({query[:40]})"
            last_error = exc
            log.warning(f"Serper key {key_index} 失败: {exc}，切换下一个 key 重试")
        except Exception as exc:
            last_error = exc
            log.warning(f"Serper key {key_index} 失败: {exc}，切换下一个 key 重试")
    if last_error is not None:
        return f"Serper 全部 key 失败: {type(last_error).__name__}"
    return "Serper 全部 key 失败: 未配置可用 key"
