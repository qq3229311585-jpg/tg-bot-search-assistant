#!/usr/bin/env python3
"""tools/fetch.py — 内容抓取、缓存读取相关工具函数"""

import http.client, ipaddress, json, re, os, logging, socket
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urljoin, urlsplit

from tg_bot.config import (
    FETCH_REMOTE_EXTRACT,
    SOURCES_DIR,
    _ctx,
)
from tg_bot.storage import load_today_index, update_today_index

log = logging.getLogger(__name__)


# ── HTTP 辅助 ─────────────────────────────────────────────────────────
def http_get(url, headers=None, timeout=15):
    h = {"User-Agent": "Mozilla/5.0"}
    if headers: h.update(headers)
    try:
        with urlopen(Request(url, headers=h), context=_ctx, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"GET err: {e}")
        return ""

def http_post(url, payload, headers=None, timeout=50):
    body = json.dumps(payload, ensure_ascii=False).encode()
    h = {"Content-Type": "application/json"}
    if headers: h.update(headers)
    try:
        with urlopen(Request(url, data=body, headers=h), context=_ctx, timeout=timeout) as r:
            return json.loads(r.read())
    except HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:800]
        except Exception:
            err_body = ""
        log.warning(f"POST err: {e} body={err_body}")
        return None
    except Exception as e:
        log.warning(f"POST err: {e}")
        return None


# ── 付费墙域名列表（fetch 失败时额外尝试 12ft.io 绕过） ────────────────
_PAYWALL_DOMAINS = {
    "ft.com", "economist.com", "nytimes.com", "wsj.com",
    "bloomberg.com", "theatlantic.com", "theinformation.com",
    "wired.com", "hbr.org", "newyorker.com",
    "foreignpolicy.com", "technologyreview.com",
}


def _is_public_ip(value):
    """Return whether an address is safe to contact from the fetch worker."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        not address.is_global
        or
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


def _public_addresses(hostname, port):
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            if item[4]
        }
    except (OSError, socket.gaierror, ValueError):
        return ()
    if not addresses or not all(_is_public_ip(address) for address in addresses):
        return ()
    return tuple(sorted(addresses))


def _parse_fetch_target(url):
    if not isinstance(url, str) or len(url) > 4096:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        return None
    try:
        explicit_port = parsed.port
        port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    return parsed, hostname, port


def validate_fetch_url(url):
    """Validate an article URL before handing it to a remote extractor.

    Fetch URLs originate from search/provider results, so this is deliberately
    fail-closed: only HTTP(S) URLs with a public, DNS-resolved destination are
    accepted.  This blocks locally resolved localhost, cloud metadata and
    private-network targets before the request.  It is intentionally paired
    with local fetching and redirect validation; a remote extraction provider
    is opt-in because its own DNS/redirect policy cannot be controlled here.
    """
    target = _parse_fetch_target(url)
    if target is None:
        return False
    parsed, hostname, port = target
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return _is_public_ip(str(literal))
    return bool(_public_addresses(hostname, port))


def _safe_local_fetch(url, timeout=15):
    """Fetch locally with a resolved public IP pinned for each connection."""
    current_url = url
    for _redirect in range(6):
        target = _parse_fetch_target(current_url)
        if target is None:
            raise ValueError("request or redirect target is not a public HTTP(S) URL")
        parsed, hostname, port = target
        addresses = _public_addresses(hostname, port)
        if not addresses:
            raise ValueError("target resolved to a non-public address")
        sock = socket.create_connection((addresses[0], port), timeout=timeout)
        if parsed.scheme.lower() == "https":
            sock = _ctx.wrap_socket(sock, server_hostname=hostname)
            connection = http.client.HTTPSConnection(hostname, port, context=_ctx, timeout=timeout)
        else:
            connection = http.client.HTTPConnection(hostname, port, timeout=timeout)
        connection.sock = sock
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            connection.request(
                "GET", path,
                headers={"Host": parsed.netloc, "User-Agent": "Mozilla/5.0", "Accept": "text/html,text/plain;q=0.9"},
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                response.read(4096)
                if not location:
                    raise ValueError("redirect response has no Location")
                current_url = urljoin(current_url, location)
                continue
            if response.status >= 400:
                raise ValueError(f"HTTP {response.status}")
            return response.read(2_000_000).decode("utf-8", errors="replace")
        finally:
            connection.close()
    raise ValueError("too many redirects")


def _extract_local_text(raw):
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', raw or '', flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _try_12ft(url):
    """尝试用 12ft.io 代理绕过付费墙，成功返回正文字符串，失败返回 None"""
    proxy_url = f"https://12ft.io/proxy?q={quote(url, safe='')}"
    try:
        raw = http_get(proxy_url, timeout=15)
        if not raw:
            return None
        # 去掉 HTML 标签提取纯文本
        text = re.sub(r'<style[^>]*>.*?</style>', ' ', raw, flags=re.DOTALL)
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 500:
            log.info(f"✅ 12ft.io 绕过成功，获取 {len(text)} 字符：{url[:60]}")
            return f"[正文来源（12ft代理）：{url}]\n\n{text[:8000]}"
        return None
    except Exception as e:
        log.debug(f"12ft.io 失败: {e}")
        return None


def execute_fetch_content(url):
    """
    本地抓取正文，并校验每次重定向的目标。

    Tavily/12ft 等远端提取服务默认关闭，因为本地 DNS 校验无法约束
    远端服务随后发生的 DNS 重解析或重定向。只有显式开启
    ``FETCH_REMOTE_EXTRACT=true`` 才会把已校验 URL 交给这些服务。
    """
    if not validate_fetch_url(url):
        log.warning("拒绝抓取不安全 URL: %s", url)
        return f"正文抓取失败（URL 不安全）：{url}"
    log.info(f"📄 local extract: {url}")
    # 判断是否付费墙域名
    try:
        _domain = url.split("/")[2].lstrip("www.")
    except Exception:
        _domain = ""
    _is_paywall = any(
        _domain == d or _domain.endswith("." + d)
        for d in _PAYWALL_DOMAINS
    )

    tavily_text = ""
    local_text = ""
    try:
        local_text = _extract_local_text(_safe_local_fetch(url))
    except Exception as e:
        log.warning(f"本地正文抓取异常: {e}")

    if len(local_text) > 300:
        return f"[正文来源：{url}]\n\n{local_text[:8000]}"

    if FETCH_REMOTE_EXTRACT:
        from tg_bot.tools.search import _tavily_request
        try:
            d = _tavily_request("https://api.tavily.com/extract", {"urls": [url]})
            if d:
                results = d.get("results", [])
                if results:
                    raw = results[0].get("raw_content", "") or results[0].get("content", "")
                    tavily_text = (raw or "").strip()
        except Exception as e:
            log.warning(f"Tavily extract 异常: {e}")

    # 内容够用，直接返回
    if len(tavily_text) > 300:
        return f"[正文来源：{url}]\n\n{tavily_text[:8000]}"

    # 内容过短或失败 + 付费墙，尝试 12ft.io
    if _is_paywall and FETCH_REMOTE_EXTRACT:
        log.info(f"🔓 Tavily 内容不足（{len(tavily_text)}字），尝试 12ft.io：{url[:60]}")
        bypass = _try_12ft(url)
        if bypass:
            return bypass

    # 有一点内容就返回，完全没有才报失败
    if tavily_text:
        return f"[正文来源：{url}]\n\n{tavily_text[:8000]}"
    if local_text:
        return f"[正文来源：{url}]\n\n{local_text[:8000]}"
    return f"正文抓取失败（所有方式均不可用）：{url}"


def execute_read_cache(ids, level="snippet"):
    """read_today_cache 工具的实际执行：按 ID 读取今日缓存的简介或正文"""
    day     = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    day_dir = os.path.join(SOURCES_DIR, day)
    id_set  = set(ids)
    found   = {}
    try:
        for fn in sorted(os.listdir(day_dir)):
            if not fn.endswith(".json") or fn == "index.json":
                continue
            data = json.loads(open(os.path.join(day_dir, fn), encoding="utf-8").read())
            for entry in (data.get("results") or data.get("sources") or []):
                if entry.get("id") in id_set:
                    found[entry["id"]] = entry
            if len(found) == len(id_set):
                break
    except Exception as e:
        return f"缓存读取失败: {e}"
    if not found:
        return "未找到指定 ID 的缓存记录"
    out = []
    for rid in ids:
        e = found.get(rid)
        if not e:
            out.append({"id": rid, "error": "未找到"})
            continue
        r = {
            "id":      rid,
            "title":   e.get("title", ""),
            "url":     e.get("url", ""),
            "snippet": e.get("snippet", ""),
        }
        if level == "full":
            fc = e.get("full_content")
            if fc:
                r["full_content"] = fc[:8000]
            else:
                r["note"] = "该页面未曾抓取原文，可调用 fetch_content 工具获取"
        out.append(r)
    return json.dumps(out, ensure_ascii=False, indent=2)
