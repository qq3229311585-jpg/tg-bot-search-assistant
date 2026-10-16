#!/usr/bin/env python3
"""muse-traffic: 只读流量汇总。仅标准库，只输出汇总数字，绝不输出网址/域名/IP。"""
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

DEFAULT_DB = "/var/lib/traffic-insight/insight.sqlite"
DEFAULT_LABELS = "/etc/traffic-insight/labels.json"
BJ_OFFSET = timedelta(hours=8)
HOUR_FMT = "%Y-%m-%d %H"
UNREADABLE = "流量数据暂时不可读"
VNSTAT_FAIL = "(vnstat 不可用)"
NOTE = "注:同一台设备若共用账号则无法区分;本报告不含访问的网站和来源地址。"
NIC_WINDOWS = ("today", "yesterday", "last7", "this_month", "last_month")
ACCOUNT_WINDOWS = ("today", "last7", "this_month")


def _to_naive_utc(now):
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None)
    return now


def period_bounds(now_utc):
    """北京时间日历边界，换算回 UTC（naive datetime）。last7 含今天共 7 天。"""
    bj = now_utc + BJ_OFFSET
    today0 = bj.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = today0.replace(day=1)
    last_month0 = (month0 - timedelta(days=1)).replace(day=1)

    def back(d):
        return d - BJ_OFFSET

    return {
        "today": back(today0),
        "yesterday": back(today0 - timedelta(days=1)),
        "last7": back(today0 - timedelta(days=6)),
        "this_month": back(month0),
        "last_month": back(last_month0),
    }


def _windows(now_utc):
    """每个窗口为 (起点, 终点)，终点 None 表示不设上限。"""
    b = period_bounds(now_utc)
    return {
        "today": (b["today"], None),
        "yesterday": (b["yesterday"], b["today"]),
        "last7": (b["last7"], None),
        "this_month": (b["this_month"], None),
        "last_month": (b["last_month"], b["this_month"]),
    }


def _hour_dt(text):
    try:
        return datetime.strptime(str(text), HOUR_FMT)
    except (TypeError, ValueError):
        return None


def _in_window(dt, window):
    if dt is None:
        return False
    start, end = window
    return dt >= start and (end is None or dt < end)


def _connect(db_path):
    # 库是 WAL 模式,只读账号无权写 -shm 文件,普通只读方式会报错;
    # immutable=1 只读主文件,不创建任何文件(数据最多落后约 5 分钟)。
    uri = "file:" + quote(os.path.abspath(db_path)) + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _query(db_path, sql, params):
    conn = _connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def nic_totals(db, now_utc):
    now = _to_naive_utc(now_utc)
    wins = _windows(now)
    lo = min(start for start, _ in wins.values()).strftime(HOUR_FMT)
    rows = _query(db, "SELECT hour, rx, tx FROM hourly_nic WHERE hour >= ?", (lo,))
    out = {k: 0 for k in NIC_WINDOWS}
    for hour, rx, tx in rows:
        dt = _hour_dt(hour)
        if dt is None:
            continue
        for key in NIC_WINDOWS:
            if _in_window(dt, wins[key]):
                out[key] += (rx or 0) + (tx or 0)
    return out


def account_totals(db, now_utc, labels):
    now = _to_naive_utc(now_utc)
    wins = _windows(now)
    lo = min(wins[k][0] for k in ACCOUNT_WINDOWS).strftime(HOUR_FMT)
    rows = _query(
        db,
        "SELECT hour, metric, bytes FROM hourly_xray WHERE metric LIKE 'user>>>%' AND hour >= ?",
        (lo,),
    )
    alias = {}
    if isinstance(labels, dict) and isinstance(labels.get("email"), dict):
        alias = labels["email"]

    acc = {}
    for hour, metric, nbytes in rows:
        parts = str(metric or "").split(">>>")
        if len(parts) < 2 or parts[0] != "user" or not parts[1]:
            continue
        dt = _hour_dt(hour)
        if dt is None:
            continue
        email = parts[1]
        item = acc.setdefault(email, {"today": 0, "last7": 0, "month": 0})
        size = nbytes or 0
        if _in_window(dt, wins["today"]):
            item["today"] += size
        if _in_window(dt, wins["last7"]):
            item["last7"] += size
        if _in_window(dt, wins["this_month"]):
            item["month"] += size

    result = []
    for email, item in acc.items():
        if not any(item.values()):
            continue
        name = alias.get(email) or email.split("@", 1)[0]
        result.append({"name": str(name), "today": item["today"],
                       "last7": item["last7"], "month": item["month"]})
    result.sort(key=lambda r: r["month"], reverse=True)
    return result


def _gib(n):
    return f"{(n or 0) / 2**30:.2f}"


def fmt_bytes(n):
    n = n or 0
    if n >= 2**30:
        return f"{n / 2**30:.2f} GiB"
    return f"{n / 2**20:.2f} MiB"


def vnstat_months(run=subprocess.run):
    try:
        result = run(["vnstat", "--json", "m", "3"], capture_output=True, text=True, timeout=10)
        if getattr(result, "returncode", 0) != 0:
            return VNSTAT_FAIL
        data = json.loads(result.stdout)
        months = data["interfaces"][0]["traffic"]["month"]
        items = []
        for m in months:
            date = m["date"]
            items.append((int(date["year"]), int(date["month"]), int(m["rx"]), int(m["tx"])))
        items.sort()
        items = items[-3:]
        if not items:
            return VNSTAT_FAIL
        lines = []
        for year, month, rx, tx in items:
            total = rx + tx
            lines.append(f"{year:04d}-{month:02d}: 总计 {_gib(total)} GiB "
                         f"(收 {_gib(rx)} / 发 {_gib(tx)})")
        return "\n".join(lines)
    except Exception:
        return VNSTAT_FAIL


def load_labels(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _scrub(text):
    """兜底：去掉可能混入别名中的网址、IP 和域名痕迹。"""
    text = re.sub(r"https?://\S*", "", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "", text)
    text = re.sub(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", "", text)
    text = text.strip()
    return text or "(未命名)"


def build_report(now=None, db_path=DEFAULT_DB, labels_path=DEFAULT_LABELS, deps=None):
    deps = deps or {}
    now_utc = _to_naive_utc(now)
    try:
        nic = nic_totals(db_path, now_utc)
        accounts = account_totals(db_path, now_utc, load_labels(labels_path))
    except (sqlite3.Error, OSError, ValueError):
        return UNREADABLE

    vn = vnstat_months(deps.get("run", subprocess.run))
    bj = now_utc + BJ_OFFSET
    lines = [
        f"# 流量汇总 (北京时间 {bj:%Y-%m-%d %H:%M})",
        "",
        "## 整机(网卡口径,上行+下行)",
        f"- 今天: {fmt_bytes(nic['today'])}",
        f"- 昨天: {fmt_bytes(nic['yesterday'])}",
        f"- 近7天: {fmt_bytes(nic['last7'])}",
        f"- 本月: {fmt_bytes(nic['this_month'])}",
        f"- 上月: {fmt_bytes(nic['last_month']) if nic['last_month'] else '无记录(本库只保存近期,上月请看下面的月度计费口径)'}",
        "",
        "## 月度计费口径(vnstat)",
        vn,
        "",
        "## 按账号(Xray)",
    ]
    if accounts:
        for acc in accounts[:8]:
            lines.append(f"- {_scrub(acc['name'])}: 今天 {fmt_bytes(acc['today'])}, "
                         f"近7天 {fmt_bytes(acc['last7'])}, 本月 {fmt_bytes(acc['month'])}")
    else:
        lines.append("- (暂无账号数据)")
    lines.append("")
    lines.append(NOTE)
    return "\n".join(lines)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args:
        print("用法: traffic")
        return 2
    print(build_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
