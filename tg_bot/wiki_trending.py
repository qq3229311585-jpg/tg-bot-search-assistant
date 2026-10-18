"""别国维基热榜：每天轮换一个语言版本的维基百科，取最近有数据的一天里浏览量相对平日暴涨的条目。

流程：选语言 -> 向前探测有数据的热榜日期 -> 逐个候选(前12)算"暴涨倍数" -> 取材料(intro) -> 模型只能依据材料写
-> 代码校验引文与数字 -> 由代码拼接成固定格式的文本。
任何一步取不到或校验不过都返回 None（调用方整块跳过），绝不凭记忆回退。
网络(get)和模型(model)都由参数注入，便于测试；网络函数失败时返回 None/[]，不抛异常。
"""

import json
import re
import statistics
import time
from datetime import timedelta
from urllib.parse import quote

from tg_bot.cold_ground import numbers_grounded

LANGS = (
    ("ja", "日语"), ("de", "德语"), ("fr", "法语"), ("es", "西班牙语"),
    ("ru", "俄语"), ("it", "意大利语"), ("pt", "葡萄牙语"), ("ko", "韩语"),
    ("ar", "阿拉伯语"), ("tr", "土耳其语"), ("pl", "波兰语"), ("nl", "荷兰语"),
    ("id", "印尼语"), ("vi", "越南语"), ("sv", "瑞典语"), ("fa", "波斯语"),
    ("th", "泰语"), ("uk", "乌克兰语"), ("cs", "捷克语"), ("he", "希伯来语"),
)
LANG_FALLBACK_TRIES = 4  # 当天语言 + 顺延最多 3 种
TOP_LIMIT = 25
SURGE_CANDIDATES = 12  # surge 只评估热榜前 12 个
SURGE_MIN_VIEWS = 3000
SURGE_MIN_RATIO = 3.0
MAX_MODEL_CANDIDATES = 2
PROBE_DAYS = 5  # 热榜数据有延迟，从昨天起向前最多探测 5 个日期
TOP_RETRY_DELAYS = (2, 4)  # 热榜请求返回空(多为 429)时的重试间隔
PROBE_RETRY_DELAYS = (2,)  # 按日期探测时只重试 1 次，避免浪费太久
SURGE_RETRY_DELAYS = (2, 4)
SURGE_GAP_SECONDS = 0.4  # 相邻 surge 请求之间的间隔
EXTRACT_MIN_CHARS = 150
EXTRACT_MAX_CHARS = 1500

_HOME_MARKERS = (
    "Main_Page", "Hauptseite", "Заглавная", "メインページ", "Головна", "Accueil",
    "Página_principal", "Portada", "Strona_główna", "Hlavní_strana", "Huvudsida",
    "Ana_Sayfa", "صفحه_اصلی", "Trang_Chính", "Halaman_Utama", "หน้าหลัก",
    "עמוד_ראשי", "대문", "Etusivu", "Forside", "Hoofdpagina", "Pagina_principale",
    "Pagina_principal",
)

WIKI_TRENDING_PROMPT = (
    "你是中文科普编辑，负责介绍外语维基百科的热门条目。\n"
    "输入是一个 JSON 对象，字段包括：lang_zh(条目所属语言版本的中文名)、title(原文条目名)、"
    "extract(维基百科条目开头的原文材料)、views、baseline、ratio(浏览量数据，仅供你了解，不要在输出中复述)。\n"
    "规则：\n"
    "1. summary：用 60～200 字的中文介绍这个条目是什么。只能依据 extract 里明确写出的内容，"
    "不得补充材料以外的信息。\n"
    "2. 禁止猜测或解释这个条目为什么突然变热、暴涨的原因；extract 里没有明确写出原因，就不要提原因。\n"
    "3. quote：从 extract 里逐字摘取的一句原文，长度 8～120 个字符，必须与 extract 完全一致，"
    "不得改写、不得加引号、不得拼接多处。\n"
    "4. quote_zh：quote 这一句的准确中文翻译。\n"
    "5. 所有数字、年代、名称必须与 extract 一致，不要改写或推算。\n"
    "6. 不要因为条目涉及的题材（政治、宗教、性、战争、犯罪等）而拒绝或回避，只按材料客观介绍。\n"
    "7. 只输出一个 JSON 对象，字段恰好为 summary、quote、quote_zh。不要输出任何其他文字。"
)


def _get_json(get, url, sleep=time.sleep, delays=()):
    """取 JSON 对象。响应为空(多为 429)时按 delays 依次等待后重试；仍失败返回 None。"""
    text = get(url) or ""
    for delay in delays:
        if text.strip():
            break
        sleep(delay)
        text = get(url) or ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _norm_space(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _is_home_or_namespace(title):
    if ":" in title or not title.strip():
        return True
    underscored = title.replace(" ", "_")
    return any(marker in underscored for marker in _HOME_MARKERS)


def rotation_languages(day_of_year):
    """返回当天要尝试的语言列表（当天语言在前，之后顺延，共 LANG_FALLBACK_TRIES 种）。"""
    start = day_of_year % len(LANGS)
    return [LANGS[(start + i) % len(LANGS)] for i in range(LANG_FALLBACK_TRIES)]


def top_titles(lang, date, get, sleep=time.sleep, delays=TOP_RETRY_DELAYS):
    """取 date 当天该语言版本的热榜前 TOP_LIMIT 个非页面条目标题。失败返回 []。"""
    url = (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
        f"{lang}.wikipedia/all-access/{date:%Y}/{date:%m}/{date:%d}"
    )
    data = _get_json(get, url, sleep=sleep, delays=delays)
    try:
        articles = data["items"][0]["articles"]
    except (KeyError, IndexError, TypeError):
        return []
    result = []
    for item in articles if isinstance(articles, list) else []:
        title = item.get("article") if isinstance(item, dict) else None
        if not isinstance(title, str) or _is_home_or_namespace(title):
            continue
        result.append(title)
        if len(result) >= TOP_LIMIT:
            break
    return result


def surge(lang, title, end_date, get, sleep=time.sleep):
    """计算 end_date 当日浏览量相对前 14 天中位数的倍数。

    未达阈值（当日 >= SURGE_MIN_VIEWS 且倍数 >= SURGE_MIN_RATIO）或数据不足返回 None，
    否则返回 (当日浏览量, 基线, 倍数)。
    """
    start = end_date - timedelta(days=14)
    url = (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
        f"{lang}.wikipedia/all-access/user/{quote(title, safe='')}/daily/"
        f"{start:%Y%m%d}/{end_date:%Y%m%d}"
    )
    data = _get_json(get, url, sleep=sleep, delays=SURGE_RETRY_DELAYS)
    try:
        items = data["items"]
    except (KeyError, TypeError):
        return None
    by_day = {}
    for item in items if isinstance(items, list) else []:
        try:
            by_day[str(item["timestamp"])[:8]] = int(item["views"])
        except (KeyError, TypeError, ValueError):
            continue
    today = by_day.get(f"{end_date:%Y%m%d}")
    if today is None:
        return None
    previous = [
        by_day[f"{end_date - timedelta(days=d):%Y%m%d}"]
        for d in range(1, 15)
        if f"{end_date - timedelta(days=d):%Y%m%d}" in by_day
    ]
    if len(previous) < 7:
        return None
    baseline = max(1, int(round(statistics.median(previous))))
    ratio = today / baseline
    if today < SURGE_MIN_VIEWS or ratio < SURGE_MIN_RATIO:
        return None
    return today, baseline, ratio


def extract_intro(lang, title, get):
    """取条目开头正文，压缩空白后取前 EXTRACT_MAX_CHARS 字；不足 EXTRACT_MIN_CHARS 字返回 None。"""
    url = (
        f"https://{lang}.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1"
        f"&explaintext=1&redirects=1&titles={quote(title, safe='')}&format=json&utf8=1"
    )
    data = _get_json(get, url)
    try:
        pages = data["query"]["pages"]
        first = next(iter(pages.values()))
        text = first.get("extract", "")
    except (KeyError, TypeError, StopIteration, AttributeError):
        return None
    if not isinstance(text, str):
        return None
    text = _norm_space(text)[:EXTRACT_MAX_CHARS]
    if len(text) < EXTRACT_MIN_CHARS:
        return None
    return text


def zh_title(lang, title, get):
    """查中文译名（langlinks），查不到返回 None，由调用方回退到原标题。"""
    url = (
        f"https://{lang}.wikipedia.org/w/api.php?action=query&prop=langlinks&lllang=zh"
        f"&titles={quote(title, safe='')}&format=json&utf8=1"
    )
    data = _get_json(get, url)
    try:
        pages = data["query"]["pages"]
        for page in pages.values():
            for link in page.get("langlinks") or []:
                name = link.get("*")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    except (KeyError, TypeError, AttributeError):
        return None
    return None


def _clean_quote(text):
    return _norm_space(text).strip("“”\"'「」『』 ")


def _validate(data, extract):
    """返回 (summary, quote, quote_zh) 或 None。"""
    if not isinstance(data, dict):
        return None
    summary = _norm_space(data.get("summary"))
    quote_text = _clean_quote(data.get("quote"))
    quote_zh = _norm_space(data.get("quote_zh"))
    if not summary or not quote_zh:
        return None
    if not 8 <= len(quote_text) <= 120:
        return None
    if quote_text not in _norm_space(extract):
        return None
    if not numbers_grounded(summary, extract) or not numbers_grounded(quote_zh, extract):
        return None
    return summary, quote_text, quote_zh


def _same_title(a, b):
    return _norm_space(a.replace("_", " ")).lower() == _norm_space(b.replace("_", " ")).lower()


def probe_end_date(lang, yesterday, get, sleep=time.sleep, cache=None):
    """从 yesterday 起向前最多探测 PROBE_DAYS 个日期，返回 (有数据的日期, 热榜标题) 或 None。

    同一次运行内 cache 按语言缓存探测结果，同一语言只探测一次。
    """
    if cache is not None and lang in cache:
        return cache[lang]
    found = None
    for back in range(PROBE_DAYS):
        day = yesterday - timedelta(days=back)
        titles = top_titles(lang, day, get, sleep=sleep, delays=PROBE_RETRY_DELAYS)
        if titles:
            found = (day, titles)
            break
    if cache is not None:
        cache[lang] = found
    return found


def _build_for_language(lang, lang_zh, yesterday, avoid, get, model, sleep, cache, state):
    probed = probe_end_date(lang, yesterday, get, sleep=sleep, cache=cache)
    if probed is None:
        return None
    end_date, titles = probed

    avoid_norm = {_norm_space(item.replace("_", " ")).lower() for item in avoid}
    qualified = []
    for title in titles[:SURGE_CANDIDATES]:
        if _norm_space(title.replace("_", " ")).lower() in avoid_norm:
            continue
        if state["surge_done"]:
            sleep(SURGE_GAP_SECONDS)
        state["surge_done"] = True
        result = surge(lang, title, end_date, get, sleep=sleep)
        if result is not None:
            qualified.append((result, title))
    qualified.sort(key=lambda pair: pair[0][2], reverse=True)

    model_calls = 0
    for (views, baseline, ratio), title in qualified:
        if model_calls >= MAX_MODEL_CANDIDATES:
            break
        extract = extract_intro(lang, title, get)
        if extract is None:
            continue
        model_calls += 1
        payload = {
            "lang_zh": lang_zh,
            "title": title.replace("_", " "),
            "extract": extract,
            "views": views,
            "baseline": baseline,
            "ratio": round(ratio, 2),
        }
        try:
            data = model(WIKI_TRENDING_PROMPT, payload)
        except Exception:
            continue
        checked = _validate(data, extract)
        if checked is None:
            continue
        summary, quote_text, quote_zh = checked

        original = title.replace("_", " ")
        translated = zh_title(lang, title, get) or original
        heading = f"{lang_zh}维基百科 · 《{translated}》"
        if not _same_title(translated, original):
            heading += f"(原题:{original})"
        return "\n".join([
            heading,
            summary,
            f"原文:“{quote_text}”",
            f"译:{quote_zh}",
            f"{end_date.month}月{end_date.day}日浏览 {views:,} 次,约为平日的 {ratio:.0f} 倍(基线日均约 {baseline:,})",
            f"出处: {lang_zh}维基百科 https://{lang}.wikipedia.org/wiki/{quote(title)}",
        ])
    return None


def build_wiki_trending(day_of_year, now_utc, *, get, model, avoid_titles=(), sleep=time.sleep):
    """返回六行格式的板块文本；无合格条目或任何失败返回 None。"""
    try:
        yesterday = (now_utc - timedelta(days=1)).date()
        avoid = [str(item) for item in (avoid_titles or ()) if str(item or "").strip()]
        cache = {}
        state = {"surge_done": False}
        for lang, lang_zh in rotation_languages(day_of_year):
            text = _build_for_language(lang, lang_zh, yesterday, avoid, get, model, sleep, cache, state)
            if text:
                return text
        return None
    except Exception:
        return None
