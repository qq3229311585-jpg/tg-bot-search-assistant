#!/usr/bin/env python3
"""pipeline/verify.py — 第四层：Verifier（审核/修补）"""

import json, re, logging

from tg_bot.prompts import _SYS_VERIFY as _SYS_VERIFIER
from tg_bot.tools.fetch import http_post
from tg_bot.storage import save_verifier_thinking

log = logging.getLogger(__name__)


def verify_reply(reply, fact_list, user_text="", attempt=0, source_index=None,
                 facts_json=None):
    """
    第四层：Verifier。
    对照事实清单 + 原始素材 核查写作AI回复，输出 JSON verdict。
    thinking enabled：不开则决策不透明，无法溯源误判原因。
    fact_list: 采集AI输出的结构化事实清单。
    source_index: gather_ai 采集到的原始来源列表（含 snippet / full_content）。
    返回 ("pass", flagged_str)、("reject", flagged_str) 或 ("unknown", reason)
    """
    import tg_bot.config as _cfg
    if not (reply or "").strip():
        return "reject", "写作AI返回空回复", []

    # 构建 F 编号证据表：逐条事实对照原始材料，供 verifier 严格核查。
    fact_block = ""
    fact_rows = (facts_json or {}).get("facts") or []
    if fact_rows:
        lines = ["（以下为事实编号与证据摘录。核查时按 F 编号逐条对照，不得泛泛放行。）\n"]
        for fact in fact_rows[:16]:
            lines.append(f"[{fact.get('id','')}] {fact.get('claim','')}")
            if fact.get("source_domains"):
                lines.append("来源域名：" + "、".join(fact.get("source_domains")[:4]))
            if fact.get("quote"):
                lines.append(f"采集片段：{fact.get('quote')}")
            evs = fact.get("evidence") or []
            if evs:
                for ev in evs[:2]:
                    lines.append(
                        f"证据 {ev.get('source_id','')} / {ev.get('domain','')}："
                        f"{(ev.get('material_excerpt') or '')[:1500]}"
                    )
            else:
                lines.append("证据：未能在原始素材索引中定位到对应正文，只能按事实清单文字低置信处理")
            lines.append("")
        fact_block = "\n".join(lines)

    # 构建原始素材块（优先 full_content 前1500字，其次 snippet）
    source_block = ""
    # ── 构建与 write_ai 相同的编号素材列表 ──────────────────────────
    numbered_sources = ""
    if source_index:
        _ns = []
        for _i, _e in enumerate(source_index, 1):
            _body = (_e.get("full_content") or _e.get("snippet") or "").replace("\n", " ").strip()[:3000]
            if not _body:
                continue
            _ns.append(f"[来源{_i}] {(_e.get('title') or _e.get('domain') or '')[:80]}（{(_e.get('domain') or _e.get('tool') or '')[:40]}）")
            _ns.append(_body)
            _ns.append("")
        numbered_sources = "\n".join(_ns)
    def _source_priority(entry):
        domain = (entry.get("domain") or "").lower()
        title = (entry.get("title") or "").lower()
        tool = (entry.get("tool") or "").lower()
        body = (entry.get("full_content") or entry.get("snippet") or "").strip()
        score = 0
        if tool in ("fetch_content", "read_today_report"):
            score -= 20
        elif tool in ("wikipedia_lookup", "read_today_cache"):
            score -= 8
        elif tool in ("web_search", "serper_search"):
            score += 30
        if domain.startswith("www.") or domain.endswith(".gov") or domain.endswith(".edu"):
            score -= 12
        if domain in ("wikipedia.org", "en.wikipedia.org", "zh.wikipedia.org"):
            score -= 4
        if "official" in title or "官网" in title or "学校概况" in title or "学校简介" in title:
            score -= 10
        if len(body) < 160:
            score += 3
        return score

    def _has_official_source(entries):
        for entry in entries or []:
            tool = (entry.get("tool") or "").lower()
            domain = (entry.get("domain") or "").lower()
            title = (entry.get("title") or "").lower()
            if tool in ("fetch_content", "read_today_report") and (
                domain.endswith(".gov") or domain.endswith(".edu") or
                "官网" in title or "official" in title or "学校概况" in title or "学校简介" in title
            ):
                return True
        return False

    if source_index:
        lines = ["（以下为网页/API原始素材，核查时以此为第一权威）\n"]
        seen = set()
        ordered_sources = []
        src_items = list(source_index or [])
        if _has_official_source(src_items):
            src_items = [
                e for e in src_items
                if (e.get("tool") or "").lower() in ("fetch_content", "read_today_report", "wikipedia_lookup")
                or (e.get("domain") or "").lower().endswith((".gov", ".edu"))
                or any(k in (e.get("title") or "").lower() for k in ("官网", "official", "学校概况", "学校简介"))
            ]
        for entry in sorted(src_items, key=_source_priority):
            key = entry.get("url") or entry.get("id") or entry.get("title")
            if not key or key in seen:
                continue
            seen.add(key)
            ordered_sources.append(entry)
        for i, entry in enumerate(ordered_sources[:12]):
            title   = entry.get("title", "")[:60]
            domain  = entry.get("domain", "")
            content = (entry.get("full_content") or "")[:4000].strip()
            snippet = (entry.get("snippet") or "")[:500].strip()
            body    = content if content else snippet
            if not body:
                continue
            lines.append(f"[来源{i+1}] {title}（{domain}）")
            lines.append(body)
            lines.append("")
        if len(lines) > 1:
            source_block = "\n".join(lines)

    # ── 新格式：用编号素材 + 待审回复，让 verifier 对照 [来源N] 核查 ──
    if numbered_sources:
        user_msg = (
            f"【用户原始问题】\n{user_text}\n\n"
            f"【原文素材（write_ai 写作时使用的同一份）】\n{numbered_sources}\n"
            f"【待审回复】\n{reply}"
        )
    elif fact_block:
        user_msg = (
            f"【用户原始问题】\n{user_text}\n\n"
            f"【事实清单】\n{fact_list}\n\n"
            f"【原始素材】\n{source_block}\n"
            f"【待审回复】\n{reply}"
        )
    else:
        user_msg = (
            f"【用户原始问题】\n{user_text}\n\n"
            f"【待审回复】\n{reply}"
        )

    def _parse_verdict(raw, reasoning, attempt_):
        """解析 verifier JSON 输出，返回 (verdict_str, flagged_str, checks_list)"""
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            # 旧格式兼容：PASS / REJECT 纯文本
            first = raw.splitlines()[0].strip().upper() if raw else ""
            if first == "REJECT":
                save_verifier_thinking(user_text, "reject", reasoning, attempt_)
                return "reject", raw, []
            if first == "PASS":
                save_verifier_thinking(user_text, "pass", reasoning, attempt_)
                return "pass", "", []
            save_verifier_thinking(user_text, "unknown(no_json)", reasoning, attempt_)
            return "unknown", "核查AI未返回可解析 JSON", []
        try:
            data = json.loads(m.group(0))
        except Exception:
            save_verifier_thinking(user_text, "unknown(json_err)", reasoning, attempt_)
            return "unknown", "核查AI JSON 解析失败", []
        checks = data.get("checks", [])
        if not isinstance(checks, list):
            checks = []
        parsed_checks = []
        flagged_lines = []
        for c in checks:
            if not isinstance(c, dict):
                continue
            status = str(c.get("status", "")).upper()
            if status not in ("SOFT", "HARD"):
                continue
            sentence = str(c.get("sentence", "")).strip()
            fact_id = c.get("fact_id")
            evidence_excerpt = c.get("evidence_excerpt")
            reason = str(c.get("reason", "")).strip()
            parsed_checks.append({
                "status": status,
                "sentence": sentence,
                "fact_id": fact_id,
                "evidence_excerpt": evidence_excerpt,
                "reason": reason,
            })
            tag = f"[{status}] "
            flagged_lines.append(
                f"- {tag}{sentence} | {fact_id if fact_id is not None else 'null'} | {reason}"
                + (f" | 原文：{evidence_excerpt}" if evidence_excerpt else "")
            )
        if parsed_checks:
            audit_str = "\n".join(flagged_lines)
            log.warning(f"❌ 审核退回\n{audit_str[:300]}")
            save_verifier_thinking(user_text, "reject", reasoning, attempt_)
            return "reject", audit_str, parsed_checks
        log.info("✅ 审核通过")
        save_verifier_thinking(user_text, "pass", reasoning, attempt_)
        return "pass", "", []

    def _call_verifier(timeout=60):
        return http_post(
            "https://api.deepseek.com/chat/completions",
            {"model": "deepseek-v4-flash",
             "messages": [
                 {"role": "system", "content": _SYS_VERIFIER},
                 {"role": "user",   "content": user_msg},
             ],
             "max_tokens": 8000,
             "temperature": 0.1,
             "thinking": {"type": "enabled", "budget_tokens": 1200}},
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_VERIFY_KEY}"},
            timeout=timeout,
        )

    try:
        resp = _call_verifier()
        if not resp or not resp.get("choices"):
            raise ValueError("空响应")
        reasoning = (resp["choices"][0]["message"].get("reasoning_content") or "").strip()
        out       = (resp["choices"][0]["message"].get("content") or "").strip()
        return _parse_verdict(out, reasoning, attempt)

    except Exception as e:
        log.warning(f"verify_reply 第1次异常: {e}，重试一次（切换 key）")
        from tg_bot.config import _next_verify_key
        _next_verify_key()
        try:
            resp2 = _call_verifier(timeout=60)
            if resp2 and resp2.get("choices"):
                reasoning2 = (resp2["choices"][0]["message"].get("reasoning_content") or "").strip()
                out2       = (resp2["choices"][0]["message"].get("content") or "").strip()
                return _parse_verdict(out2, reasoning2, attempt)
            save_verifier_thinking(user_text, "unknown(retry_fail)", "", attempt)
            return "unknown", "核查AI调用失败或超时", []
        except Exception as e2:
            log.warning(f"verify_reply 重试也失败: {e2}，标记 unknown")
            save_verifier_thinking(user_text, "unknown(exception)", "", attempt)
            return "unknown", "核查AI调用失败或超时", []


def patch_by_verifier(reply, exec_report, audit_report, user_text=""):
    """
    最后兜底：3次重写仍不通过，监管AI持完整执行报告亲自外科手术式修改。
    返回修改后文本，失败则返回 None。
    """
    import tg_bot.config as _cfg
    sys_prompt = (
        "你是「监管AI」，写作AI经过3次修改仍未改正，现在由你直接处理这篇文本。\n\n"
        "你持有完整事实清单和具体问题清单，请对回复做最小改动：\n"
        "① 有问题的来源归属词 → 删掉归属，保留事实内容\n"
        "   例：'TikTok上有博主推荐小苏打' → '小苏打效果不错'\n"
        "② 整段在事实清单里完全找不到依据的 → 整段删除\n"
        "③ 其余内容一字不动，严格保持原有格式和段落结构\n\n"
        "直接输出修改后的完整文本，不加任何说明、注释或前言。"
    )
    user_msg = (
        f"【事实清单】\n{exec_report}\n\n"
        f"【已确认的问题清单】\n{audit_report}\n\n"
        f"【需要修改的文本】\n{reply}"
    )
    try:
        resp = http_post(
            "https://api.deepseek.com/chat/completions",
            {"model": "deepseek-v4-flash",
             "messages": [
                 {"role": "system", "content": sys_prompt},
                 {"role": "user",   "content": user_msg},
             ],
             "max_tokens": 3000,
             "temperature": 0.1,
             "thinking": {"type": "enabled", "budget_tokens": 600}},
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_VERIFY_KEY}"},
            timeout=75,
        )
        if resp and resp.get("choices"):
            reasoning = (resp["choices"][0]["message"].get("reasoning_content") or "").strip()
            patched = (resp["choices"][0]["message"].get("content") or "").strip()
            if patched:
                log.info("🔧 监管AI直接修改完成")
                save_verifier_thinking(user_text, "patch", reasoning, attempt=99)
                return patched
    except Exception as e:
        log.warning(f"patch_by_verifier 失败: {e}")
    return None


def regenerate_reply(messages, system, exec_report, rejected_reply, audit_report, attempt):
    """
    重写调用（主 key）：把完整执行报告 + 逐条审核意见发给写作AI，让它对照修改。
    exec_report 包含所有工具调用结果，写作AI可以对照改写，不需要重新搜索。
    attempt: 第几次重写（1-based）
    """
    import tg_bot.config as _cfg
    urgency = "" if attempt < 3 else "这是最后一次机会，务必彻底改正，否则将由监管AI直接处理。"

    retry_msgs = list(messages) + [
        {"role": "assistant", "content": rejected_reply},
        {"role": "user", "content":
            f"【系统修改指令·第{attempt}次】{urgency}\n\n"

            "监管AI审核了你的回复，发现以下具体问题：\n\n"
            f"{audit_report}\n\n"

            "以下是你本轮的完整执行记录（来源索引、搜索内容、抓取原文均在其中，"
            "对照这份记录修改，不需要重新搜索）：\n\n"
            f"{exec_report}\n\n"

            "修改要求：\n"
            "① 逐条对照问题清单修改，一条都不能漏\n"
            "② 正文里出现了索引里没有的网站名 → 删掉来源归属词，事实内容可以保留\n"
            "③ 凭空编造、执行记录里找不到依据的整段 → 删除或替换为真实搜索到的内容\n"
            "④ 格式和段落结构保持原样\n"
            "⑤ 只输出最终干净正文——不写原文、不写草稿、不写'← 已删除'、不写'修正前/修正后'等任何对比标注；任何中间过程痕迹一律不出现\n"
            "⑥ 不要提及修改过程，不要说'根据系统指令'等话\n"
            "⑦ 只改被标注的问题句子，未被标注的段落一字不动——禁止改写、润色、重组正确内容"
        }
    ]
    msgs = [{"role": "system", "content": system}] + retry_msgs
    try:
        resp = http_post(
            "https://api.deepseek.com/chat/completions",
            {"model": "deepseek-v4-flash",
             "messages": msgs,
             "max_tokens": 3000,
             "temperature": 0.5,
             "thinking": {"type": "enabled", "budget_tokens": 500}},
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_KEY}"},
            timeout=90,
        )
        if resp and resp.get("choices"):
            new_text = (resp["choices"][0]["message"].get("content") or "").strip()
            if new_text:
                return new_text
    except Exception as e:
        log.warning(f"regenerate_reply 异常: {e}")
    return rejected_reply  # 万一失败，退回原稿
