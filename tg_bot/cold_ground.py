"""冷知识取材：先从中文维基百科取材料，模型只能依据材料写，并附出处。

取不到材料或校验不过就返回 None，绝不凭记忆回退。
网络(get)和模型(model)都由参数注入，便于测试。
"""

import json
import re
from urllib.parse import quote

WIKI_API = "https://zh.wikipedia.org/w/api.php"

_TITLE_BLOCKLIST = (
    "列表", "消歧义", "年表", "分类:",
    "中心", "研究所", "大学", "学院", "公司", "协会", "委员会", "实验室", "博物馆",
    "电影", "电视剧", "游戏", "专辑", "小说", "漫画",
)


def wiki_search(topic, *, get):
    url = (
        f"{WIKI_API}?action=query&list=search&srsearch={quote(topic)}"
        "&srlimit=15&format=json&utf8=1&variant=zh-cn"
    )
    try:
        data = json.loads(get(url))
        items = data["query"]["search"]
        titles = [item["title"] for item in items]
    except (ValueError, KeyError, TypeError):
        return []
    result = []
    for title in titles:
        if any(word in title for word in _TITLE_BLOCKLIST):
            continue
        if len(title) > 20:
            continue
        result.append(title)
    return result


def wiki_extract(title, *, get):
    url = (
        f"{WIKI_API}?action=query&prop=extracts&explaintext=1"
        "&exsectionformat=plain&redirects=1"
        f"&titles={quote(title)}&format=json&utf8=1&variant=zh-cn"
    )
    try:
        data = json.loads(get(url))
        pages = data["query"]["pages"]
        first = next(iter(pages.values()))
        text = first.get("extract", "")
    except (ValueError, KeyError, TypeError, StopIteration, AttributeError):
        return ""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:4000]


def wiki_url(title):
    return f"https://zh.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"


def _strip_thousands(s):
    return re.sub(r"(\d),(?=\d{3}(?!\d))", r"\1", s)


def numbers_grounded(content, extract):
    content = _strip_thousands(content)
    extract = _strip_thousands(extract)
    for num in re.findall(r"\d+(?:\.\d+)?", content):
        if num not in extract:
            return False
    return True


COLD_GROUND_PROMPT = (
    "你是中文冷知识科普作者。\n"
    "输入是一个 JSON 对象，字段包括：topic(主题)、title(维基百科条目名)、"
    "extract(维基百科材料正文)、avoid(已经讲过的知识点，不得重复)。\n"
    "规则：\n"
    "1. 只能使用 extract 里明确写出的事实，不得补充任何材料以外的信息。\n"
    "2. 所有数字、年代、名称必须与材料一致，不要改写或推算。\n"
    "3. 写一段 120～200 字的连续中文，不分点、不用小标题，"
    "不要以“你知道吗”之类的话开头。\n"
    "4. 不得重复 avoid 中的知识点。\n"
    "5. 从材料里选最出人意料、最具体的一个事实来写（带数字或实例），"
    "不要写机构、人物或作品的简介。\n"
    "6. 如果材料整体都是机构/人物/作品介绍而没有可讲的具体事实，输出 {\"content\": \"\"}。\n"
    "7. 只输出一个 JSON 对象，字段恰好为 content，例如 {\"content\": \"……\"}。"
    "不要输出任何其他文字。"
)


def build_grounded(topic, avoid, day_of_year, *, get, model, max_attempts=3):
    titles = wiki_search(topic, get=get)
    if not titles:
        return None
    pool = titles[:8]
    k = day_of_year % len(pool)
    rotated = pool[k:] + pool[:k]
    candidates = [t for t in rotated if not any(t in a for a in avoid)]

    attempts = 0
    for title in candidates:
        if attempts >= max_attempts:
            break
        extract = wiki_extract(title, get=get)
        if len(extract) < 1200:
            continue
        attempts += 1
        payload = {
            "topic": topic,
            "title": title,
            "extract": extract,
            "avoid": avoid[:25],
        }
        try:
            data = model(COLD_GROUND_PROMPT, payload)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        content = str(data.get("content") or "").strip()
        if not 60 <= len(content) <= 320:
            continue
        if not numbers_grounded(content, extract):
            continue
        return content + "\n出处: 维基百科《" + title + "》 " + wiki_url(title)
    return None
