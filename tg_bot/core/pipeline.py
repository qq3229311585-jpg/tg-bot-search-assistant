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

_HIGH_RISK_RE = re.compile(
    r"(医学|医疗|用药|药物|病|症状|诊断|治疗|法律|法规|合同|起诉|判刑|"
    r"金融|投资|股票|基金|债券|保险|税|贷款|政策|签证|移民|合规|财报|财务)",
    re.I,
)


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
    """只压缩显式短回答或超短自动档，避免误砍长回答。"""
    from tg_bot.agents.curator import _USER_LENGTH_RE, _BRIEF_HINT_RE
    lo, hi = target_words
    reply = re.sub(r"\n?（全文约[^）]*字[^）]*）\s*$", "", reply or "").strip()
    explicit = bool(_USER_LENGTH_RE.search(user_text or "") or _BRIEF_HINT_RE.search(user_text or ""))
    if not (explicit or hi <= 200) or hi > 1500:
        return reply
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


def _critic_budget(user_text: str, write_req: WriteRequest, cfg: PipelineConfig) -> dict:
    """Decide how much verification latency this request is allowed to spend."""
    _lo, hi = write_req.target_words
    is_single = "single_item" in (write_req.style_hints or [])
    if is_single or hi <= 200:
        return {
            "level": "short",
            "max_fix_cycles": min(cfg.max_rewrites, 1),
            "reaudit_after_fix": False,
            "allow_rewrite": False,
        }
    if _HIGH_RISK_RE.search(user_text or ""):
        return {
            "level": "high_risk",
            "max_fix_cycles": min(cfg.max_rewrites, 2),
            "reaudit_after_fix": True,
            "allow_rewrite": True,
        }
    return {
        "level": "normal",
        "max_fix_cycles": min(cfg.max_rewrites, 1),
        "reaudit_after_fix": True,
        "allow_rewrite": True,
    }


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
    suggested_words: int = 0,
    progress_cb=None,
    gather_fn=None,
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

    def _prog(stage: str, status: str, detail: str = "") -> None:
        if not progress_cb:
            return
        try:
            progress_cb(stage, status, detail)
        except Exception as exc:
            log.debug("progress callback failed (non-fatal): %s", exc)

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
    _prog("query_fixer", "start", "")
    if cfg.query_fixer:
        query_variants = fix_query(user_text, keywords)
        log.info(f"🔧 QueryFixer: {query_variants}")
    else:
        query_variants = [user_text]
    gather_keywords = list(dict.fromkeys(list(keywords or []) + query_variants))
    _prog("query_fixer", "done", query_variants[0][:30] if query_variants else "")

    # 新奇账本仅影响冷知识/笑话等可替换主题；不影响普通事实问答。
    from tg_bot.storage import NOVELTY_REQUEST_RE, append_novelty_entry, extract_novelty_topic, format_novelty_avoid_block
    _is_novelty_request = bool(NOVELTY_REQUEST_RE.search(user_text or ""))
    _avoid_block = format_novelty_avoid_block() if _is_novelty_request else ""

    # ── Step 1: 采集（复用现有 gather_ai）──────────────────────────
    _prog("gather", "start", "")
    # gather_ai 返回 (source_index_list, meta)
    gather_runner = gather_fn or gather_ai
    raw_sources_list, meta = gather_runner(
        user_text, gather_keywords,
        chat_id=None if progress_cb else chat_id,
        pre_results=pre_results,
        pre_source_entries=pre_source_entries,
        retry_hint=retry_hint,
        prev_searches=prev_searches,
        focus_task=focus_task,
        status_cb=progress_cb,
        evidence_audit_enabled=cfg.chat_evidence_audit,
        evidence_corrective_rounds=cfg.chat_evidence_corrective_rounds,
        evidence_max_requirements=cfg.chat_evidence_max_requirements,
        avoid_block=_avoid_block,
        suggested_words=suggested_words,
    )
    meta = meta or {}
    meta.setdefault("rounds", [])
    meta.setdefault("tool_calls_summary", [])
    meta.setdefault("tool_results", [])
    meta.setdefault("fetched_pages", [])
    meta.setdefault("failed_urls", [])
    meta.setdefault("facts_json", {})
    if cfg.chat_evidence_audit:
        meta["raw_reasoning_redacted"] = True
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
    # Complex searches must prove coverage before the Writer sees the material.
    # Fast/direct paths intentionally never set audit_required, so retain their
    # existing low-latency behavior.
    if meta.get("audit_required") and not meta.get("sufficient"):
        supported_ids = set(meta.get("supported_source_ids") or [])
        supported_sources = [source for source in raw_sources if source.id in supported_ids]
        if not raw_sources:
            # 采集本身一无所获，才真正拒答。
            meta["source_index"] = []
            return (
                "没有搜索到可用于回答的资料。请换个问法或发「再查一下」。",
                "insufficient_evidence",
                meta,
            )
        # 审计未通过时不再一票否决拒答：优先用审计认可的来源子集，审计一条
        # 都没认可（含审计器故障）时用全部采集素材，并在回复末尾附降级说明。
        # 宁可给出打了折扣但有依据的回答（如休市日给最近收盘价），也不给零分。
        if supported_sources:
            raw_sources = supported_sources
        meta["degraded_answer"] = True
    # 字数档位：消歧层按"问题分量"定锚，采集层只允许因素材不足下调，
    # 不允许因素材丰富上调——否则"刘备是谁"配上维基全文就会写出1500字。
    _rank = {"short": 0, "medium": 1, "long": 2, "detailed": 3}
    _anchor_sl = (suggested_length or "").strip().lower()
    _gather_sl = str(meta.get("suggested_length") or "").strip().lower()
    if _anchor_sl in _rank and _gather_sl in _rank:
        _sl = _anchor_sl if _rank[_anchor_sl] <= _rank[_gather_sl] else _gather_sl
    else:
        _sl = _anchor_sl if _anchor_sl in _rank else _gather_sl
    _prog("gather", "done", str(len(raw_sources)))

    # ── Step 2: Curator（可选）────────────────────────────────────────
    _prog("curator", "start", "")
    if cfg.curator and raw_sources:
        write_req = curate(
            raw_sources,
            user_query=user_text,
            keywords=keywords,
            suggested_length=_sl,
            suggested_words=suggested_words,
            history_context=history_context,
        )
    else:
        # 降级：不用 Curator，直接用所有来源
        from tg_bot.agents.curator import _LENGTH_MAP, target_words_to_max_tokens
        if suggested_words:
            target_words = (int(suggested_words * 0.85), int(suggested_words * 1.15))
        else:
            sl = _sl.strip().lower() if _sl else ""
            target_words = _LENGTH_MAP.get(sl, _LENGTH_MAP["medium"])
        write_req = WriteRequest(
            user_query=user_text,
            sources=raw_sources[:8],
            target_words=target_words,
            history_context=history_context or [],
        )

    if not write_req.sources:
        _prog("curator", "done", "0")
        log.warning("⚠️ 无可用来源，返回兜底提示")
        meta["source_index"] = []
        meta.setdefault("tool_calls_summary", [])
        meta.setdefault("rounds", [])
        return (
            "搜索到了相关信息，但素材质量不足以给出可靠回答。请换个问法或发「再查一下」。",
            "no_sources",
            meta,
        )

    if _avoid_block:
        write_req.avoid_block = _avoid_block

    _lo, _hi = write_req.target_words
    _prog("curator", "done", f"{len(write_req.sources)}条·{_lo}-{_hi}字")

    # ── Step 3: Writer ───────────────────────────────────────────────
    _prog("writer", "start", "")
    reply, reasoning = write(write_req)
    meta["writer_state"] = {
        "mode": "writer", "reasoning_present": bool(reasoning), "reply_present": bool((reply or "").strip()),
    }
    if not meta.get("raw_reasoning_redacted"):
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
            meta["writer_state"] = {
                "mode": "legacy_fallback", "reasoning_present": bool(reasoning),
                "reply_present": bool((reply or "").strip()),
            }
            if not meta.get("raw_reasoning_redacted"):
                meta["write_reasoning"] = reasoning or meta.get("write_reasoning", "")
            if (reply or "").strip():
                log.info(f"✍️ Writer 空回复，已降级旧 write_ai 生成 {len(reply)} 字")
            else:
                return "生成回复时出错，请稍后再试。", "write_empty", meta
        except Exception as _legacy_e:
            log.warning(f"⚠️ Writer 空回复降级失败: {_legacy_e}", exc_info=True)
            return "生成回复时出错，请稍后再试。", "write_empty", meta

    reply = _enforce_short_length(reply, user_text, write_req.target_words)
    _prog("writer", "done", str(len(reply)))

    # ── Step 4: Critic + Patcher 循环（可选）──────────────────────────
    verify_status = "skip"
    if cfg.critic:
        budget = _critic_budget(user_text, write_req, cfg)
        log.info(
            "🧪 Critic 预算: level=%s max_fix_cycles=%s reaudit=%s rewrite=%s",
            budget["level"], budget["max_fix_cycles"],
            budget["reaudit_after_fix"], budget["allow_rewrite"],
        )
        fix_cycles = 0
        attempt = 0
        while True:
            _prog("critic", "start", str(attempt))
            report = critique(
                reply,
                write_req.sources,
                user_text,
                attempt,
                shard_threshold=cfg.chat_review_shard_threshold,
                max_chunks=cfg.chat_review_max_chunks,
                chunk_evidence_chars=cfg.chat_review_chunk_evidence_chars,
                progress_cb=_prog,
            )
            meta["review_audit"] = {
                "mode": report.mode,
                "chunks_total": report.chunks_total,
                "chunks_passed": report.chunks_passed,
                "chunks_unknown": report.chunks_unknown,
                "issues_total": len(report.issues),
            }

            if report.verdict == "pass":
                verify_status = "pass"
                _prog("critic", "done", "pass")
                log.info(f"✅ Critic 通过（attempt={attempt}）")
                break

            if report.verdict == "unknown":
                verify_status = "unknown"
                _prog("critic", "done", "unknown")
                log.warning("⚠️ Critic 状态 unknown，保留 Writer 草稿并记录，不触发重写")
                break

            if fix_cycles >= budget["max_fix_cycles"]:
                verify_status = f"limit_{report.verdict}"
                _prog("critic", "done", verify_status)
                log.warning(
                    "⚠️ Critic 修正预算已用尽: level=%s verdict=%s fixes=%s",
                    budget["level"], report.verdict, fix_cycles,
                )
                break

            if report.verdict == "patch" and cfg.patcher:
                # 小问题：Patcher 直接改
                _prog("critic", "done", "patch")
                _prog("patcher", "start", "")
                reply = patch(reply, report, write_req.sources, user_text)
                reply = _enforce_short_length(reply, user_text, write_req.target_words)
                fix_cycles += 1
                verify_status = f"patched_once" if fix_cycles == 1 else f"patched_{fix_cycles}"
                _prog("patcher", "done", str(len(reply)))
                if not budget["reaudit_after_fix"]:
                    log.info("🧪 短回答已 patch 一次，跳过二次 Critic")
                    break
                attempt += 1
                continue

            if report.verdict == "rewrite":
                if not budget["allow_rewrite"]:
                    if cfg.patcher and report.issues:
                        reply = patch(reply, report, write_req.sources, user_text)
                        reply = _enforce_short_length(reply, user_text, write_req.target_words)
                        fix_cycles += 1
                        verify_status = "patched_instead_of_rewrite"
                    else:
                        verify_status = "rewrite_blocked_short"
                    _prog("critic", "done", verify_status)
                    log.info("🧪 短回答禁止整篇 rewrite，已走最小处理")
                    break
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
                meta["writer_state"] = {
                    "mode": "rewrite", "reasoning_present": bool(reasoning),
                    "reply_present": bool((reply or "").strip()),
                }
                if not meta.get("raw_reasoning_redacted"):
                    meta["write_reasoning"] = reasoning or meta.get("write_reasoning", "")
                reply = _enforce_short_length(reply, user_text, write_req.target_words)
                fix_cycles += 1
                verify_status = f"rewrite_once" if fix_cycles == 1 else f"rewrite_{fix_cycles}"
                if not budget["reaudit_after_fix"]:
                    break
                attempt += 1
                continue

            verify_status = f"unhandled_{report.verdict}"
            _prog("critic", "done", verify_status)
            break

    reply = _enforce_short_length(reply, user_text, write_req.target_words)

    if meta.get("degraded_answer") and (reply or "").strip():
        # 降级标注只在真正值得提醒时出现：高风险话题（金融/医疗/法律等），
        # 或来源确实单薄（独立域名不足2个）。其余情况静默——审计结果在
        # 进度面板里可见，每条回复都挂免责声明只会让它失去意义。
        _used_domains = {
            source.domain for source in write_req.sources
            if source.domain and not str(source.url or "").startswith("direct://")
        }
        _high_risk = bool(_HIGH_RISK_RE.search(user_text or ""))
        if _high_risk or len(_used_domains) < 2:
            reply = reply.rstrip() + "\n\nⓘ 部分信息未交叉核实，重要事项请以官方来源为准。"

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

    if _is_novelty_request and (reply or "").strip():
        try:
            first_line = next((line.strip() for line in reply.splitlines() if line.strip()), "")
            append_novelty_entry("chat", extract_novelty_topic(first_line))
        except Exception as exc:
            log.debug("novelty ledger 记录失败: %s", exc)

    log.info(
        f"🏁 Pipeline 完成: verify={verify_status}, "
        f"sources={len(write_req.sources)}, reply={len(reply)}字"
    )
    return reply, verify_status, meta
