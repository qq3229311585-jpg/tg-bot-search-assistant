#!/usr/bin/env python3
"""tools/search.py — 搜索相关工具函数"""

import json, logging
from urllib.error import HTTPError
from urllib.request import urlopen, Request

from tg_bot.config import (
    BRAVE_KEY, TAVILY_KEYS, SERPER_KEYS, SERPER_KEY,
    _ctx,
    _next_serper_key,
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
    import tg_bot.config as _cfg
    for attempt in range(len(TAVILY_KEYS)):
        key = TAVILY_KEYS[_cfg._tavily_idx % len(TAVILY_KEYS)]
        payload["api_key"] = key
        try:
            body = json.dumps(payload).encode()
            req  = Request(endpoint, data=body,
                           headers={"Content-Type": "application/json"})
            with urlopen(req, context=_ctx, timeout=25) as r:
                data = json.loads(r.read())
            # 成功，下次从下一个 key 开始（均匀分摊）
            inc_quota(f"tavily_{_cfg._tavily_idx % len(TAVILY_KEYS)}")
            _cfg._tavily_idx += 1
            return data
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower() or "limit" in err.lower():
                log.warning(f"Tavily key[{_cfg._tavily_idx % len(TAVILY_KEYS)}] 超限，切换下一个")
                _cfg._tavily_idx += 1
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
            lines.append(f"• {r.get('title','')}\n"
                         f"  {r.get('description','')[:200]}\n"
                         f"  {r.get('url','')}")
        return "\n\n".join(lines)
    except Exception as e:
        log.warning(f"execute_search Brave 异常: {e}")
        return _tavily_search_fallback(query, search_type)

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
            date_tag = f" [{pub}]" if pub else ""
            lines.append(f"• {title}{date_tag}\n  {content}\n  {url_}")
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
    if not _cfg.SERPER_KEYS or not _cfg.SERPER_KEY:
        return "Serper 未配置"
    log.info(f"🔍 Serper [{search_type}]: {query}")
    try:
        endpoint = "https://google.serper.dev/news" if search_type == "news" else "https://google.serper.dev/search"
        payload  = {"q": query, "num": 5}
        body     = json.dumps(payload).encode()
        req      = Request(endpoint, data=body, headers={
            "Content-Type": "application/json",
            "X-API-KEY": _cfg.SERPER_KEY
        })
        with urlopen(req, context=_ctx, timeout=15) as r:
            d = json.loads(r.read())
        inc_quota("serper")
        items = d.get("news", d.get("organic", []))
        if not items:
            return "Serper 未找到相关结果。"
        items = sorted(items, key=lambda it: _preferred_rank(it.get("link", "")))
        lines = []
        for it in items[:5]:
            title    = it.get("title", "")
            snippet  = it.get("snippet", "")[:200]
            link     = it.get("link", "")
            date     = it.get("date", "")
            date_tag = f" [{date}]" if date else ""
            lines.append(f"• {title}{date_tag}\n  {snippet}\n  {link}")
        return "\n\n".join(lines)
    except HTTPError as e:
        if getattr(e, "code", None) == 400:
            return f"Serper 调用失败: HTTP 400 ({query[:40]})"
        log.warning(f"Serper key {_cfg._serper_idx} 失败: {e}，切换下一个 key 重试")
        _next_serper_key()
        try:
            endpoint = "https://google.serper.dev/news" if search_type == "news" else "https://google.serper.dev/search"
            payload  = {"q": query, "num": 5}
            body     = json.dumps(payload).encode()
            req2 = Request(endpoint, data=body, headers={
                "Content-Type": "application/json",
                "X-API-KEY": _cfg.SERPER_KEY
            })
            with urlopen(req2, context=_ctx, timeout=15) as r2:
                d2 = json.loads(r2.read())
            inc_quota("serper")
            items2 = d2.get("news", d2.get("organic", []))
            if not items2:
                return "Serper 未找到相关结果。"
            items2 = sorted(items2, key=lambda it: _preferred_rank(it.get("link", "")))
            lines2 = []
            for it in items2[:5]:
                title   = it.get("title", "")
                snippet = it.get("snippet", "")[:200]
                link    = it.get("link", "")
                date    = it.get("date", "")
                date_tag = f" [{date}]" if date else ""
                lines2.append(f"• {title}{date_tag}\n  {snippet}\n  {link}")
            return "\n\n".join(lines2)
        except Exception as e2:
            return f"Serper 全部 key 失败: {e2}"
    except Exception as e:
        log.warning(f"Serper key {_cfg._serper_idx} 失败: {e}，切换下一个 key 重试")
        _next_serper_key()
        try:
            endpoint = "https://google.serper.dev/news" if search_type == "news" else "https://google.serper.dev/search"
            payload  = {"q": query, "num": 5}
            body     = json.dumps(payload).encode()
            req2 = Request(endpoint, data=body, headers={
                "Content-Type": "application/json",
                "X-API-KEY": _cfg.SERPER_KEY
            })
            with urlopen(req2, context=_ctx, timeout=15) as r2:
                d2 = json.loads(r2.read())
            inc_quota("serper")
            items2 = d2.get("news", d2.get("organic", []))
            if not items2:
                return "Serper 未找到相关结果。"
            items2 = sorted(items2, key=lambda it: _preferred_rank(it.get("link", "")))
            lines2 = []
            for it in items2[:5]:
                title   = it.get("title", "")
                snippet = it.get("snippet", "")[:200]
                link    = it.get("link", "")
                date    = it.get("date", "")
                date_tag = f" [{date}]" if date else ""
                lines2.append(f"• {title}{date_tag}\n  {snippet}\n  {link}")
            return "\n\n".join(lines2)
        except Exception as e2:
            return f"Serper 全部 key 失败: {e2}"
