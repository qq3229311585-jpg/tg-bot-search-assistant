#!/usr/bin/env python3
"""commands/info.py — /thinking /tools /sources /worklog 命令处理函数"""

import json, logging
from datetime import datetime, timezone, timedelta

from tg_bot.config import THINKING_FILE, TOOLLOG_FILE, SOURCES_DIR, DAILY_SUMMARIES_DIR, DAILY_LOGS_DIR, USER_PROFILES_FILE
from tg_bot.bot_utils import send
from tg_bot.storage import (
    load_thinking, fmt_toollog_for_prompt, fmt_worklog,
    list_sources_files, read_sources_file,
)

log = logging.getLogger(__name__)


def handle_thinking(chat_id):
    th = load_thinking()
    if not th:
        send(chat_id, "暂无思考记录。")
        return

    # 始终取最新一条问题的思考记录，快速路径也显示，不过滤
    seen_users = []
    for entry in reversed(th):
        u = entry.get("user")
        if u and u not in seen_users:
            seen_users.append(u)
    if not seen_users:
        send(chat_id, "暂无思考记录。"); return
    last_user = seen_users[0]

    related = [e for e in th if e.get("user") == last_user]
    out = [f"🧠 最近一轮思考记录\n用户问: {last_user}\n"]

    for entry in related:
        role = entry.get("role", "gather")
        ts   = entry.get("ts", "")

        if role == "gather" or "rounds" in entry:
            # 采集AI：多轮 reasoning
            rounds = entry.get("rounds", [])
            if rounds:
                out.append(f"\n【采集AI】{ts}")
                for r in rounds:
                    if r.get("reasoning") or r.get("tool_calls"):
                        out.append(f"  第{r['round']+1}轮 | 工具: {'、'.join(r.get('tool_calls',[])) or '无'}")
                        if r.get("reasoning"):
                            out.append(f"  思考: {r['reasoning'][:600]}")

        elif role == "write_ai":
            # 写作AI
            reasoning = entry.get("reasoning", "")
            if reasoning:
                out.append(f"\n【写作AI】{ts}")
                out.append(f"  {reasoning[:800]}")

        elif role == "verifier":
            # 核查AI
            reasoning = entry.get("reasoning", "")
            verdict   = entry.get("verdict", "")
            if reasoning or verdict:
                out.append(f"\n【核查AI】{ts}  结论: {verdict}")
                if reasoning:
                    out.append(f"  {reasoning[:600]}")

    send(chat_id, "\n".join(out))


def handle_tools(chat_id):
    s = fmt_toollog_for_prompt(n=10)
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
