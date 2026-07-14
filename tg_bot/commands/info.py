#!/usr/bin/env python3
"""commands/info.py — /thinking /tools /sources /worklog 命令处理函数"""

import json, logging
from collections import Counter
from datetime import datetime, timezone, timedelta

from tg_bot.config import THINKING_FILE, TOOLLOG_FILE, SOURCES_DIR, DAILY_SUMMARIES_DIR, DAILY_LOGS_DIR, USER_PROFILES_FILE
from tg_bot.bot_utils import send
from tg_bot.storage import (
    load_thinking, load_toollog, fmt_worklog,
    list_sources_files, read_sources_file,
)

log = logging.getLogger(__name__)


def _format_thinking_digest(th, tool_log=None, *, max_chars=2600):
    """Build a detailed execution chain without exposing verbatim reasoning."""
    th = th or []
    tool_log = tool_log or []
    latest_tool = tool_log[-1] if tool_log else {}
    last_user = str(latest_tool.get("user") or "").strip()
    if not last_user:
        last_user = next(
            (str(entry.get("user") or "").strip() for entry in reversed(th) if entry.get("user")),
            "",
        )
    if not last_user:
        return "暂无思考或执行记录。"

    related = [entry for entry in th if entry.get("user") == last_user]
    route_info = latest_tool.get("route_info") or {}
    route = str(route_info.get("route") or "").lower()
    if not route:
        route = "search" if any(entry.get("rounds") for entry in related) else "fast"
    route_label = {
        "search": "搜索回答",
        "fast": "快速回答",
    }.get(route, route or "未记录")

    timestamps = [str(entry.get("ts")) for entry in related if entry.get("ts")]
    if latest_tool.get("ts"):
        timestamps.append(str(latest_tool["ts"]))

    rounds = []
    tools = Counter()
    wrote_reply = False
    verifier_verdict = ""
    for entry in related:
        role = entry.get("role", "gather")
        if role == "gather" or "rounds" in entry:
            for round_ in entry.get("rounds") or []:
                names = [str(name) for name in (round_.get("tool_calls") or []) if name]
                tools.update(names)
                if names:
                    number = int(round_.get("round", len(rounds))) + 1
                    rounds.append((number, names))
        elif role == "write_ai":
            wrote_reply = True
        elif role == "verifier":
            verifier_verdict = str(entry.get("verdict") or verifier_verdict or "").lower()

    tool_details = [str(item) for item in (latest_tool.get("model_tools") or []) if item]
    if not tools and tool_details:
        tools.update(item.split("(", 1)[0] for item in tool_details)

    verify_status = str(latest_tool.get("verify_status") or verifier_verdict or "").lower()
    verdict_label = {
        "pass": "通过",
        "skip": "未核查",
        "unknown": "未知",
        "reject": "退回",
        "no_sources": "素材不足",
        "skip_no_tools_warned": "未搜索核查",
    }.get(verify_status)
    if not verdict_label:
        if verify_status.startswith("patched"):
            verdict_label = "修正后完成"
        elif verify_status.startswith("rewrite"):
            verdict_label = "重写后完成"
        elif verify_status:
            verdict_label = verify_status
        else:
            verdict_label = "未记录"

    out = [
        "🧠 最近一轮执行链",
        f"用户问：{last_user[:240]}",
        f"路线：{route_label}",
    ]
    reason = str(route_info.get("reason") or "").strip()
    if reason:
        out.append(f"原因：{reason[:180]}")
    if timestamps:
        unique_times = list(dict.fromkeys(timestamps))
        if len(unique_times) == 1:
            out.append(f"时间：{unique_times[0]}")
        else:
            out.append(f"时间：{unique_times[0]} ～ {unique_times[-1]}")

    out.append("")
    out.append("执行步骤：")
    step = 1
    if route == "fast":
        out.append(f"{step}. 快速回答：未调用搜索工具")
        step += 1
    else:
        for number, names in rounds:
            out.append(f"{step}. 采集第{number}轮：{'、'.join(names)}")
            step += 1
        if not rounds and tool_details:
            out.append(f"{step}. 采集：已调用 {len(tool_details)} 次工具")
            step += 1
        elif not rounds:
            out.append(f"{step}. 采集：未记录到有效工具调用")
            step += 1
    if wrote_reply or route == "search":
        out.append(f"{step}. 写作：已完成")
        step += 1
    if verify_status or verifier_verdict:
        out.append(f"{step}. 核查：{verdict_label}")

    if tools:
        out.append("")
        out.append("工具合计：" + "、".join(f"{name} × {count}" for name, count in tools.items()))
    if tool_details:
        out.append("工具明细：")
        out.extend(f"- {item[:180]}" for item in tool_details[:12])

    evidence_labels = {
        "source_index": "来源已索引",
        "fetched_pages": "正文已抓取",
        "search_tools": "搜索工具已调用",
        "read_today_report": "日报已读取",
        "vps_traffic": "VPS 数据已读取",
        "direct_tools": "直接工具已调用",
    }
    evidence = [
        evidence_labels.get(flag, str(flag))
        for flag in (latest_tool.get("evidence_flags") or [])
    ]
    if evidence:
        out.append("证据状态：" + "、".join(evidence))
    failed_urls = [str(url) for url in (latest_tool.get("failed_urls") or []) if url]
    if failed_urls:
        out.append("抓取失败：" + "、".join(failed_urls[:3]))
    out.append(f"核查结论：{verdict_label}")
    out.append("内部逐字思考不展示；这里保留的是可核验的执行、工具和证据链。")
    return "\n".join(out)[:max_chars]


def handle_thinking(chat_id):
    send(chat_id, _format_thinking_digest(load_thinking(), load_toollog()))


def _format_toollog_for_user(items, *, n=10, max_chars=3600):
    """Format tool execution metadata for users without reasoning previews."""
    items = (items or [])[-n:]
    if not items:
        return ""
    route_labels = {"search": "搜索回答", "fast": "快速回答"}
    evidence_labels = {
        "source_index": "来源已索引",
        "fetched_pages": "正文已抓取",
        "search_tools": "搜索工具已调用",
        "read_today_report": "日报已读取",
        "vps_traffic": "VPS 数据已读取",
        "direct_tools": "直接工具已调用",
    }
    verdict_labels = {
        "pass": "通过",
        "skip": "未核查",
        "unknown": "未知",
        "no_sources": "素材不足",
        "skip_no_tools_warned": "未搜索核查",
    }
    blocks = []
    for index, item in enumerate(items, 1):
        route_info = item.get("route_info") or {}
        route = str(route_info.get("route") or "")
        route_label = route_labels.get(route, route or "未记录")
        lines = [
            f"{index}. [{item.get('ts', '')}] {item.get('user', '')}",
            f"   路线：{route_label}",
        ]
        reason = str(route_info.get("reason") or "").strip()
        if reason:
            lines.append(f"   原因：{reason[:180]}")
        tools = [str(tool) for tool in (item.get("model_tools") or []) if tool]
        lines.append("   工具：" + (" → ".join(tools[:12]) if tools else "无"))
        evidence = [
            evidence_labels.get(flag, str(flag))
            for flag in (item.get("evidence_flags") or [])
        ]
        if evidence:
            lines.append("   证据：" + "、".join(evidence))
        verify_status = str(item.get("verify_status") or "")
        if verify_status:
            lines.append("   核查：" + verdict_labels.get(verify_status, verify_status))
        failed_urls = [str(url) for url in (item.get("failed_urls") or []) if url]
        if failed_urls:
            lines.append("   抓取失败：" + "、".join(failed_urls[:3]))
        reply_preview = str(item.get("reply_preview") or "").strip()
        if reply_preview:
            lines.append("   回复：" + reply_preview[:180])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)[:max_chars]


def handle_tools(chat_id):
    s = _format_toollog_for_user(load_toollog(), n=10)
    if s:
        send(chat_id, f"🛠 最近 10 轮工具使用：\n{s}")
    else:
        send(chat_id, "暂无工具使用记录。")


def handle_sources(chat_id):
    entries = list_sources_files(n=10)
    if not entries:
        send(chat_id, "📂 暂无来源存档（发起一次搜索后自动生成）。")
        return
    lines = ["📂 最近来源存档（/source 【文件名】 查看详情）\n"]
    for e in entries:
        lines.append(
            f"  {e['ts']}  {e['sources']}条来源/{e['fetched']}篇正文\n"
            f"  ❓ {e['user']}\n"
            f"  📄 {e['filename']}"
        )
    send(chat_id, "\n".join(lines))


def handle_source_detail(chat_id, text):
    parts_ = text[8:].strip().split()
    fname  = parts_[0] if parts_ else ""
    detail_arg = parts_[1] if len(parts_) > 1 else None
    if not fname.endswith(".json"):
        fname += ".json"
    # 解析 detail 参数：数字=第N条，full=全部正文，无=总览
    if detail_arg == "full":
        detail = "full"
    elif detail_arg and detail_arg.isdigit():
        detail = int(detail_arg)
    else:
        detail = None
    send(chat_id, read_sources_file(fname, detail=detail))


def handle_worklog(chat_id, text):
    date_arg = text[9:].strip() if text.startswith("/worklog ") else ""
    # 支持 /worklog、/worklog 20260517、/worklog yesterday
    if date_arg.lower() in ("yesterday", "昨天"):
        date_arg = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=1)).strftime("%Y%m%d")
    send(chat_id, fmt_worklog(date_arg))


def handle_diary(chat_id, text):
    """
    /diary 命令：查询每日总结和用户画像。
    用法：
      /diary            → 今天
      /diary 昨天       → 昨天
      /diary 2026-05-20 → 指定日期
      /diary list       → 列出所有记录日期
      /diary profile    → 最近7天用户画像
    """
    import os, json
    date_arg = text[7:].strip() if text.startswith("/diary ") else ""
    bj_now = datetime.now(timezone(timedelta(hours=8)))

    # ── list 模式 ─────────────────────────────────────────────────────
    if date_arg.lower() in ("list", "列表", "ls"):
        if not os.path.isdir(DAILY_SUMMARIES_DIR):
            send(chat_id, "📭 暂无任何日总结记录"); return
        files = sorted(
            [f[:-5] for f in os.listdir(DAILY_SUMMARIES_DIR) if f.endswith(".json")],
            reverse=True
        )
        if not files:
            send(chat_id, "📭 暂无任何日总结记录"); return
        lines = ["📅 已有日总结的日期："]
        for d in files[:20]:
            try:
                info = json.loads(open(os.path.join(DAILY_SUMMARIES_DIR, d + ".json"), encoding="utf-8").read())
                n = info.get("msg_count", 0)
                topics = "、".join(info.get("topics", [])[:3])
                lines.append(f"  {d}  ({n}条对话){('  ' + topics) if topics else ''}")
            except Exception:
                lines.append(f"  {d}")
        send(chat_id, "\n".join(lines)); return

    # ── profile 模式 ──────────────────────────────────────────────────
    if date_arg.lower() in ("profile", "画像", "用户画像"):
        try:
            profiles = json.loads(open(USER_PROFILES_FILE, encoding="utf-8").read())
        except Exception:
            send(chat_id, "📭 暂无用户画像记录"); return
        recent = [p for p in profiles if p.get("profile")][-7:]
        if not recent:
            send(chat_id, "📭 暂无用户画像记录"); return
        lines = ["🪞 最近用户画像汇总\n"]
        for p in reversed(recent):
            lines.append(f"━━ {p['date']} ━━")
            lines.append(p["profile"])
            lines.append("")
        send(chat_id, "\n".join(lines)); return

    # ── 日期解析 ──────────────────────────────────────────────────────
    if date_arg.lower() in ("yesterday", "昨天"):
        target = (bj_now - timedelta(days=1)).strftime("%Y-%m-%d")
    elif date_arg == "":
        target = bj_now.strftime("%Y-%m-%d")
    else:
        raw = date_arg.replace("-", "")
        if len(raw) == 8 and raw.isdigit():
            target = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        else:
            send(chat_id, "❓ 格式：YYYY-MM-DD，或 昨天 / list / profile"); return

    # ── 读取指定日期（总结 → 原始日志 → 无记录 三级回退） ────────────
    sum_path = os.path.join(DAILY_SUMMARIES_DIR, f"{target}.json")
    log_path = os.path.join(DAILY_LOGS_DIR, f"{target}.jsonl")

    if os.path.exists(sum_path):
        # ① 有总结
        try:
            d = json.loads(open(sum_path, encoding="utf-8").read())
        except Exception as e:
            send(chat_id, f"读取失败: {e}"); return
        parts = [f"📅 {target} 日总结"]
        if d.get("topics"):
            parts.append("话题：" + "、".join(d["topics"]))
        parts.append("")
        parts.append(d.get("summary", "（无总结内容）"))
        if d.get("profile"):
            parts.append("")
            parts.append("━━ 用户画像 ━━")
            parts.append(d["profile"])
        if d.get("generated"):
            parts.append("")
            parts.append(f"生成时间：{d['generated']}")
        send(chat_id, "\n".join(parts))

    elif os.path.exists(log_path):
        # ② 有原始日志，总结还没跑
        import json as _json
        msgs = []
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try: msgs.append(_json.loads(line))
                    except: pass
        lines = [f"📋 {target} 有原始对话（共 {len(msgs)} 条），日总结尚未生成"]
        for m in msgs[:20]:
            role = "用户" if m.get("role") == "user" else "AI"
            lines.append(f"[{m.get('ts','')}] {role}：{m.get('content','')[:200]}")
        if len(msgs) > 20:
            lines.append(f"…（仅显示前20条）")
        send(chat_id, "\n".join(lines))

    else:
        # ③ 什么都没有
        send(chat_id, f"📭 {target} 无任何对话记录（该日期早于系统上线，或当天未使用 bot）")
