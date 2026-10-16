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


def _geocode_city(city):
    """open-meteo 免费地理编码：城市名 → (纬度, 经度, 规范名, 时区名)；失败返回 None。"""
    from tg_bot.tools.fetch import http_get
    from urllib.parse import quote_plus
    try:
        # zh/en 索引各有盲区（zh 匹配中文城市名、en 匹配英文），双查合并
        results = []
        for lang in ("zh", "en"):
            try:
                raw = http_get(
                    f"https://geocoding-api.open-meteo.com/v1/search?name={quote_plus(city)}&count=5&language={lang}",
                    headers={"Accept": "application/json"}, timeout=10,
                )
                results.extend(
                    r for r in (json.loads(raw or "{}").get("results") or [])
                    if r.get("latitude") is not None
                )
            except Exception:
                continue
        if not results:
            return None
        # 同名地点取人口最大的（"东京"应命中东京都，而不是国内同名小镇）
        hit = max(results, key=lambda r: r.get("population") or 0)
        # 用户输入含中文就用用户的叫法显示（"武汉"而不是"Wuhan"）
        display = city if re.search(r"[一-鿿]", city) else (hit.get("name") or city)
        return hit["latitude"], hit["longitude"], display, hit.get("timezone") or ""
    except Exception as e:
        log.debug(f"geocode {city} 失败: {e}")
        return None


def execute_weather(city=""):
    from tg_bot.tools.fetch import http_get
    lat, lon, place = ANYANG_LAT, ANYANG_LON, "安阳"
    city = (city or "").strip()
    if city and city not in ("安阳", "安阳市"):
        located = _geocode_city(city)
        if located is None:
            return f"没找到城市「{city}」的位置信息，请确认城市名。"
        lat, lon, place, _tz = located
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            f"weather_code,precipitation,wind_speed_10m"
            f"&hourly=temperature_2m,precipitation_probability,weather_code"
            f"&daily=temperature_2m_max,temperature_2m_min,"
            f"precipitation_probability_max,uv_index_max"
            f"&timezone=Asia%2FShanghai&forecast_days=3"
        )
        d     = json.loads(http_get(url))
        daily = d["daily"]
        hourly = d.get("hourly", {})

        lines = [f"📍 {place}天气"]

        # 今天按早/中/晚/夜四个时段直接列，不再单独总述一次“今日”。
        if hourly.get("time"):
            times  = hourly["time"]
            temps  = hourly["temperature_2m"]
            rains  = hourly["precipitation_probability"]
            wcodes = hourly["weather_code"]
            slots  = [("早上", 6), ("中午", 12), ("晚上", 18), ("夜间", 22)]
            for label, h in slots:
                idx = next((i for i, t in enumerate(times) if f"T{h:02d}:00" in t), None)
                if idx is not None:
                    t     = temps[idx]
                    r     = rains[idx]
                    wdesc = WMO_ZH.get(wcodes[idx], "")
                    rain_tag = " ☔" if r >= 30 else ""
                    lines.append(f"{label} {t:.0f}°C {wdesc}{rain_tag}")

        # 空气质量（安阳在污染带，AQI 比 UV 实用）
        aqi_line = _air_quality_line(lat, lon)
        if aqi_line:
            lines.append(aqi_line)

        # 明天、后天各一行摘要，不再往后延伸。
        weekday_labels = ["明天", "后天"]
        for i in range(1, min(3, len(daily["temperature_2m_max"]))):
            tlo = daily["temperature_2m_min"][i]
            thi = daily["temperature_2m_max"][i]
            rp  = daily["precipitation_probability_max"][i]
            wc  = WMO_ZH.get(daily.get("weather_code", [0]*3)[i] if "weather_code" in daily else 0, "")
            lines.append(f"{weekday_labels[i - 1]} {wc} {tlo:.0f}~{thi:.0f}°C 降雨{rp}%")

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
        # /sponsors/xxx、/login 等站内链接会被宽泛的 href 正则误当仓库名抓走
        _non_repo_prefixes = {"sponsors", "login", "signup", "topics", "trending", "features", "site", "about"}
        lines = [f"📦 GitHub 今日热榜（Top {min(8, len(articles))}）"]
        count = 0
        for art in articles[:8]:
            name = ""
            for candidate in re.findall(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', art):
                if candidate.split("/")[0].lower() not in _non_repo_prefixes:
                    name = candidate
                    break
            if not name:
                continue
            # 描述（my-1 类的 p）
            dm = re.search(r'<p[^>]*my-1[^>]*>(.*?)</p>', art, re.DOTALL)
            import html as _html
            desc = _html.unescape(re.sub(r'\s+', ' ', dm.group(1))).strip()[:100] if dm else ""
            # 语言
            lm = re.search(r'itemprop="programmingLanguage">\s*([^<]+)', art)
            lang_tag = lm.group(1).strip() if lm else ""
            # 今日新增星标
            tm = re.search(r'([\d,]+)\s*stars today', art)
            today = tm.group(1).replace(",", "") if tm else ""
            # 名字加粗独立一行，语言/星数用 · 连接；条目之间空行，避免挤成一坨
            meta_parts = [part for part in (lang_tag, f"+{today}★" if today else "") if part]
            title_line = f"<b>{name}</b>"
            if meta_parts:
                title_line += " · " + " · ".join(meta_parts)
            if count:
                lines.append("")
            lines.append(title_line)
            if desc:
                lines.append(desc)
            count += 1
        log.info(f"📦 GitHub 热榜抓取成功：{count} 条")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"execute_github_trending 失败: {e}")
        return "GitHub 热榜暂时无法获取"


def execute_exchange_snapshot():
    """人民币兑美元/日元汇率快照（open.er-api.com，免费无 key）。"""
    from tg_bot.tools.fetch import http_get
    try:
        raw = http_get("https://open.er-api.com/v6/latest/USD",
                       headers={"Accept": "application/json"}, timeout=15)
        if not raw:
            return ""
        rates = (json.loads(raw).get("rates") or {})
        cny = rates.get("CNY")
        jpy = rates.get("JPY")
        if not cny:
            return ""
        parts = [f"1 USD = {cny:.4f} CNY"]
        if jpy:
            parts.append(f"100 JPY = {cny / jpy * 100:.4f} CNY")
        log.info(f"💱 汇率快照获取成功 CNY={cny}")
        return "\n".join(parts)
    except Exception as e:
        log.warning(f"execute_exchange_snapshot 失败: {e}")
        return ""


def execute_us_market_snapshot():
    """纳斯达克综合指数 / 标普500 快照（Yahoo Finance 公开 chart 接口，免费无 key）。"""
    from tg_bot.tools.fetch import http_get
    try:
        lines = []
        quote_date = None
        for symbol, label in (("%5EIXIC", "纳斯达克综合指数"), ("%5EGSPC", "标普500")):
            raw = http_get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            if not raw:
                continue
            meta = ((json.loads(raw).get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
            price = meta.get("regularMarketPrice")
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            if price is None or not prev:
                continue
            market_time = meta.get("regularMarketTime")
            if quote_date is None and market_time:
                try:
                    quote_date = datetime.fromtimestamp(
                        int(market_time), timezone(timedelta(hours=8))
                    ).date()
                except (TypeError, ValueError, OSError):
                    quote_date = None
            change = (price - prev) / prev * 100
            mark = f"  {'🟢+' if change >= 0 else '🔴'}{change:.2f}%"
            # 名字单独一行、点位+涨跌幅一行，避免手机窄屏把长行拆得七零八落。
            lines.append(f"{label}")
            lines.append(f"{price:,.2f}{mark}")
        # 周末/休市时行情停留在最后一个交易日，如实标注数据日期，避免误当实时价
        if lines and quote_date is not None:
            today = datetime.now(timezone(timedelta(hours=8))).date()
            if quote_date != today:
                lines.append(f"（{quote_date.month}月{quote_date.day}日收盘数据）")
        if lines:
            log.info("📈 美股指数快照获取成功")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"execute_us_market_snapshot 失败: {e}")
        return ""


def _steam_recommendations_total(appid):
    """查单款游戏的官方评价总数（口碑信号），失败返回 0。"""
    from tg_bot.tools.fetch import http_get
    try:
        raw = http_get(
            f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=cn&l=schinese&filters=recommendations",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if not raw:
            return 0
        data = (json.loads(raw).get(str(appid)) or {}).get("data") or {}
        return int((data.get("recommendations") or {}).get("total") or 0)
    except Exception:
        return 0


def execute_exchange_rate(base="USD", target="CNY", amount=0):
    """任意币种汇率查询/换算（open.er-api.com，免费无 key）。"""
    from tg_bot.tools.fetch import http_get
    base = (base or "USD").strip().upper()
    target = (target or "CNY").strip().upper()
    try:
        raw = http_get(f"https://open.er-api.com/v6/latest/{base}",
                       headers={"Accept": "application/json"}, timeout=15)
        data = json.loads(raw or "{}")
        rates = data.get("rates") or {}
        rate = rates.get(target)
        if not rate:
            if data.get("result") == "error":
                return f"汇率查询失败：不认识的货币代码「{base}」。请用 ISO 代码（如 USD/JPY/EUR）。"
            return f"汇率查询失败：找不到目标货币「{target}」。"
        update = (data.get("time_last_update_utc") or "")[:16]
        lines = [f"1 {base} = {rate:.4f} {target}"]
        try:
            amount = float(amount or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount > 0:
            lines.append(f"{amount:g} {base} = {amount * rate:,.2f} {target}")
        if update:
            lines.append(f"（数据更新：{update} UTC）")
        log.info(f"💱 汇率查询 {base}->{target} = {rate}")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"execute_exchange_rate 失败: {e}")
        return "汇率查询失败：接口暂时不可用。"


def execute_stock_quote(query=""):
    """个股/指数报价（Yahoo Finance 公开接口，免费无 key）。query 为空时返回纳指+标普快照。"""
    from tg_bot.tools.fetch import http_get
    from urllib.parse import quote_plus
    query = (query or "").strip()
    if not query:
        return execute_us_market_snapshot()
    try:
        raw = http_get(
            f"https://query1.finance.yahoo.com/v1/finance/search?q={quote_plus(query)}&quotesCount=1&newsCount=0",
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        quotes = (json.loads(raw or "{}").get("quotes") or [])
        if not quotes:
            return f"没找到「{query}」对应的股票或指数。"
        symbol = quotes[0].get("symbol") or ""
        display = quotes[0].get("shortname") or quotes[0].get("longname") or symbol
        raw2 = http_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote_plus(symbol)}",
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        meta = ((json.loads(raw2 or "{}").get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        currency = meta.get("currency") or ""
        if price is None:
            return f"找到了 {display}（{symbol}），但暂时取不到报价。"
        lines = [f"{display}（{symbol}）", f"{price:,.2f} {currency}"]
        if prev:
            change = (price - prev) / prev * 100
            lines[-1] += f"  {'🟢+' if change >= 0 else '🔴'}{change:.2f}%"
        market_time = meta.get("regularMarketTime")
        if market_time:
            try:
                dt = datetime.fromtimestamp(int(market_time), timezone(timedelta(hours=8)))
                lines.append(f"（{dt.month}月{dt.day}日 {dt:%H:%M} 北京时间数据）")
            except (TypeError, ValueError, OSError):
                pass
        log.info(f"📈 个股报价 {symbol} 获取成功")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"execute_stock_quote 失败: {e}")
        return "股票报价查询失败：接口暂时不可用。"


def execute_world_time(place=""):
    """查询某地当前时间（open-meteo 地理编码给时区 + 本地 zoneinfo 计算）。"""
    from zoneinfo import ZoneInfo
    place = (place or "").strip()
    if not place:
        now = datetime.now(timezone(timedelta(hours=8)))
        return f"北京时间：{now:%Y-%m-%d %H:%M}（{['周一','周二','周三','周四','周五','周六','周日'][now.weekday()]}）"
    located = _geocode_city(place)
    if located is None or not located[3]:
        return f"没找到「{place}」的时区信息，请确认地名。"
    _lat, _lon, name, tz_name = located
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        return f"没找到「{place}」的时区信息，请确认地名。"
    beijing = datetime.now(timezone(timedelta(hours=8)))
    offset_hours = (now.utcoffset() - beijing.utcoffset()).total_seconds() / 3600
    diff = f"比北京{'快' if offset_hours > 0 else '慢'} {abs(offset_hours):g} 小时" if offset_hours else "与北京同时区"
    weekday = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]
    return f"{name}当前时间：{now:%Y-%m-%d %H:%M}（{weekday}，{tz_name}，{diff}）"


def _http_json(url, timeout=12, ua="Mozilla/5.0"):
    """直连接口通用 JSON GET；失败返回 None。"""
    from tg_bot.tools.fetch import http_get
    try:
        raw = http_get(url, headers={"Accept": "application/json", "User-Agent": ua}, timeout=timeout)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _aqi_label(us_aqi):
    for limit_, label in ((50, "优"), (100, "良"), (150, "轻度污染"), (200, "中度污染"), (300, "重度污染")):
        if us_aqi <= limit_:
            return label
    return "严重污染"


def _air_quality_line(lat, lon):
    """open-meteo 空气质量：返回一行 AQI/PM2.5 描述，失败返回空串。"""
    data = _http_json(
        f"https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={lat}&longitude={lon}&current=us_aqi,pm2_5,pm10"
    ) or {}
    cur = data.get("current") or {}
    aqi = cur.get("us_aqi")
    if aqi is None:
        return ""
    parts = [f"空气 AQI {aqi:.0f}（{_aqi_label(aqi)}）"]
    if cur.get("pm2_5") is not None:
        parts.append(f"PM2.5 {cur['pm2_5']:.0f}")
    if cur.get("pm10") is not None:
        parts.append(f"PM10 {cur['pm10']:.0f}")
    return " · ".join(parts)


def execute_chinese_holiday(date_str=""):
    """中国法定节假日/调休查询（jiejiariapi 全年数据）。date_str 空=今天+下一个假期。"""
    today = datetime.now(timezone(timedelta(hours=8))).date()
    try:
        target = datetime.strptime(date_str.strip(), "%Y-%m-%d").date() if date_str.strip() else today
    except ValueError:
        return f"日期「{date_str}」格式不对，请用 YYYY-MM-DD。"
    year_data = _http_json(f"https://api.jiejiariapi.com/v1/holidays/{target.year}") or {}
    if not year_data:
        return "节假日数据接口暂时不可用。"

    def day_desc(d):
        info = year_data.get(d.isoformat())
        if info:
            return f"{info.get('name','')}{'休' if info.get('isOffDay') else '补班'}"
        return "周末休息" if d.weekday() >= 5 else "正常工作日"

    lines = [f"{target.isoformat()}（{['周一','周二','周三','周四','周五','周六','周日'][target.weekday()]}）：{day_desc(target)}"]
    if not date_str.strip():
        # 找下一个法定休假日（跨年时补拉下一年）
        pool = dict(year_data)
        pool.update(_http_json(f"https://api.jiejiariapi.com/v1/holidays/{target.year + 1}") or {})
        upcoming = sorted(
            (d, v) for d, v in pool.items()
            if v.get("isOffDay") and d > target.isoformat()
        )
        if upcoming:
            d, v = upcoming[0]
            days_away = (datetime.strptime(d, "%Y-%m-%d").date() - target).days
            lines.append(f"下一个法定假期：{v.get('name','')}（{d}，{days_away} 天后）")
    log.info("📅 节假日查询成功")
    return "\n".join(lines)


def execute_github_release(repo=""):
    """查任意 GitHub 仓库的最新 release（官方 API）。repo 形如 owner/name。"""
    repo = (repo or "").strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return "请提供 owner/repo 形式的仓库名，如 SagerNet/sing-box、python/cpython。"
    rel = _http_json(
        f"https://api.github.com/repos/{repo}/releases/latest",
        ua="tg-bot-search-assistant",
    )
    if not rel or not rel.get("tag_name"):
        return f"没查到 {repo} 的 release（仓库可能不存在或不用 release 发版）。"
    tag = rel.get("tag_name", "")
    published = (rel.get("published_at") or "")[:10]
    body = re.sub(r"\s+", " ", str(rel.get("body") or "")).strip()[:300]
    lines = [f"{repo} 最新版本：{tag}" + (f"（{published} 发布）" if published else "")]
    if body:
        lines.append(f"更新要点：{body}")
    log.info(f"📦 github_release {repo} = {tag}")
    return "\n".join(lines)


def execute_epic_free_games():
    """Epic 每周免费游戏（官方促销接口）。"""
    data = _http_json(
        "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=zh-CN"
    ) or {}
    elements = ((((data.get("data") or {}).get("Catalog") or {}).get("searchStore") or {}).get("elements") or [])
    now_items, upcoming = [], []
    for el in elements:
        promos = el.get("promotions") or {}
        title = (el.get("title") or "").strip()
        if not title:
            continue
        if any(
            (offer.get("discountSetting") or {}).get("discountPercentage") == 0
            for block in (promos.get("promotionalOffers") or [])
            for offer in (block.get("promotionalOffers") or [])
        ):
            now_items.append(title)
        elif promos.get("upcomingPromotionalOffers"):
            upcoming.append(title)
    if not now_items and not upcoming:
        return "Epic 免费游戏信息暂时无法获取。"
    lines = []
    if now_items:
        lines.append("本周免费领取：" + "、".join(now_items[:4]))
    if upcoming:
        lines.append("即将免费：" + "、".join(upcoming[:4]))
    lines.append("领取地址：store.epicgames.com/free-games")
    log.info(f"🎁 Epic 免费游戏：{len(now_items)} 现行 / {len(upcoming)} 预告")
    return "\n".join(lines)


def execute_cn_hot_topics(platform="all"):
    """中文热榜：微博/知乎（60s 开源聚合，尽力而为）+ B站（官方接口）。"""
    platform = (platform or "all").strip().lower()
    sections = []

    def _sixty(kind, label, emoji):
        data = _http_json(f"https://60s.viki.moe/v2/{kind}") or {}
        rows = data.get("data") or []
        titles = [str(r.get("title") or r.get("word") or "").strip() for r in rows]
        titles = [t for t in titles if t][:8]
        if titles:
            sections.append(f"{emoji} {label}\n" + "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles)))

    if platform in ("weibo", "all", "微博"):
        _sixty("weibo", "微博热搜", "🔥")
    if platform in ("zhihu", "all", "知乎"):
        _sixty("zhihu", "知乎热榜", "💬")
    if platform in ("bilibili", "all", "b站", "bili"):
        data = _http_json("https://api.bilibili.com/x/web-interface/popular?ps=8") or {}
        rows = ((data.get("data") or {}).get("list") or [])[:8]
        titles = [str(r.get("title") or "").strip() for r in rows if r.get("title")]
        if titles:
            sections.append("📺 B站热门\n" + "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles)))
    if not sections:
        return "中文热榜接口暂时都不可用（第三方聚合源不稳定，稍后再试）。"
    log.info(f"🔥 中文热榜获取成功：{len(sections)} 个来源")
    return "\n\n".join(sections)


def execute_earthquake_recent(min_magnitude=5.0, limit=5):
    """近期显著地震（USGS 官方全球数据）。"""
    try:
        min_magnitude = max(2.5, float(min_magnitude or 5.0))
        limit = max(1, min(10, int(limit or 5)))
    except (TypeError, ValueError):
        min_magnitude, limit = 5.0, 5
    data = _http_json(
        "https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
        f"&minmagnitude={min_magnitude}&limit={limit}&orderby=time"
    ) or {}
    feats = data.get("features") or []
    if not feats:
        return f"最近没有 {min_magnitude} 级以上的地震记录（USGS 数据）。"
    lines = [f"近期 {min_magnitude} 级以上地震（USGS，北京时间）："]
    for f in feats:
        p = f.get("properties") or {}
        try:
            when = datetime.fromtimestamp(int(p.get("time", 0)) / 1000, timezone(timedelta(hours=8)))
            when_str = f"{when.month}月{when.day}日 {when:%H:%M}"
        except (TypeError, ValueError, OSError):
            when_str = "?"
        lines.append(f"· M{p.get('mag', '?')} {p.get('place', '')}（{when_str}）")
    log.info(f"🌏 地震查询成功：{len(feats)} 条")
    return "\n".join(lines)


def execute_x_trends():
    """X(推特) 热门话题（trends24 第三方页面解析，尽力而为）。"""
    from tg_bot.tools.fetch import http_get
    try:
        raw = http_get("https://trends24.in/", headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        }, timeout=15)
        if not raw:
            return "X 热门话题暂时无法获取（第三方源不稳定）。"
        import html as _html
        block = re.search(r'<ol class="trend-card__list"[^>]*>(.*?)</ol>', raw, re.DOTALL)
        if not block:
            return "X 热门话题暂时无法获取（页面结构已变）。"
        names = re.findall(r'<a[^>]*>([^<]+)</a>', block.group(1))
        names = [_html.unescape(n).strip() for n in names if n.strip()][:12]
        if not names:
            return "X 热门话题暂时无法获取。"
        log.info(f"🐦 X 热门话题：{len(names)} 条")
        return "X(推特) 当前全球热门话题：\n" + "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
    except Exception as e:
        log.warning(f"execute_x_trends 失败: {e}")
        return "X 热门话题暂时无法获取（第三方源不稳定）。"


def execute_steam_specials(limit=3, exclude_names=None):
    """Steam 官方中国区特惠 API（简体中文、人民币）。

    选品规则：第 1 款固定是"今天折扣力度最大"的一款（诚实标注，不是"历史史低"——
    Steam 官方接口不提供历史价格，没有第三方比价数据源就不编这个事实）；
    剩下 limit-1 款按官方评价数（口碑）从高到低挑，避开已选中的那款。

    exclude_names：可选，传入最近已经推荐过的游戏名集合，用于日报场景"每天不重复"
    （交互聊天里直接问"steam打折"不传这个参数，永远返回当前最佳结果，不受历史影响）。
    """
    from tg_bot.tools.fetch import http_get
    from concurrent.futures import ThreadPoolExecutor
    try:
        raw = http_get(
            "https://store.steampowered.com/api/featuredcategories?cc=cn&l=schinese",
            headers={"Accept": "application/json"},
            timeout=20,
        )
        if not raw:
            return ""
        items = ((json.loads(raw).get("specials") or {}).get("items") or [])
        deals = []
        seen_names = set()
        for it in items:
            name = (it.get("name") or "").strip()
            appid = it.get("id")
            pct = int(it.get("discount_percent") or 0)
            final = it.get("final_price")
            if not name or not appid or pct < 30 or final is None:
                continue
            if name in seen_names:
                continue
            seen_names.add(name)
            deals.append({"appid": appid, "name": name, "pct": pct, "final": final})
        if not deals:
            return ""

        # 昨天/前几天推荐过的名字先排除；候选池不够 limit 款时放弃排除，
        # 宁可偶尔重复，也不要为了"不重复"硬凑进不合格的游戏。
        exclude_names = {n.strip() for n in (exclude_names or ()) if n and n.strip()}
        pool = [d for d in deals if d["name"] not in exclude_names] if exclude_names else deals
        if len(pool) < min(limit, len(deals)):
            pool = deals

        # 官方评价总数当口碑信号，供"好玩"两款排序用。
        with ThreadPoolExecutor(max_workers=min(8, len(pool)), thread_name_prefix="steam-recs") as thread_pool:
            recs = list(thread_pool.map(lambda d: _steam_recommendations_total(d["appid"]), pool))
        for deal, rec in zip(pool, recs):
            deal["recommendations"] = rec

        pool_sorted_by_discount = sorted(pool, key=lambda d: (-d["pct"], -d["recommendations"]))
        discount_pick = pool_sorted_by_discount[0]
        fun_pool = [d for d in pool if d is not discount_pick]
        fun_pool.sort(key=lambda d: -d["recommendations"])
        selected = [discount_pick] + fun_pool[:max(0, limit - 1)]

        lines = []
        link_lines = []
        for deal in selected:
            price = f"¥{deal['final'] / 100:.0f}" if deal["final"] else "免费"
            tag = "  🔥折扣力度最大" if deal is discount_pick else ""
            lines.append(f"• <b>{deal['name']}</b>  {price}  -{deal['pct']}%{tag}")
            link_lines.append(f"{len(link_lines) + 1}. https://store.steampowered.com/app/{deal['appid']}/")
        if link_lines:
            lines.append("<blockquote expandable>🔗 商店链接\n" + "\n".join(link_lines) + "</blockquote>")
        log.info(f"🎮 Steam 中国区特惠抓取成功：{len(selected)} 条")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"execute_steam_specials 失败: {e}")
        return ""


def execute_hn_front_page(limit=8):
    """Hacker News 官方 Algolia API 的当日真实首页热帖（免 key）。"""
    from tg_bot.tools.fetch import http_get
    try:
        raw = http_get(
            f"https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage={int(limit)}",
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if not raw:
            return "HN 热帖暂时无法获取"
        hits = (json.loads(raw).get("hits") or [])
        if not hits:
            return "HN 热帖暂时无法获取"
        # 按热度（points）降序排序，决定谁上榜；热度/讨论数只用来排序，不再显示在正文里。
        hits = sorted(hits, key=lambda h: -(h.get("points") or 0))
        lines = []
        link_lines = []
        for h in hits[:limit]:
            title = (h.get("title") or "").strip()
            if not title:
                continue
            hn_url = f"https://news.ycombinator.com/item?id={h.get('objectID', '')}"
            lines.append(f"• {title}")
            link_lines.append(f"{len(link_lines) + 1}. {h.get('url') or hn_url}")
        if not lines:
            return "HN 热帖暂时无法获取"
        if link_lines:
            lines.append("<blockquote expandable>🔗 原文链接\n" + "\n".join(link_lines) + "</blockquote>")
        log.info(f"🗣️ HN 首页热帖抓取成功：{len(link_lines)} 条")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"execute_hn_front_page 失败: {e}")
        return "HN 热帖暂时无法获取"


_PROXY_RELEASE_REPOS = (
    ("sing-box", "SagerNet/sing-box"),
    ("Xray-core", "XTLS/Xray-core"),
    ("mihomo", "MetaCubeX/mihomo"),
)


def execute_proxy_releases(days=7):
    """直接查代理内核仓库的 GitHub releases；近 N 天无更新时隐藏板块。"""
    from tg_bot.tools.fetch import http_get
    lines = []
    now = datetime.now(timezone.utc)
    for name, repo in _PROXY_RELEASE_REPOS:
        try:
            raw = http_get(
                f"https://api.github.com/repos/{repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "tg-bot-daily-report"},
                timeout=15,
            )
            if not raw:
                continue
            rel = json.loads(raw)
            tag = (rel.get("tag_name") or "").strip()
            published = (rel.get("published_at") or "").strip()
            if not tag or not published:
                continue
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            age_days = (now - pub_dt).total_seconds() / 86400
            if age_days > max(1, int(days)):
                continue
            body = rel.get("body") or ""
            body = re.sub(r"^#+\s*What's Changed\s*", "", body.strip(), flags=re.I)
            body = re.sub(r"\*\s*[0-9a-f]{6,}\s+", "", body)  # 去掉 commit hash
            body = re.sub(r"\s+", " ", body).strip()[:150]
            date_str = pub_dt.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d")
            lines.append(f"  • {name} {tag}（{date_str} 发布）")
            if body:
                lines.append(f"    {body}")
        except Exception as e:
            log.debug(f"proxy release {repo} 查询失败: {e}")
    if not lines:
        return ""
    log.info(f"🔒 代理 releases 抓取成功：{len(lines)} 行")
    return "\n".join(lines)


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
        d = _get(f"{_cfg.DEEPSEEK_API_BASE}/user/balance",
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
