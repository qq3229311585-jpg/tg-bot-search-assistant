#!/usr/bin/env python3
"""DeepSeek 官方 Search / Fetch API 封装(只用标准库)。

- ds_search_rows: 搜索,返回项目统一行格式 {url,title,snippet,published_at,source}
  并保留 authority / rerank_score 原始字段。
- ds_fetch: 抓取单个 URL 的 Markdown 正文,失败返回 ""。
- 网络访问通过参数注入的 post(url, payload, key) -> (status, text) 完成,便于测试。
- 任何异常、无 key、非 200 响应都返回空结果,永不抛异常。
- Key 来自环境变量 DEEPSEEK_KEY_0 / DEEPSEEK_KEY_1(缺失则该源直接跳过),按请求轮换。
"""

import datetime as _dt
import json
import os
import threading
import time
import urllib.error
import urllib.request

SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"
KEY_ENV = ("DEEPSEEK_KEY_0", "DEEPSEEK_KEY_1")
TIMEOUT = 20
QUERY_MAX = 128
COUNT_DEFAULT = 8
COUNT_MAX = 50
MIN_RERANK = 1.0
SUMMARY_MAX = 600
MIN_GAP = 0.25        # 账户 QPS=5,同进程请求间隔不小于 0.25 秒
RETRY_WAIT = 1.5      # 429 后等待再重试一次
_UA = "tg_bot-deepseek-tools/1.0"
_DAY_OFFSETS = {"day": 2, "week": 8, "month": 32}

_lock = threading.Lock()
_state = {"last": None, "calls": 0, "next_key": 0}


def _keys():
    out = []
    for name in KEY_ENV:
        value = (os.environ.get(name) or "").strip()
        if value:
            out.append(value)
    return out


def ds_available():
    """是否配置了至少一个 DeepSeek key。"""
    return bool(_keys())


def _pick_key():
    keys = _keys()
    if not keys:
        return None
    with _lock:
        index = _state["next_key"] % len(keys)
        _state["next_key"] += 1
    return keys[index]


def _throttle():
    """同进程内相邻两次请求至少间隔 MIN_GAP 秒(锁内等待,保证跨线程也生效)。"""
    with _lock:
        now = time.monotonic()
        last = _state["last"]
        if last is not None:
            wait = MIN_GAP - (now - last)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        _state["last"] = now


def _request(url, payload, post):
    """发送一次请求(429 时等待后重试一次),返回解析后的 dict;失败返回 None。"""
    key = _pick_key()
    if key is None:
        return None
    for attempt in range(2):
        _throttle()
        try:
            status, text = post(url, payload, key)
        except Exception:
            return None
        if status == 429 and attempt == 0:
            time.sleep(RETRY_WAIT)
            continue
        if status != 200:
            return None
        with _lock:
            _state["calls"] += 1
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None
    return None


def pop_call_count():
    """返回并清零自上次调用以来,返回 HTTP 200 的 DeepSeek 请求数(用于审计)。"""
    with _lock:
        n = _state["calls"]
        _state["calls"] = 0
    return n


def _date_operator(time_range, today=None):
    days = _DAY_OFFSETS.get(time_range)
    if days is None:
        return ""
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    return f" after:{(today - _dt.timedelta(days=days)).isoformat()}"


def build_query(query, time_range="any", today=None):
    """给查询追加 after: 时间操作符;超过 128 字符时截断原查询。"""
    q = str(query or "").strip()
    op = _date_operator(time_range, today)
    if op and len(q) + len(op) > QUERY_MAX:
        q = q[: QUERY_MAX - len(op)].rstrip()
    elif not op:
        q = q[:QUERY_MAX].rstrip()
    return (q + op).strip()


def ds_search_rows(query, time_range="any", *, post, count=COUNT_DEFAULT):
    """DeepSeek 搜索,返回统一行格式列表;任何失败返回 []。"""
    try:
        q = build_query(query, time_range)
        if not q:
            return []
        n = max(1, min(COUNT_MAX, int(count)))
        data = _request(SEARCH_URL, {"query": q, "count": n, "need_content": False}, post)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        rows = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or not title:
                continue
            score = item.get("rerank_score")
            numeric = isinstance(score, (int, float)) and not isinstance(score, bool)
            if numeric and score < MIN_RERANK:
                continue
            summary = str(item.get("summary") or "").strip()
            snippet = summary or str(item.get("snippet") or "").strip()
            row = {
                "url": url,
                "title": title,
                "snippet": snippet[:SUMMARY_MAX],
                "published_at": item.get("date"),
                "source": "deepseek",
            }
            if "authority" in item:
                row["authority"] = item.get("authority")
            if "rerank_score" in item:
                row["rerank_score"] = score
            rows.append(row)
        return rows
    except Exception:
        return []


def ds_fetch(url, *, post):
    """DeepSeek 抓取,返回 Markdown 正文;失败返回 ""。"""
    try:
        target = str(url or "").strip()
        if not target:
            return ""
        data = _request(FETCH_URL, {"url": target}, post)
        content = data.get("content") if isinstance(data, dict) else None
        return content.strip() if isinstance(content, str) else ""
    except Exception:
        return ""


def default_post(url, payload, key):
    """真实 HTTP POST(urllib,超时 20 秒)。返回 (状态码, 响应文本);HTTPError 也返回码与体。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "User-Agent": _UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        return exc.code, text
