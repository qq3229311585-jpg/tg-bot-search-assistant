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


# The order is intentional: snapshots first, then the current news lanes and
# the legacy/external lanes mentioned by the bot prompt.
DEFAULT_REPORT_SECTIONS: tuple[ReportSectionSpec, ...] = (
    ReportSectionSpec(
        "weather", "天气预报", "snapshot", collector="native:execute_weather",
        aliases=("天气", "安阳天气"),
    ),
    ReportSectionSpec(
        "exchange", "汇率", "snapshot", collector="external",
        aliases=("USD/CNY", "美元", "人民币"),
    ),
    ReportSectionSpec(
        "market", "行情速览", "snapshot", collector="external",
        aliases=("BTC", "ETH", "比特币", "以太坊", "行情"),
    ),
    ReportSectionSpec(
        "china", "中国要闻", "event", "中国 今日 重大新闻", "news",
        aliases=("中国新闻",),
    ),
    ReportSectionSpec(
        "global", "全球要闻", "event", "全球 今日 重大新闻", "news",
        aliases=("全球新闻",),
    ),
    ReportSectionSpec(
        "ai_tech", "AI / 技术", "event", "AI 人工智能 技术 今日 新闻", "news",
        aliases=("AI 速报", "AI速报", "人工智能速报", "人工智能"),
    ),
    ReportSectionSpec(
        "proxy", "代理圈动态", "event", "sing-box Xray-core 代理工具 更新", "news",
        aliases=("代理圈", "sing-box", "Xray", "代理"),
    ),
    ReportSectionSpec(
        "hackernews", "圈子热议", "event", "Hacker News 热议 今日", "news",
        # Legacy prompt/report headings often shorten this to just
        # "圈子在聊" without repeating the Hacker News source name.
        aliases=("圈子在聊", "HackerNews", "Hacker News", "HN"),
    ),
    ReportSectionSpec(
        "github", "GitHub 热榜", "event", "GitHub trending 今日 star", "news",
        aliases=("GitHub热榜", "GitHub Trending", "仓库"),
    ),
    ReportSectionSpec(
        "steam", "Steam 降价优惠", "event", "Steam 降价 优惠 游戏 今日", "news",
        aliases=("Steam降价", "Steam 折扣", "游戏优惠"),
    ),
    ReportSectionSpec(
        "cold_knowledge", "今日冷知识", "event", "科学 历史 趣味 冷知识 今日", "news",
        aliases=("冷知识", "趣味事实"),
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
    heading_like = text.startswith(("【", "📍", "💱", "📈", "🤖", "🔒", "🗣", "🛠", "⚡", "🎮", "🌤", "📊", "💰", "📰", "🧠"))
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
    return f"【{section.title}】"


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
