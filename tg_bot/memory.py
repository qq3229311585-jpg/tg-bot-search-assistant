"""Small, side-effect-free helpers for injecting durable chat context."""

from __future__ import annotations


def build_memory_context(
    summary: str | None,
    context_turns: list[dict] | tuple[dict, ...] | None,
    *,
    profile: str | None = None,
    max_chars: int = 2400,
) -> str:
    """Format saved summaries/context for model prompts without raw reasoning."""
    if max_chars < 64:
        raise ValueError("max_chars must be at least 64")

    lines: list[str] = []
    profile_text = str(profile or "").strip()
    if profile_text:
        lines.extend(("【用户画像（仅作偏好参考）】", profile_text[:1000]))

    summary_text = str(summary or "").strip()
    if summary_text:
        lines.extend(("【长期记忆摘要】", summary_text[:1200]))

    recent = []
    for item in (context_turns or ())[-6:]:
        if not isinstance(item, dict):
            continue
        user = str(item.get("user") or "").strip()
        assistant = str(item.get("assistant") or "").strip()
        if user or assistant:
            recent.append(f"- 用户：{user[:180]}\n  助手摘要：{assistant[:240]}")
    if recent:
        if lines:
            lines.append("")
        lines.extend(("【近期对话摘要】", *recent))

    if not lines:
        return "【长期记忆】暂无可用的历史摘要或近期对话记录。"

    text = "\n".join(lines).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


__all__ = ["build_memory_context"]
