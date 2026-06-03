#!/usr/bin/env python3
"""tools/native.py — 本地/原生 API 工具函数（天气、VPS流量、GitHub热榜、API余额等）"""

import json, re, logging, subprocess
import time as _time
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tg_bot.config import (
    DEEPSEEK_KEYS, DEEPSEEK_VERIFY_KEYS,
    TAVILY_KEYS, SERPER_KEYS,
    BRAVE_KEY,
    ANYANG_LAT, ANYANG_LON, WMO_ZH,
    QUOTA_FILE, API_FREE_LIMITS,
    _ctx,
)
from tg_bot.storage import inc_quota

log = logging.getLogger(__name__)

_WIKI_CACHE = {}
_WIKI_CACHE_TTL = 600


def _wiki_cache_get(key):
    if key in _WIKI_CACHE:
        ts, val = _WIKI_CACHE[key]
        if _time.time() - ts < _WIKI_CACHE_TTL:
            return val
    return None


def _wiki_cache_set(key, val):
    _WIKI_CACHE[key] = (_time.time(), val)


def execute_weather():
    from tg_bot.tools.fetch import http_get
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={ANYANG_LAT}&longitude={ANYANG_LON}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            f"weather_code,precipitation,wind_speed_10m"
            f"&hourly=temperature_2m,precipitation_probability,weather_code"
            f"&daily=temperature_2m_max,temperature_2m_min,"
            f"precipitation_probability_max,uv_index_max"
            f"&timezone=Asia%2FShanghai&forecast_days=3"
        )
        d     = json.loads(http_get(url))
        cur   = d["current"]
        daily = d["daily"]
        hourly = d.get("hourly", {})

        code  = cur.get("weather_code", 0)
        desc  = WMO_ZH.get(code, f"代码{code}")
        tmin  = daily["temperature_2m_min"][0]
        tmax  = daily["temperature_2m_max"][0]
        feel  = cur["apparent_temperature"]
        humi  = cur["relative_humidity_2m"]
        wind  = cur.get("wind_speed_10m", 0)
        uv    = daily.get("uv_index_max", [0])[0]
        rain_max = daily["precipitation_probability_max"][0]

        tips = []
        if rain_max >= 50: tips.append("☔带伞")
        if uv >= 6:        tips.append("🧴防晒")
        tip_str = "  " + "  ".join(tips) if tips else ""

        lines = [
            f"📍 安阳当前天气",
            f"  {desc}  {cur['temperature_2m']:.0f}°C（体感{feel:.0f}°）  湿度{humi}%  风速{wind:.0f}km/h",
            f"  今日 {tmin:.0f}～{tmax:.0f}°C  降雨概率{rain_max}%  UV指数{uv:.0f}{tip_str}",
        ]

        # 4时段预报
        if hourly.get("time"):
            times  = hourly["time"]
            temps  = hourly["temperature_2m"]
            rains  = hourly["precipitation_probability"]
            wcodes = hourly["weather_code"]
            slots  = [("🌅","早",6),("☀️","午",12),("🌆","晚",18),("🌙","夜",22)]
            slot_lines = []
            for emoji, label, h in slots:
                idx = next((i for i, t in enumerate(times) if f"T{h:02d}:00" in t), None)
                if idx is not None:
                    t     = temps[idx]
                    r     = rains[idx]
                    wdesc = WMO_ZH.get(wcodes[idx], "")
                    rain_tag = " ☔" if r >= 30 else ""
                    slot_lines.append(f"  {emoji}{label} {t:.0f}°C {wdesc}{rain_tag}")
            if slot_lines:
                lines.append("\n".join(slot_lines))

        # 未来2天
        weekdays = ["周一","周二","周三","周四","周五","周六","周日"]
        from datetime import datetime as _dt
        for i in range(1, min(3, len(daily["temperature_2m_max"]))):
            date_str = daily.get("time", [None]*3)[i] or ""
            wday = ""
            if date_str:
                try: wday = weekdays[_dt.strptime(date_str, "%Y-%m-%d").weekday()]
                except: pass
            tlo = daily["temperature_2m_min"][i]
            thi = daily["temperature_2m_max"][i]
            rp  = daily["precipitation_probability_max"][i]
            wc  = WMO_ZH.get(daily.get("weather_code", [0]*3)[i] if "weather_code" in daily else 0, "")
            lines.append(f"  {wday}({date_str})  {tlo:.0f}～{thi:.0f}°C  {wc}  降雨{rp}%")

        return "\n".join(lines)
    except Exception as e:
        return f"天气查询失败: {e}"


def execute_vps_traffic():
    """查询 VPS 网络流量状态，综合多个来源"""
    sections = []

    # 1. vnstat 月度/日度流量统计
    try:
        r = subprocess.run(["vnstat", "--json"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            d = json.loads(r.stdout)
            ifaces = d.get("interfaces", [])
            if ifaces:
                iface = ifaces[0]
                name  = iface.get("name", "eth0")
                traffic = iface.get("traffic", {})

                def fmt_bytes(b):
                    if b >= 1e9:  return f"{b/1e9:.2f} GB"
                    if b >= 1e6:  return f"{b/1e6:.2f} MB"
                    if b >= 1e3:  return f"{b/1e3:.2f} KB"
                    return f"{b} B"

                lines = [f"📊 vnstat 流量统计（接口：{name}）"]
                # 本月
                month = (traffic.get("month") or [None])[-1]
                if month:
                    rx = month.get("rx", 0)
                    tx = month.get("tx", 0)
                    lines.append(f"  本月  ↓{fmt_bytes(rx)}  ↑{fmt_bytes(tx)}  共{fmt_bytes(rx+tx)}")
                # 今日
                day = (traffic.get("day") or [None])[-1]
                if day:
                    rx = day.get("rx", 0)
                    tx = day.get("tx", 0)
                    lines.append(f"  今日  ↓{fmt_bytes(rx)}  ↑{fmt_bytes(tx)}  共{fmt_bytes(rx+tx)}")
                # 近7天
                days = traffic.get("day") or []
                if len(days) >= 2:
                    rx7 = sum(d_.get("rx",0) for d_ in days[-7:])
                    tx7 = sum(d_.get("tx",0) for d_ in days[-7:])
                    lines.append(f"  近7天 ↓{fmt_bytes(rx7)}  ↑{fmt_bytes(tx7)}")
                sections.append("\n".join(lines))
    except FileNotFoundError:
        sections.append("⚠️ vnstat 未安装，仅显示实时接口数据")
    except Exception as e:
        sections.append(f"vnstat 查询失败: {e}")

    # 2. /proc/net/dev 实时接口数据
    try:
        with open("/proc/net/dev") as f:
            raw = f.read()
        lines = ["📡 当前网络接口（/proc/net/dev）"]
        for line in raw.strip().splitlines()[2:]:
            parts = line.split()
            if len(parts) < 10: continue
            iface = parts[0].rstrip(":")
            if iface in ("lo",): continue
            rx_b = int(parts[1])
            tx_b = int(parts[9])
            def fb(b):
                if b>=1e9: return f"{b/1e9:.2f}GB"
                if b>=1e6: return f"{b/1e6:.2f}MB"
                if b>=1e3: return f"{b/1e3:.2f}KB"
                return f"{b}B"
            lines.append(f"  {iface}  ↓{fb(rx_b)}  ↑{fb(tx_b)}（累计，自上次重启）")
        sections.append("\n".join(lines))
    except Exception as e:
        sections.append(f"/proc/net/dev 读取失败: {e}")

    # 3. 当前连接数
    try:
        r = subprocess.run(["ss", "-tn", "state", "established"],
                           capture_output=True, text=True, timeout=5)
        conn_count = max(0, len(r.stdout.strip().splitlines()) - 1)
        sections.append(f"🔗 当前 TCP 已建立连接数：{conn_count}")
    except Exception:
        pass

    # 4. 系统负载 & 内存（顺带）
    try:
        with open("/proc/loadavg") as f:
            la = f.read().split()
        with open("/proc/meminfo") as f:
            mem_lines = f.read().splitlines()
        total = free = 0
        for ml in mem_lines:
            if ml.startswith("MemTotal"):  total = int(ml.split()[1])
            if ml.startswith("MemAvailable"): free = int(ml.split()[1])
        used_pct = round((total - free) / total * 100) if total else 0
        sections.append(
            f"🖥  负载：{la[0]} {la[1]} {la[2]}  "
            f"内存：{(total-free)//1024}MB/{total//1024}MB（{used_pct}%）"
        )
    except Exception:
        pass

    return "\n\n".join(sections) if sections else "流量信息获取失败"


def execute_github_trending(language="", since="daily"):
    """
    直接抓取 github.com/trending 页面并解析，不依赖 Jina。
    返回格式化文本，供 bot 直接使用。
    """
    from tg_bot.tools.fetch import http_get
    lang_path = f"/{language}" if language else ""
    url = f"https://github.com/trending{lang_path}?since={since}"
    try:
        raw = http_get(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }, timeout=15)
        if not raw:
            return "GitHub 热榜暂时无法获取"
        articles = re.findall(r'<article[^>]*Box-row[^>]*>(.*?)</article>', raw, re.DOTALL)
        if not articles:
            return "GitHub 热榜暂时无法获取（页面结构已变）"
        lines = [f"📦 GitHub 今日热榜（Top {min(8, len(articles))}）"]
        for art in articles[:8]:
            # 仓库名
            nm = re.search(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', art)
            name = nm.group(1) if nm else "?"
            # 描述（my-1 类的 p）
            dm = re.search(r'<p[^>]*my-1[^>]*>(.*?)</p>', art, re.DOTALL)
            desc = re.sub(r'\s+', ' ', dm.group(1)).strip()[:60] if dm else ""
            # 语言
            lm = re.search(r'itemprop="programmingLanguage">\s*([^<]+)', art)
            lang_tag = lm.group(1).strip() if lm else ""
            # 今日新增星标
            tm = re.search(r'([\d,]+)\s*stars today', art)
            today = tm.group(1).replace(",", "") if tm else ""
            lang_str  = f" [{lang_tag}]" if lang_tag else ""
            today_str = f" +{today}★今日" if today else ""
            lines.append(f"  • {name}{lang_str}{today_str}")
            if desc:
                lines.append(f"    {desc}")
        log.info(f"📦 GitHub 热榜抓取成功：{len(articles)} 条")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"execute_github_trending 失败: {e}")
        return "GitHub 热榜暂时无法获取"


def execute_api_balance():
    """
    查询所有外部 API 的剩余额度/余额，返回格式化文本。
    DeepSeek 可作为工具调用，/balance 命令也直接调用。
    """
    import urllib.request as _ur
    import tg_bot.config as _cfg
    lines = []

    def _get(url, headers):
        try:
            req = _ur.Request(url, headers={**headers, "User-Agent": "Mozilla/5.0"})
            with _ur.urlopen(req, context=_ctx, timeout=10) as r:
                return json.loads(r.read())
        except Exception as e:
            return {"_err": str(e)}

    # ── DeepSeek（同一账号下多 key，只取余额不重复） ──────────────────
    ds_seen = {}   # balance → name
    for name, key in [
        ("写作", _cfg.DEEPSEEK_KEYS[0]), ("核查", _cfg.DEEPSEEK_VERIFY_KEYS[0])
    ]:
        d = _get("https://api.deepseek.com/user/balance",
                 {"Authorization": f"Bearer {key}"})
        if "_err" in d:
            ds_seen[name] = f"查询失败（{d['_err'][:40]}）"
            continue
        for bi in d.get("balance_infos", []):
            bal = bi.get("total_balance", "?")
            cur = bi.get("currency", "CNY")
            ds_seen[bal] = f"{bal} {cur}"
    if ds_seen:
        unique = list(dict.fromkeys(ds_seen.values()))
        lines.append("💳 DeepSeek\n  余额：" + " / ".join(unique))
    else:
        lines.append("💳 DeepSeek  查询失败")

    # ── Tavily ────────────────────────────────────────────────────────
    tv_lines = []
    for i, key in enumerate(_cfg.TAVILY_KEYS):
        d = _get("https://api.tavily.com/usage",
                 {"Authorization": f"Bearer {key}"})
        if "_err" in d or "error" in str(d).lower():
            tv_lines.append(f"  key-{i}：失效或查询失败")
            continue
        acc = d.get("account", d.get("key", {}))
        used  = acc.get("plan_usage", acc.get("usage", "?"))
        limit = acc.get("plan_limit", acc.get("limit", "?"))
        plan  = acc.get("current_plan", "")
        tv_lines.append(f"  key-{i}（{plan}）：{used} / {limit} 次已用")
    lines.append("🔍 Tavily\n" + "\n".join(tv_lines))

    # ── Serper ────────────────────────────────────────────────────────
    sp_lines = []
    for i, key in enumerate(_cfg.SERPER_KEYS):
        d = _get("https://google.serper.dev/account", {"X-API-KEY": key})
        if "_err" in d:
            sp_lines.append(f"  key-{i}：查询失败")
            continue
        bal = d.get("balance", "?")
        sp_lines.append(f"  key-{i}：剩余 {bal} 次")
    lines.append("🔎 Serper\n" + "\n".join(sp_lines))

    # ── Brave ─────────────────────────────────────────────────────────
    bd = _get("https://api.search.brave.com/res/v1/subscriptions/usage",
              {"Accept": "application/json",
               "X-Subscription-Token": _cfg.BRAVE_KEY})
    if "_err" in bd or not bd:
        # Brave 无余额 API，从本地 quota 读已用量
        try:
            import json as _json
            q = _json.loads(open(_cfg.QUOTA_FILE).read())
            used_brave = q.get("counts", {}).get("brave", 0)
            lines.append(f"🦁 Brave  本月已用 {used_brave} / {_cfg.API_FREE_LIMITS.get('brave', 1000)} 次（无余额接口）")
        except Exception:
            lines.append("🦁 Brave  无法查询")
    else:
        lines.append(f"🦁 Brave  {bd}")

    # ── 本地 quota 汇总 ───────────────────────────────────────────────
    try:
        import json as _json
        q = _json.loads(open(_cfg.QUOTA_FILE).read())
        counts = q.get("counts", {})
        month  = q.get("month", "")
        parts  = [f"{k}={v}" for k, v in counts.items()]
        lines.append(f"📊 本地计数（{month}）\n  " + "  ".join(parts))
    except Exception:
        pass

    return "\n\n".join(lines)


def validate_facts_sheet(text: str) -> bool:
    """检查采集AI输出是否符合事实清单格式规范。"""
    if "═══ 事实清单 ═══" not in text:
        return False
    if "═══ 清单结束 ═══" not in text:
        return False
    for section in ["【直接API来源】", "【搜索来源】", "【未获取到】"]:
        if section not in text:
            return False
    return True


# ── Wikipedia 查询执行（中英双语并行）────────────────────────────────
def _wiki_fetch_one(lang: str, query: str, chars: int = 3000) -> str:
    """查单语 Wikipedia，返回格式化文本，失败返回空字符串"""
    from urllib.parse import quote_plus
    base  = f"https://{lang}.wikipedia.org/w/api.php"
    import json as _json
    import re as _re
    clean = lambda s: _re.sub(r'<[^>]+>', '', s or "")

    cache_key = f"wiki:{lang}:{query}:{chars}"
    cached = _wiki_cache_get(cache_key)
    if cached is not None:
        return cached

    def _wiki_get_json(url):
        for attempt in range(3):
            try:
                req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(req, context=_ctx, timeout=15) as r:
                    return _json.loads(r.read().decode("utf-8", errors="replace"))
            except HTTPError as e:
                if e.code == 429 and attempt < 2:
                    wait = 1.5 * (2 ** attempt)
                    log.warning(f"Wikipedia 429，{wait:.1f}s 后重试（第 {attempt + 1} 次）")
                    _time.sleep(wait)
                    continue
                raise

    try:
        sd = _wiki_get_json(
            f"{base}?action=query&list=search&srsearch={quote_plus(query)}"
            f"&srlimit=3&format=json")
        hits = sd.get("query", {}).get("search", [])
        if not hits:
            _wiki_cache_set(cache_key, "")
            return ""
        title = hits[0]["title"]
        ed = _wiki_get_json(
            f"{base}?action=query&titles={quote_plus(title)}"
            f"&prop=extracts&explaintext=true&format=json")
        pages   = ed.get("query", {}).get("pages", {})
        extract = list(pages.values())[0].get("extract", "")[:chars]
        if not extract:
            _wiki_cache_set(cache_key, "")
            return ""
        label  = "中文Wikipedia" if lang == "zh" else "英文Wikipedia"
        result = f"【{label}】{title}:\n{extract}"
        if len(hits) > 1:
            others = "\n".join(
                f"  • {h['title']}: {clean(h.get('snippet',''))[:100]}"
                for h in hits[1:]
            )
            result += f"\n\n（其他候选：\n{others}）"
        _wiki_cache_set(cache_key, result)
        return result
    except Exception as e:
        log.debug(f"Wikipedia [{lang}] 查询失败: {e}")
        _wiki_cache_set(cache_key, "")
        return ""

def execute_wikipedia(query):
    log.info(f"📖 Wikipedia（双语）: {query}")
    has_cjk = bool(re.search(r'[一-鿿]', query))

    # 总是同时查中英两个版本，中文结果放前面（含正确汉字字形）
    if has_cjk:
        zh_res = _wiki_fetch_one("zh", query, chars=3000)
        en_res = _wiki_fetch_one("en", query, chars=2000)
    else:
        en_res = _wiki_fetch_one("en", query, chars=3000)
        # 英文查询也同步查中文（拼音/英文名在中文Wikipedia也有重定向）
        zh_res = _wiki_fetch_one("zh", query, chars=2000)

    parts = []
    if has_cjk:
        if zh_res: parts.append(zh_res)
        if en_res: parts.append(en_res)
    else:
        if en_res: parts.append(en_res)
        if zh_res: parts.append(zh_res)

    if not parts:
        return "Wikipedia 未找到相关词条。"

    note = ("（中文版含人名/地名/机构名的正确汉字字形，"
            "写中文时人名请以中文Wikipedia为准）\n\n"
            if zh_res and en_res else "")
    return note + "\n\n─────────────────\n\n".join(parts)


def execute_search_chat_history(keyword, limit=20):
    """在 daily_logs + chat_history.json 里搜索包含关键词的消息。"""
    import json as _json, os as _os
    from tg_bot.config import HISTORY_FILE, DAILY_LOGS_DIR

    kw_lower = keyword.lower()
    matches = []
    seen = set()

    try:
        if _os.path.isdir(DAILY_LOGS_DIR):
            for fname in sorted(_os.listdir(DAILY_LOGS_DIR)):
                if not fname.endswith(".jsonl"):
                    continue
                date_part = fname[:-6]
                with open(_os.path.join(DAILY_LOGS_DIR, fname), encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = _json.loads(line)
                        except Exception:
                            continue
                        content = item.get("content", "")
                        if kw_lower in content.lower():
                            ts = item.get("ts", "")
                            role = "用户" if item.get("role") == "user" else "Bot"
                            key = f"{date_part}_{ts}_{content[:30]}"
                            if key not in seen:
                                seen.add(key)
                                matches.append(f"[{date_part} {ts}] {role}：{content[:120]}")
    except Exception as e:
        log.debug(f"search_chat_history daily_logs: {e}")

    try:
        data = _json.loads(open(HISTORY_FILE, encoding="utf-8").read())
        for item in data:
            content = item.get("content", "")
            if kw_lower in content.lower():
                ts = item.get("ts", "")
                role = "用户" if item.get("role") == "user" else "Bot"
                entry = f"[{ts}] {role}：{content[:120]}"
                if entry not in matches:
                    matches.append(entry)
    except Exception:
        pass

    if not matches:
        return f"在所有对话历史中未找到包含「{keyword}」的消息。"
    return f"找到 {len(matches)} 条匹配（最多显示{limit}条）：\n\n" + "\n\n".join(matches[-limit:])
