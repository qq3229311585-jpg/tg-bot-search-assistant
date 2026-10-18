"""日报板块注册表与 legacy 文本兼容工具。

板块本身只描述策略和采集边界，不在这里发起网络请求。采集器可以由
``scripts/build-daily-report.py``、定时任务或外部日报生成器按 ``collector``
字段接入；这样新增 Steam、代理更新等来源时，不需要再改历史状态模型。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class ReportSectionSpec:
    """One stable user-facing report section."""

    id: str
    title: str
    kind: str  # snapshot | event
    query: str = ""
    collector: str = "external"
    items: int = 4
    cooldown_days: int = 14
    aliases: tuple[str, ...] = ()
    emoji: str = "📌"
    reserve: bool = False


# The order is intentional: snapshots first, then the current news lanes and
# the legacy/external lanes mentioned by the bot prompt.
DEFAULT_REPORT_SECTIONS: tuple[ReportSectionSpec, ...] = (
    ReportSectionSpec(
        "weather", "天气预报", "snapshot", collector="native:execute_weather",
        aliases=("天气", "安阳天气"), emoji="🌤",
    ),
    ReportSectionSpec(
        "exchange", "汇率", "snapshot", collector="native:execute_exchange_snapshot",
        aliases=("USD/CNY", "美元", "人民币"), emoji="💱",
    ),
    ReportSectionSpec(
        "market", "美股速览", "snapshot", collector="native:execute_us_market_snapshot",
        aliases=("纳斯达克", "标普500", "美股", "NASDAQ", "S&P500", "行情"), emoji="📈",
    ),
    ReportSectionSpec(
        "china", "中国要闻", "event", "中国 今日 重大新闻", "news",
        aliases=("中国新闻",), emoji="📰",
    ),
    ReportSectionSpec(
        "global", "全球要闻", "event", "全球 今日 重大新闻", "news",
        aliases=("全球新闻",), emoji="🌍",
    ),
    ReportSectionSpec(
        "ai_tech", "AI 速报", "event", "AI 人工智能 技术 今日 新闻", "news",
        aliases=("AI / 技术", "AI速报", "人工智能速报", "人工智能"), emoji="🤖",
    ),
    ReportSectionSpec(
        "beyond", "视野外", "event", "世界 各地 新发现 新闻", "news", emoji="🧭",
    ),
    ReportSectionSpec(
        "security_privacy", "安全与隐私", "event", "网络安全 隐私 漏洞 今日 新闻", "news",
        emoji="🛡", reserve=True,
    ),
    ReportSectionSpec(
        "open_source", "开源与开发工具", "event", "开源 开发者工具 今日 发布 更新", "news",
        emoji="🧰", reserve=True,
    ),
    ReportSectionSpec(
        "science_space", "科学与太空", "event", "科学 太空 研究 今日 突破", "news",
        emoji="🔭", reserve=True,
    ),
    ReportSectionSpec(
        "product_updates", "产品与服务更新", "event", "软件 产品 服务 今日 重大更新", "news",
        emoji="📦", reserve=True,
    ),
    # 代理圈动态改为直接查 sing-box / Xray-core 的 GitHub releases，
    # 有新版本才报，不再用新闻搜索拿旧文章凑数。
    ReportSectionSpec(
        "proxy", "代理圈动态", "snapshot", collector="native:execute_proxy_releases",
        aliases=("代理圈", "sing-box", "Xray", "代理"), emoji="🔒",
    ),
    # 圈子在聊改为 HN 官方 API 的当日真实首页热帖。
    ReportSectionSpec(
        "hackernews", "圈子在聊", "snapshot", collector="native:execute_hn_front_page",
        aliases=("圈子热议", "HackerNews", "Hacker News", "HN"), emoji="🗣️",
    ),
    # GitHub 热榜改用真实 trending 页面抓取器（每天必然不同）。
    ReportSectionSpec(
        "github", "GitHub 热榜", "snapshot", collector="native:execute_github_trending",
        aliases=("GitHub热榜", "GitHub Trending", "仓库"), emoji="🛠",
    ),
    # Steam 特价改为官方中国区 API（简体中文、人民币价格），不再用新闻搜索凑数。
    ReportSectionSpec(
        "steam", "Steam 特价推荐", "snapshot", collector="native:execute_steam_specials",
        aliases=("Steam 降价优惠", "Steam降价", "Steam 折扣", "游戏优惠"), emoji="🎮",
    ),
    # 别国维基热榜：每天轮换一个外语维基，取昨日暴涨条目，由构建脚本生成（generated）。
    ReportSectionSpec(
        "wiki_trending", "别国维基热榜", "snapshot", collector="generated",
        aliases=("维基热榜", "外语维基"), emoji="🌐",
    ),
    # 冷知识由构建脚本的编辑 AI 按话题轮盘直接撰写（generated），不走新闻搜索。
    ReportSectionSpec(
        "cold_knowledge", "今日冷知识", "snapshot", collector="generated",
        aliases=("冷知识", "趣味事实"), emoji="🧠",
    ),
)

DEFAULT_REPORT_SECTION_IDS = tuple(item.id for item in DEFAULT_REPORT_SECTIONS)
SUPPORTED_REPORT_SECTION_IDS = frozenset(DEFAULT_REPORT_SECTION_IDS)
EVENT_SECTION_IDS = tuple(item.id for item in DEFAULT_REPORT_SECTIONS if item.kind == "event")
SNAPSHOT_SECTION_IDS = tuple(item.id for item in DEFAULT_REPORT_SECTIONS if item.kind == "snapshot")

_SECTION_BY_ID = {item.id: item for item in DEFAULT_REPORT_SECTIONS}
_SECTION_COLLECTORS: dict[str, Callable[..., Any]] = {}


def section_spec(section_id: str) -> ReportSectionSpec:
    """Return a registered section or raise a useful configuration error."""
    key = str(section_id or "").strip().lower()
    try:
        return _SECTION_BY_ID[key]
    except KeyError as exc:
        raise ValueError(f"unknown daily report section: {section_id!r}") from exc


def resolve_sections(section_ids: Iterable[str] | None = None) -> tuple[ReportSectionSpec, ...]:
    """Resolve a user-configured ordered list while removing duplicates."""
    if section_ids is None:
        return DEFAULT_REPORT_SECTIONS
    result = []
    seen = set()
    for raw in section_ids:
        key = str(raw or "").strip().lower()
        if not key or key in seen:
            continue
        result.append(section_spec(key))
        seen.add(key)
    return tuple(result) or DEFAULT_REPORT_SECTIONS


def register_section_collector(section_id: str, collector: Callable[..., Any]) -> Callable[..., Any]:
    """Register a process-local collector adapter for a section.

    Adapters may return a formatted snapshot string or structured candidate
    mappings. The build script currently uses its news-provider boundary for
    event sections, but this hook keeps native/external collectors replaceable
    without changing the section registry or state format.
    """
    if not callable(collector):
        raise TypeError("section collector must be callable")
    key = section_spec(section_id).id
    _SECTION_COLLECTORS[key] = collector
    return collector


def get_section_collector(section_id: str) -> Callable[..., Any] | None:
    return _SECTION_COLLECTORS.get(section_spec(section_id).id)


def _matches_section(line: str, spec: ReportSectionSpec) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    # Headings normally use 【...】 or an emoji. Requiring one of these avoids
    # treating a sentence mentioning “GitHub” as a new section.
    heading_like = text.startswith(("【", "📍", "💱", "📈", "🤖", "🔒", "🗣", "🛠", "⚡", "🎮", "🌤", "📊", "💰", "📰", "🧠", "🌍", "📌"))
    if not heading_like and not re.match(r"^(?:天气|汇率|行情|AI|代理|圈子|GitHub|Steam|今日冷知识|Hacker|HN|sing-box|Xray)", text, re.I):
        return False
    terms = (spec.title, spec.id, *spec.aliases)
    return any(term and term.casefold() in text.casefold() for term in terms)


def split_external_sections(text: str, sections: Iterable[ReportSectionSpec] | None = None) -> dict[str, str]:
    """Extract known sections from an externally generated legacy report.

    The returned values include their original heading and body so an old
    collector can be preserved byte-for-byte apart from surrounding whitespace.
    Unknown text is intentionally ignored; the generated report header is
    supplied by the new renderer.
    """
    specs = tuple(sections or DEFAULT_REPORT_SECTIONS)
    lines = str(text or "").splitlines()
    found: dict[str, list[str]] = {}
    current: ReportSectionSpec | None = None
    for line in lines:
        # 板块分隔线不属于任何板块内容
        if line.strip() and set(line.strip()) <= {"┄", "─", "-"}:
            current = None
            continue
        matched = next((spec for spec in specs if _matches_section(line, spec)), None)
        if matched is not None:
            current = matched
            found.setdefault(matched.id, []).append(line.rstrip())
            continue
        if current is not None:
            found[current.id].append(line.rstrip())
    return {
        section_id: "\n".join(lines).strip()
        for section_id, lines in found.items()
        if "\n".join(lines).strip()
    }


def section_heading(section: ReportSectionSpec) -> str:
    emoji = getattr(section, "emoji", "") or ""
    return f"{emoji}  {section.title}".strip()


def empty_section_text(section: ReportSectionSpec, *, repeated: bool = False) -> str:
    if repeated:
        body = "今日没有新的高价值事件，已跳过重复内容。"
    elif section.kind == "snapshot":
        body = "今日快照采集器暂未提供数据。"
    else:
        body = "今日暂无可验证的新内容。"
    return f"{section_heading(section)}\n\n{body}"


__all__ = [
    "ReportSectionSpec",
    "DEFAULT_REPORT_SECTIONS",
    "DEFAULT_REPORT_SECTION_IDS",
    "SUPPORTED_REPORT_SECTION_IDS",
    "EVENT_SECTION_IDS",
    "SNAPSHOT_SECTION_IDS",
    "section_spec",
    "resolve_sections",
    "register_section_collector",
    "get_section_collector",
    "split_external_sections",
    "section_heading",
    "empty_section_text",
]
