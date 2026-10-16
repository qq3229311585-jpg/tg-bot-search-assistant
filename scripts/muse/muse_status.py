#!/usr/bin/env python3
"""服务器一页体检（只读，无参数）。时间一律用北京时间(UTC+8)。"""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
SERVICES = ["xray", "hysteria-server", "nginx", "tg-bot", "conan-kid", "textrpg", "vnstat", "ssh"]
STALE_HOURS = 30
ARCHIVE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
GIB = 1024 ** 3


def _bj(now=None):
    if now is None:
        return datetime.now(BJ)
    if now.tzinfo is None:
        return now.replace(tzinfo=BJ)
    return now.astimezone(BJ)


def service_states(names, run=subprocess.run):
    states = {}
    for name in names:
        try:
            proc = run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5)
            states[name] = proc.stdout.strip()
        except Exception:
            states[name] = "unknown"
    return states


def read_uptime(path="/proc/uptime"):
    try:
        with open(path, encoding="utf-8") as f:
            seconds = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return "未知"
    days, rem = divmod(int(seconds), 86400)
    return f"运行 {days} 天 {rem // 3600} 小时"


def read_load(path="/proc/loadavg"):
    try:
        with open(path, encoding="utf-8") as f:
            one, five, fifteen = f.read().split()[:3]
    except (OSError, ValueError):
        return "未知"
    return f"负载 {one} / {five} / {fifteen}(1/5/15 分钟)"


def read_mem(path="/proc/meminfo"):
    info = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    info[key] = int(rest.split()[0])
        total = info["MemTotal"]
        avail = info["MemAvailable"]
    except (OSError, ValueError, KeyError, IndexError):
        return "未知"
    if total <= 0:
        return "未知"
    used = total - avail
    return (f"内存 已用 {used / 1024 / 1024:.1f} GiB / 总量 {total / 1024 / 1024:.1f} GiB"
            f" ({used * 100 / total:.0f}%)")


def disk_usage(path="/"):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return "未知"
    pct = usage.used * 100 / usage.total if usage.total else 0
    return f"已用 {usage.used / GIB:.1f} GiB / 总 {usage.total / GIB:.1f} GiB ({pct:.0f}%)"


def report_freshness(directory="/srv/daily-report", now=None):
    now = _bj(now)
    latest = os.path.join(directory, "latest.md")
    try:
        mtime = os.path.getmtime(latest)
    except OSError:
        return ("还没有日报", True)
    made = datetime.fromtimestamp(mtime, BJ)
    age = max((now - made).total_seconds() / 3600, 0)
    try:
        archives = sum(
            1 for name in os.listdir(directory)
            if ARCHIVE_RE.match(name) and os.path.isfile(os.path.join(directory, name))
        )
    except OSError:
        archives = 0
    text = f"最新日报生成于 {made:%Y-%m-%d %H:%M}(北京时间),距今 {age:.1f} 小时,存档 {archives} 天"
    stale = age > STALE_HOURS
    if stale:
        text += "(已超过 30 小时，可能没有按时生成)"
    return text, stale


def build_report(now=None, deps=None):
    funcs = {
        "service_states": service_states,
        "read_uptime": read_uptime,
        "read_load": read_load,
        "read_mem": read_mem,
        "disk_usage": disk_usage,
        "report_freshness": report_freshness,
    }
    if deps:
        funcs.update(deps)
    now = _bj(now)

    states = funcs["service_states"](SERVICES)
    fresh_text, stale = funcs["report_freshness"](now=now)

    lines = [f"# 服务器状态 (北京时间 {now:%Y-%m-%d %H:%M})", "", "## 服务"]
    bad = False
    for name in SERVICES:
        state = states.get(name, "unknown")
        mark = "✓" if state == "active" else "✗"
        if mark == "✗":
            bad = True
        lines.append(f"- {mark} {name}: {state}")

    lines += ["", "## 日报", fresh_text, "", "## 资源"]
    lines.append(f"- {funcs['read_uptime']()}")
    lines.append(f"- {funcs['read_load']()}")
    lines.append(f"- {funcs['read_mem']()}")
    lines.append(f"- 磁盘 {funcs['disk_usage']()}")

    lines.append("")
    if bad or stale:
        lines.append("⚠ 有异常项,请告诉用户")
    else:
        lines.append("一切正常")
    return "\n".join(lines) + "\n"


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args:
        print("用法: status")
        return 2
    print(build_report(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
