#!/usr/bin/env python3
"""core/pipeline.py — 搜索 lane 的流程指挥中心

把 Searcher / Curator / Writer / Critic / Patcher 串联起来。
每个模块可通过 PipelineConfig 独立开关。

调用方式（从 bot.py 里的搜索路径调用）：
    from tg_bot.core.pipeline import run_search_pipeline
    reply = run_search_pipeline(text, keywords, chat_id=chat_id, config=cfg)

这个模块只做串联，不做任何 AI 调用本身。
"""
from __future__ import annotations
import logging
import re
from typing import Optional

from tg_bot.core.contracts import PipelineConfig, WriteRequest
from tg_bot.workers.facts_builder import build_minimal_facts_json

log = logging.getLogger(__name__)


def _trim_at_sentence_boundary(text: str, lo: int, hi: int) -> str:
    text = (text or "").strip()
    if len(text) <= hi:
        return text
    cut = text[:hi]
    marks = [cut.rfind(m) for m in ("。", "！", "？", "\n")]
    pos = max(marks)
    if pos >= max(lo, int(hi * 0.65)):
        return cut[:pos + 1].strip()
    return cut.rstrip("，,；;：:、 ") + "。"


def _enforce_short_length(reply: str, user_text: str, target_words: tuple[int, int]) -> str:
    """显式短字数请求用硬约束兜底，避免模型自称合规但实际超长。"""
    lo, hi = target_words
    if hi > 450:
        return reply

    reply = re.sub(r"\n?（全文约[^）]*字[^）]*）\s*$", "", reply or "").strip()
    if len(reply) <= hi:
        return reply

    try:
        from tg_bot.pipeline.gather import fast_chat
        compressed = fast_chat(
            [{
                "role": "user",
                "content": (
                    f"用户原始要求：{user_text}\n\n"
                    f"请把下面正文压缩到 {lo}-{hi} 个汉字，保留关键事实和必要的 [来源N] 标注。"
                    "不要写字数说明，不要写分析过程，只输出压缩后的正文。\n\n"
                    f"{reply}"
                ),
            }],
            system="你是压缩编辑，只删冗余，不新增事实。",
            max_tokens=max(int(hi * 2.2), 800),
            temp=0.2,
        ).strip()
        compressed = re.sub(r"\n?（全文约[^）]*字[^）]*）\s*$", "", compressed).strip()
        if compressed:
            reply = compressed
            log.info(f"✂️ 显式字数约束压缩：{len(reply)} 字，目标 {lo}-{hi}")
    except Exception as _e:
        log.warning(f"显式字数约束压缩失败: {_e}")

    if len(reply) > hi:
        reply = _trim_at_sentence_boundary(reply, lo, hi)
        log.info(f"✂️ 显式字数约束截断：{len(reply)} 字，目标 {lo}-{hi}")
    return reply


def run_search_pipeline(
    user_text: str,
    keywords: list[str],
    chat_id: Optional[int] = None,
    config: Optional[PipelineConfig] = None,
    history_context: Optional[list[dict]] = None,
    pre_results: Optional[str] = None,
    pre_source_entries: Optional[list[dict]] = None,
    retry_hint: bool = False,
    prev_searches: Optional[list[str]] = None,
    focus_task: Optional[dict] = None,
    suggested_length: str = "",
) -> tuple[str, str, dict]:
    """
    执行完整搜索 pipeline，返回 (reply, verify_status, meta)。

    meta 包含 source_index、tool_calls_summary 等，供 bot.py 记录工作日志。
    """
    from tg_bot.agents.query_fixer import fix_query
    from tg_bot.agents.curator import curate
    from tg_bot.agents.writer import write
    from tg_bot.agents.critic import critique
    from tg_bot.agents.patcher import patch
    from tg_bot.core.contracts import Source
    from tg_bot.pipeline.gather import gather_ai  # 复用现有采集层

    cfg = config or PipelineConfig.from_env()

    meta = {
        "rounds": [],
        "tool_calls_summary": [],
        "tool_results": [],
        "fetched_pages": [],
        "source_index": [],
        "failed_urls": [],
        "facts_json": {},
    }

    # ── Step 0: QueryFixer（可选）────────────────────────────────────
    if cfg.query_fixer:
        query_variants = fix_query(user_text, keywords)
        log.info(f"🔧 QueryFixer: {query_variants}")
    else:
        query_variants = [user_text]
    gather_keywords = list(dict.fromkeys(list(keywords or []) + query_variants))

    # ── Step 1: 采集（复用现有 gather_ai）──────────────────────────
    # gather_ai 返回 (source_index_list, meta)
    raw_sources_list, meta = gather_ai(
        user_text, gather_keywords,
        chat_id=chat_id,
        pre_results=pre_results,
        pre_source_entries=pre_source_entries,
        retry_hint=retry_hint,
        prev_searches=prev_searches,
        focus_task=focus_task,
    )
    meta = meta or {}
    meta.setdefault("rounds", [])
    meta.setdefault("tool_calls_summary", [])
    meta.setdefault("tool_results", [])
    meta.setdefault("fetched_pages", [])
    meta.setdefault("failed_urls", [])
    meta.setdefault("facts_json", {})
    meta["query_variants"] = query_variants

    # 把 gather_ai 返回的 dict list 转为 Source dataclass
    raw_sources = [
        Source(
            id=e.get("id", ""),
            url=e.get("url", ""),
            domain=e.get("domain", ""),
            title=e.get("title", ""),
            snippet=e.get("snippet", ""),
            full_content=e.get("full_content", ""),
            tool=e.get("tool", ""),
            query=e.get("query", ""),
        )
        for e in (raw_sources_list or [])
    ]
    _sl = meta.get("suggested_length") or suggested_length

    # ── Step 2: Curator（可选）────────────────────────────────────────
    if cfg.curator and raw_sources:
        write_req = curate(
            raw_sources,
            user_query=user_text,
            keywords=keywords,
            suggested_length=_sl,
            history_context=history_context,
        )
    else:
        # 降级：不用 Curator，直接用所有来源
        from tg_bot.agents.curator import _LENGTH_MAP, target_words_to_max_tokens
        sl = _sl.strip().lower() if _sl else ""
        target_words = _LENGTH_MAP.get(sl, _LENGTH_MAP["medium"])
        write_req = WriteRequest(
            user_query=user_text,
            sources=raw_sources[:8],
            target_words=target_words,
            history_context=history_context or [],
        )

    if not write_req.sources:
        log.warning("⚠️ 无可用来源，返回兜底提示")
        meta["source_index"] = []
        meta.setdefault("tool_calls_summary", [])
        meta.setdefault("rounds", [])
        return (
            "搜索到了相关信息，但素材质量不足以给出可靠回答。请换个问法或发「再查一下」。",
            "no_sources",
            meta,
        )

    # ── Step 3: Writer ───────────────────────────────────────────────
    reply, reasoning = write(write_req)
    meta["write_reasoning"] = reasoning or ""

    if not (reply or "").strip():
        log.warning("⚠️ Writer 返回空回复")
        try:
            from tg_bot.pipeline.write import write_ai as _legacy_write_ai
            _legacy_sources = [
                {"id": s.id, "url": s.url, "domain": s.domain, "title": s.title,
                 "snippet": s.snippet, "full_content": s.full_content, "tool": s.tool,
                 "query": s.query, "score": s.score}
                for s in write_req.sources
            ]
            reply, reasoning = _legacy_write_ai(
                user_text,
                "",
                history_context=history_context,
                facts_json=None,
                source_index=_legacy_sources,
                suggested_length=_sl,
                tool_results=meta.get("tool_results", []),
            )
            meta["write_reasoning"] = reasoning or meta.get("write_reasoning", "")
            if (reply or "").strip():
                log.info(f"✍️ Writer 空回复，已降级旧 write_ai 生成 {len(reply)} 字")
            else:
                return "生成回复时出错，请稍后再试。", "write_empty", meta
        except Exception as _legacy_e:
            log.warning(f"⚠️ Writer 空回复降级失败: {_legacy_e}", exc_info=True)
            return "生成回复时出错，请稍后再试。", "write_empty", meta

    reply = _enforce_short_length(reply, user_text, write_req.target_words)

    # ── Step 4: Critic + Patcher 循环（可选）──────────────────────────
    verify_status = "skip"
    if cfg.critic:
        for attempt in range(cfg.max_rewrites + 1):
            report = critique(reply, write_req.sources, user_text, attempt)

            if report.verdict == "pass":
                verify_status = "pass"
                log.info(f"✅ Critic 通过（attempt={attempt}）")
                break

            if attempt >= cfg.max_rewrites:
                # 最后一次：用 Patcher 做最小改动
                if cfg.patcher:
                    reply = patch(reply, report, write_req.sources, user_text)
                    verify_status = "patched"
                else:
                    verify_status = "failed"
                break

            if report.verdict == "patch" and cfg.patcher:
                # 小问题：Patcher 直接改
                reply = patch(reply, report, write_req.sources, user_text)
                reply = _enforce_short_length(reply, user_text, write_req.target_words)
                verify_status = f"patched_attempt_{attempt}"
                # 改完再过一次 Critic
                continue

            if report.verdict == "rewrite":
                # 大问题：Writer 重写
                feedback = "\n".join(
                    f"[{iss.severity}] {iss.sentence[:80]} | {iss.reason}"
                    + (f" | 建议：{iss.suggested_fix}" if iss.suggested_fix else "")
                    for iss in report.issues
                )
                write_req.reject_feedback = feedback
                write_req.verifier_checks = [
                    {"status": iss.severity, "sentence": iss.sentence,
                     "reason": iss.reason, "suggested_fix": iss.suggested_fix,
                     "evidence_excerpt": iss.evidence_excerpt}
                    for iss in report.issues
                ]
                reply, reasoning = write(write_req)
                meta["write_reasoning"] = reasoning or meta.get("write_reasoning", "")
                reply = _enforce_short_length(reply, user_text, write_req.target_words)
                verify_status = f"rewrite_attempt_{attempt}"
                continue

    reply = _enforce_short_length(reply, user_text, write_req.target_words)

    # 把 Source 转回 dict 格式，供 bot.py 记录
    meta["source_index"] = [
        {"id": s.id, "url": s.url, "domain": s.domain, "title": s.title,
         "snippet": s.snippet, "full_content": s.full_content, "tool": s.tool,
         "query": s.query, "score": s.score}
        for s in write_req.sources
    ]
    meta["facts_json"] = build_minimal_facts_json(meta["source_index"])
    meta.setdefault("tool_calls_summary", [])
    meta.setdefault("rounds", [])
    meta.setdefault("tool_results", [])
    meta.setdefault("fetched_pages", [])
    meta.setdefault("failed_urls", [])
    meta["verify_status"] = verify_status

    log.info(
        f"🏁 Pipeline 完成: verify={verify_status}, "
        f"sources={len(write_req.sources)}, reply={len(reply)}字"
    )
    return reply, verify_status, meta
