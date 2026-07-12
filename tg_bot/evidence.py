#!/usr/bin/env python3
"""evidence.py — local evidence helpers for non-search paths."""

import re
from datetime import datetime, timezone, timedelta

from tg_bot.facts import build_facts_json
from tg_bot.storage import load_report
from tg_bot.tools.native import execute_vps_traffic


_VPS_TRAFFIC_RE = re.compile(
    r"(流量|带宽|vps|服务器|network|traffic|用量|消耗|使用状况|使用量|今日|昨天|本月|近7天)",
    re.IGNORECASE,
)
_TODAY_REPORT_RE = re.compile(
    r"(午报|今日报告|今天报告|日报|早报|read_today_report|天气|汇率|行情速报|AI速报|代理圈|GitHub热榜|冷知识)",
    re.IGNORECASE,
)


def should_use_vps_traffic(text, pre=None, route_info=None):
    """Return True when the user is asking about the server/VPS traffic state."""
    text = (text or "").strip()
    route_info = route_info or {}
    query_type = str((pre or {}).get("query_type", ""))
    if query_type == "系统查询":
        return bool(_VPS_TRAFFIC_RE.search(text))
    if route_info.get("route") == "search":
        return False
    return bool(_VPS_TRAFFIC_RE.search(text))


def should_use_today_report(text, pre=None, route_info=None):
    """Return True when the user is asking about today's saved report."""
    text = (text or "").strip()
    route_info = route_info or {}
    if route_info.get("route") == "search":
        return False
    if not _TODAY_REPORT_RE.search(text):
        return False
    query_type = str((pre or {}).get("query_type", ""))
    if query_type in ("系统查询", "其他", "闲聊", ""):
        return True
    return any(k in text for k in ("午报", "今日报告", "今天报告", "read_today_report"))


def _pick_line(raw, *needles):
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if all(n in s for n in needles):
            return s
    return ""


def build_vps_traffic_pack(user_text):
    """Collect local VPS traffic evidence and build a mini fact sheet."""
    raw = execute_vps_traffic()
    bj_now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    month_line = _pick_line(raw, "本月")
    day_line = _pick_line(raw, "今日")
    week_line = _pick_line(raw, "近7天")
    conn_line = _pick_line(raw, "当前 TCP")
    load_line = _pick_line(raw, "负载：")

    facts = []
    if month_line:
        facts.append(("VPS 月度流量", month_line))
    if day_line:
        facts.append(("VPS 今日流量", day_line))
    if week_line:
        facts.append(("VPS 近7天流量", week_line))
    if conn_line:
        facts.append(("VPS 当前连接数", conn_line))
    if load_line:
        facts.append(("VPS 系统负载与内存", load_line))

    if not facts:
        facts.append(("VPS 流量状态", raw.splitlines()[0] if raw else "流量信息获取失败"))
    facts.append(("VPS 数据范围说明", "当前工具仅返回本月/今日/近7天与实时接口状态，没有按“昨天”单独统计的历史汇总"))

    fact_lines = [
        "═══ 事实清单 ═══",
        f"用户问题：{user_text}",
        f"采集时间：{bj_now}",
        "",
        "【直接API来源】",
    ]
    for idx, (title, body) in enumerate(facts, 1):
        fnum = f"F{idx:03d}"
        excerpt = body[:160].replace('"', "“")
        fact_lines.append(f"[{fnum}] {title}")
        fact_lines.append("       来源：local://vps_traffic（本地工具）")
        fact_lines.append(f'       原文片段："{excerpt}"')
        fact_lines.append("")
    fact_lines.append("═══ 清单结束 ═══")
    fact_list = "\n".join(fact_lines)

    source_index = [{
        "id": "LOCAL_VPS_TRAFFIC",
        "tool": "vps_traffic",
        "query": user_text[:40],
        "title": "VPS 流量状态",
        "url": "local://vps_traffic",
        "domain": "local://vps_traffic",
        "snippet": raw[:600],
        "full_content": raw,
    }]
    facts_json = build_facts_json(fact_list, source_index)

    return {
        "reference_mode": "evidence_backed",
        "evidence_flags": ["vps_traffic"],
        "tool_calls_summary": ["vps_traffic"],
        "tool_results": [{"tool": "vps_traffic", "query": user_text[:40], "snippet": raw[:400]}],
        "source_index": source_index,
        "fetched_pages": [],
        "facts_json": facts_json,
        "fact_list": fact_list,
        "raw": raw,
        "rounds": [{
            "round": 0,
            "role": "local_tool",
            "reasoning": "调用 vps_traffic 获取本地流量状态",
            "tool_calls": ["vps_traffic"],
            "content_preview": raw[:300],
        }],
    }


def _report_section_summary(report):
    """Extract a compact section status list for verifier-friendly facts."""
    section_patterns = [
        ("天气预报", r"(天气预报|天气)"),
        ("汇率", r"(汇率|USD/CNY|美元|人民币)"),
        ("行情速览", r"(行情速览|BTC|ETH|比特币|以太坊)"),
        ("中国/全球要闻", r"(中国要闻|全球要闻|重大新闻|今日要闻)"),
        ("AI 速报", r"(AI速报|AI 速报|人工智能速报|人工智能)"),
        ("代理圈动态", r"(代理圈动态|sing-box|Xray|代理)"),
        ("圈子热议", r"(圈子在聊|圈子热议|HackerNews|HN)"),
        ("GitHub 热榜", r"(GitHub热榜|GitHub 热榜|star|仓库)"),
        ("Steam 降价优惠", r"(Steam 降价优惠|Steam优惠|Steam折扣|Steam)"),
        ("今日冷知识", r"(今日冷知识|冷知识)"),
    ]
    lines = [l.strip() for l in (report or "").splitlines() if l.strip()]
    joined = "\n".join(lines)
    results = []
    for title, pat in section_patterns:
        m = re.search(pat, joined, re.IGNORECASE)
        if not m:
            results.append((title, "未在今日午报文本中找到该板块标题或相关内容"))
            continue
        start = max(0, m.start() - 80)
        end = min(len(joined), m.end() + 220)
        excerpt = joined[start:end].replace("\n", " ")
        results.append((title, excerpt[:240]))
    return results


def build_today_report_pack(user_text, report_text=None):
    """Read today's report as local evidence and build a mini fact sheet."""
    raw = report_text if report_text is not None else load_report()
    raw = (raw or "").strip()
    if not raw:
        raw = "今日午报尚未生成或为空。"
    bj_now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    facts = [
        ("今日午报读取状态", "已调用 read_today_report 本地工具读取 /var/lib/morning-report/today_report.txt"),
        ("今日午报全文概览", raw[:360]),
    ]
    facts.extend(_report_section_summary(raw))

    fact_lines = [
        "═══ 事实清单 ═══",
        f"用户问题：{user_text}",
        f"采集时间：{bj_now}",
        "",
        "【本地报告来源】",
    ]
    for idx, (title, body) in enumerate(facts, 1):
        fnum = f"F{idx:03d}"
        excerpt = (body or "")[:220].replace('"', "“")
        fact_lines.append(f"[{fnum}] {title}")
        fact_lines.append("       来源：local://read_today_report（本地工具）")
        fact_lines.append(f'       原文片段："{excerpt}"')
        fact_lines.append("")
    fact_lines.append("═══ 清单结束 ═══")
    fact_list = "\n".join(fact_lines)

    derived_status = "\n".join(f"{title}: {body}" for title, body in facts)
    source_content = (
        "工具：read_today_report\n"
        "读取文件：/var/lib/morning-report/today_report.txt\n\n"
        "【板块扫描结果】\n"
        f"{derived_status}\n\n"
        "【午报原文】\n"
        f"{raw}"
    )
    source_index = [{
        "id": "LOCAL_TODAY_REPORT",
        "tool": "read_today_report",
        "query": user_text[:40],
        "title": "今日午报全文",
        "url": "local://read_today_report",
        "domain": "local://read_today_report",
        "snippet": raw[:600],
        "full_content": source_content,
    }]
    facts_json = build_facts_json(fact_list, source_index)

    return {
        "reference_mode": "evidence_backed",
        "reply_mode": "report",
        "evidence_flags": ["read_today_report"],
        "tool_calls_summary": ["read_today_report"],
        "tool_results": [{"tool": "read_today_report", "query": user_text[:40], "snippet": raw[:400]}],
        "source_index": source_index,
        "fetched_pages": [],
        "facts_json": facts_json,
        "fact_list": fact_list,
        "raw": raw,
        "rounds": [{
            "round": 0,
            "role": "local_tool",
            "reasoning": "调用 read_today_report 读取今日午报全文",
            "tool_calls": ["read_today_report"],
            "content_preview": raw[:300],
        }],
    }
