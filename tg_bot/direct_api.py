"""Shared definition for read-only direct API results in ordinary chat."""

DIRECT_API_TOOLS = frozenset({
    "check_weather",
    "vps_traffic",
    "wikipedia_lookup",
    "github_trending",
    "exchange_rate",
    "stock_quote",
    "world_time",
    "steam_specials",
    "hn_front_page",
    "proxy_releases",
    "chinese_holiday",
    "github_release",
    "epic_free_games",
    "cn_hot_topics",
    "earthquake_recent",
    "x_trends",
})

_FAILURE_PREFIXES = {
    "check_weather": ("天气查询失败:",),
    "vps_traffic": ("流量信息获取失败",),
    "github_trending": ("github 热榜暂时无法获取",),
    "wikipedia_lookup": ("wikipedia 未找到相关词条。",),
    "exchange_rate": ("汇率查询失败",),
    "stock_quote": ("股票报价查询失败", "没找到「"),
    "world_time": ("没找到「",),
    "steam_specials": ("steam 特惠列表暂时无法获取",),
    "hn_front_page": ("hn 热帖暂时无法获取",),
    "proxy_releases": ("代理内核版本信息暂时无法获取",),
    "chinese_holiday": ("节假日数据接口暂时不可用", "日期「"),
    "github_release": ("没查到 ", "请提供 owner/repo"),
    "epic_free_games": ("epic 免费游戏信息暂时无法获取",),
    "cn_hot_topics": ("中文热榜接口暂时都不可用",),
    "earthquake_recent": (),
    "x_trends": ("x 热门话题暂时无法获取",),
}


def direct_api_failure_reason(tool: object, text: object) -> str:
    """Return a known native-tool failure reason without guessing about prose."""
    name = str(tool or "").strip()
    value = str(text or "").strip()
    if not value:
        return "empty_result"
    folded = value.lower()
    for prefix in _FAILURE_PREFIXES.get(name, ()):
        if folded.startswith(prefix.lower()):
            return "native_failure_text"
    return ""
