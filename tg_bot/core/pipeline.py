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
    """确定性截断：不调用模型。不能把 [来源N] 截成半个，也不留孤立的标题行。"""
    text = (text or "").strip()
    if len(text) <= hi:
        return text
    cut = text[:hi]

    # 不能把 [来源N] 截成半个：截断点落在未闭合的 [ 里，就退回到那个 [ 之前
    last_open = cut.rfind('[')
    last_close = cut.rfind(']')
    if last_open > last_close:
        cut = cut[:last_open]

    marks = [cut.rfind(m) for m in ("。", "！", "？", "\n")]
    pos = max(marks)
    if pos >= max(lo, int(hi * 0.65)):
        result = cut[:pos + 1].strip()
    else:
        result = cut.rstrip("，,；;：:、 ") + "。"

    # 不留下以“：”结尾、后面已无内容的孤立标题行
    lines = result.splitlines()
    while lines and (not lines[-1].strip() or (
        len(lines[-1].strip()) <= 20 and lines[-1].strip().endswith(("：", ":"))
    )):
        lines.pop()
    trimmed = "\n".join(lines).strip()
    return trimmed or result


def _trim_only_short_length(reply: str, target_words: tuple[int, int]) -> str:
    """Patcher/终检之后只允许确定性截断，不再调模型压缩。"""
    lo, hi = target_words
    if hi > 450:
        return reply
    reply = re.sub(r"\n?（全文约[^）]*字[^）]*）\s*$", "", reply or "").strip()
    if len(reply) <= hi:
        return reply
    trimmed = _trim_at_sentence_boundary(reply, lo, hi)
    log.info(f"✂️ Patch/终检后确定性截断：{len(trimmed)} 字，目标 {lo}-{hi}")
    return trimmed


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
    progress_cb=None,
    local_sources: Optional[list[dict]] = None,
) -> tuple[str, str, dict]:
    """
    执行完整搜索 pipeline，返回 (reply, verify_status, meta)。

    meta 包含 source_index、tool_calls_summary 等，供 bot.py 记录工作日志。

    local_sources: 传入时（本地证据路径，如 vps_traffic / today_report），
    跳过 QueryFixer 和联网采集，直接把这份本地证据构造成 Source 列表送进
    Curator → Writer → Critic → 终检，不会被联网搜索结果覆盖。
    """
    from tg_bot.agents.query_fixer import fix_query
    from tg_bot.agents.curator import curate
    from tg_bot.agents.writer import write
    from tg_bot.agents.critic import critique
    from tg_bot.agents.patcher import patch
    from tg_bot.core.contracts import Source
    from tg_bot.pipeline.gather import gather_ai  # 复用现有采集层
    from tg_bot.workers.source_utils import is_nav_or_empty

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

    if local_sources is not None:
        # ── 本地证据路径：跳过 QueryFixer + 联网采集，直接用本地证据 ──────
        _prog("query_fixer", "done", "本地证据")
        _prog("gather", "done", str(len(local_sources)))
        _local_src_objs = [
            Source(
                id=e.get("id", ""), url=e.get("url", ""), domain=e.get("domain", ""),
                title=e.get("title", ""), snippet=e.get("snippet", ""),
                full_content=e.get("full_content", ""), tool=e.get("tool", ""),
                query=e.get("query", ""), published_at=e.get("published_at", "") or "",
            )
            for e in (local_sources or [])
        ]
        # 直接 API 工具（vps_traffic / read_today_report 等）不按长度过滤，
        # is_nav_or_empty 已经按 tool 区分门槛，这里只是兜底防空。
        valid = [
            s for s in _local_src_objs
            if s.body and not is_nav_or_empty(s.title, s.body, s.tool)
        ]
        for s in valid:
            s.score = 100.0
        from tg_bot.agents.curator import _LENGTH_MAP as _LOCAL_LENGTH_MAP
        _sl_local = (suggested_length or "").strip().lower()
        write_req = WriteRequest(
            user_query=user_text,
            sources=valid,
            target_words=_LOCAL_LENGTH_MAP.get(_sl_local, _LOCAL_LENGTH_MAP["medium"]),
            history_context=history_context or [],
        )
        evidence_sufficient = True
        evidence_gaps = ""
        meta = {
            "rounds": [], "tool_calls_summary": [s.tool for s in valid if s.tool],
            "tool_results": [], "fetched_pages": [], "source_index": [],
            "failed_urls": [], "facts_json": {}, "query_variants": [user_text],
        }
        _prog("curator", "done", f"{len(valid)}条·本地证据")

    else:
        # ── Step 0: QueryFixer（可选）────────────────────────────────────
        _prog("query_fixer", "start", "")
        if cfg.query_fixer:
            query_variants = fix_query(user_text, keywords)
            log.info(f"🔧 QueryFixer: {query_variants}")
        else:
            query_variants = [user_text]
        gather_keywords = list(dict.fromkeys(list(keywords or []) + query_variants))
        _prog("query_fixer", "done", query_variants[0][:30] if query_variants else "")

        # ── Step 1: 采集（复用现有 gather_ai）──────────────────────────
        _prog("gather", "start", "")
        # gather_ai 返回 (source_index_list, meta)
        raw_sources_list, meta = gather_ai(
            user_text, gather_keywords,
            chat_id=None if progress_cb else chat_id,
            pre_results=pre_results,
            pre_source_entries=pre_source_entries,
            retry_hint=retry_hint,
            prev_searches=prev_searches,
            focus_task=focus_task,
            status_cb=progress_cb,
        )
        meta = meta or {}
        meta.setdefault("rounds", [])
        meta.setdefault("tool_calls_summary", [])
        meta.setdefault("tool_results", [])
        meta.setdefault("fetched_pages", [])
        meta.setdefault("failed_urls", [])
        meta.setdefault("facts_json", {})
        meta["query_variants"] = query_variants

        # ── Step 1b: 素材不足时补搜一次（只补一次，不是重试轮）───────────────
        import os as _os

        def _supplement_enabled() -> bool:
            return _os.environ.get("PIPELINE_SUPPLEMENT_GATHER", "true").lower() not in ("false", "0", "no")

        evidence_sufficient = True
        evidence_gaps = ""
        if (not retry_hint) and (meta.get("sufficient") is False) and _supplement_enabled():
            _prog("gather", "start", "补搜一次")
            _prev_qs = [
                str(t).split("(", 1)[-1].rstrip(")")
                for t in (meta.get("tool_calls_summary") or [])
            ]
            _prev_qs = [q for q in _prev_qs if q]
            log.info(f"🔁 素材不足（{meta.get('gather_reason','')}），补搜一次: prev={_prev_qs[:5]}")
            try:
                sup_sources_list, sup_meta = gather_ai(
                    user_text, gather_keywords,
                    chat_id=None if progress_cb else chat_id,
                    pre_results=None,
                    pre_source_entries=raw_sources_list,
                    retry_hint=True,
                    prev_searches=_prev_qs,
                    focus_task=focus_task,
                    status_cb=progress_cb,
                )
                sup_meta = sup_meta or {}
                raw_sources_list = sup_sources_list or raw_sources_list
                for _k in ("tool_calls_summary", "tool_results", "fetched_pages", "rounds", "failed_urls"):
                    meta[_k] = list(meta.get(_k, []) or []) + list(sup_meta.get(_k, []) or [])
                meta["sufficient"] = sup_meta.get("sufficient", meta.get("sufficient"))
                meta["gather_reason"] = sup_meta.get("gather_reason", meta.get("gather_reason", ""))
                meta["suggested_length"] = sup_meta.get("suggested_length") or meta.get("suggested_length", "")
                meta["gather_supplemented"] = True
            except Exception as _sup_e:
                log.warning(f"补搜失败，沿用已有素材: {_sup_e}")
            _prog("gather", "done", str(len(raw_sources_list or [])))

        if meta.get("sufficient") is False:
            evidence_sufficient = False
            evidence_gaps = meta.get("gather_reason", "")
            meta["evidence_sufficient"] = False

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
                published_at=e.get("published_at", "") or "",
            )
            for e in (raw_sources_list or [])
        ]
        _sl = meta.get("suggested_length") or suggested_length
        _prog("gather", "done", str(len(raw_sources)))

        # ── Step 2: Curator（可选）────────────────────────────────────────
        _prog("curator", "start", "")
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

    write_req.evidence_sufficient = evidence_sufficient
    write_req.evidence_gaps = evidence_gaps if not evidence_sufficient else ""

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

    _lo, _hi = write_req.target_words
    _prog("curator", "done", f"{len(write_req.sources)}条·{_lo}-{_hi}字")

    # ── Step 3: Writer ───────────────────────────────────────────────
    _prog("writer", "start", "")
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
    _prog("writer", "done", str(len(reply)))

    # ── Step 4: Critic + Patcher 循环（可选）──────────────────────────
    verify_status = "skip"
    last_report = None
    any_llm_failed = False
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
            report = critique(reply, write_req.sources, user_text, attempt)
            last_report = report
            if report.llm_failed:
                any_llm_failed = True

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
                reply, patch_mode = patch(reply, report, write_req.sources, user_text)
                meta.setdefault("patch_modes", []).append(patch_mode)
                reply = _trim_only_short_length(reply, write_req.target_words)
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
                        reply, patch_mode = patch(reply, report, write_req.sources, user_text)
                        meta.setdefault("patch_modes", []).append(patch_mode)
                        reply = _trim_only_short_length(reply, write_req.target_words)
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

    # ── 终检（A9）：确定性截断 + 再跑一次规则核查，删剩余 HARD 句 ──────────
    from tg_bot.workers import claim_check as _claim_check

    reply = _trim_only_short_length(reply, write_req.target_words)
    final_reply, final_info = _claim_check.finalize_reply(reply, write_req.sources, user_text)
    removed_count = final_info.get("removed_count", 0)
    used_fallback = final_info.get("used_fallback", False)
    if removed_count:
        log.info(f"🧹 终检删除 {removed_count} 处仍无来源支撑的句子")
    reply = final_reply
    meta["final_removed_sentences"] = removed_count

    if used_fallback:
        verify_status = "insufficient_after_check"

    if (not evidence_sufficient) or used_fallback:
        verification_level = "insufficient"
    elif cfg.critic and any_llm_failed:
        verification_level = "rule_only"
    elif removed_count:
        verification_level = "verified_with_removals"
    elif verify_status.startswith("limit_"):
        verification_level = "partial"
    else:
        verification_level = "verified"

    rule_issue_count = last_report.rule_issue_count if last_report else 0
    llm_issue_count = (
        sum(1 for iss in last_report.issues if iss.origin == "llm") if last_report else 0
    )
    meta["verification"] = {
        "level": verification_level,
        "removed_count": removed_count,
        "rule_issue_count": rule_issue_count,
        "llm_issue_count": llm_issue_count,
    }
    # 审计用：最后一次 Critic 报告的完整问题列表（区分规则/模型来源），
    # 供 worklog/detailed_log/sources 落盘，回看某条回复具体因为什么被改/被删。
    meta["critic_issues"] = [
        {
            "sentence": iss.sentence, "severity": iss.severity, "reason": iss.reason,
            "source_ref": iss.source_ref, "origin": iss.origin,
        }
        for iss in (last_report.issues if last_report else [])
    ]
    meta["final_removed_issues"] = final_info.get("final_hard_issues", 0)

    # 把 Source 转回 dict 格式，供 bot.py 记录
    meta["source_index"] = [
        {"id": s.id, "url": s.url, "domain": s.domain, "title": s.title,
         "snippet": s.snippet, "full_content": s.full_content, "tool": s.tool,
         "query": s.query, "score": s.score, "published_at": s.published_at}
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
        f"🏁 Pipeline 完成: verify={verify_status}, verification={verification_level}, "
        f"sources={len(write_req.sources)}, reply={len(reply)}字"
    )
    return reply, verify_status, meta
