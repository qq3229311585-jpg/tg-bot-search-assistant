#!/usr/bin/env python3
"""muse-history: 在日报存档 (/srv/daily-report/YYYY-MM-DD.md) 里按关键词搜索。仅用标准库。"""
import os
import re
import sys
import unicodedata

DEFAULT_DIR = "/srv/daily-report"
DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
USAGE = "用法: history <关键词>"
INVALID = "输入无效: 关键词长度须在 2-50 字之间,且不能含控制字符"
MAX_LINE_CHARS = 300


def _valid_keyword(kw):
    if not 2 <= len(kw) <= 50:
        return False
    # Cc 类别包含换行、制表符等所有控制字符
    return not any(unicodedata.category(ch) == "Cc" for ch in kw)


def _list_archive_files(directory):
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    files = []
    for name in names:
        if DATE_FILE_RE.match(name) and os.path.isfile(os.path.join(directory, name)):
            files.append(name)
    # 文件名是 YYYY-MM-DD，字典序即日期序；倒序即从新到旧
    files.sort(reverse=True)
    return files


def _clip(line):
    line = line.strip()
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS] + "…"
    return line


def search(keyword, directory=DEFAULT_DIR, max_per_file=5, max_total=40):
    kw = keyword.strip()
    if not _valid_keyword(kw):
        return INVALID

    files = _list_archive_files(directory)
    if not files:
        return "日报存档里还没有内容"

    needle = kw.casefold()
    blocks = []
    total_hits = 0
    truncated = False

    for name in files:
        try:
            with open(os.path.join(directory, name), encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue

        file_hits = []
        for line in lines:
            if needle not in line.casefold():
                continue
            if len(file_hits) >= max_per_file:
                break
            if total_hits >= max_total:
                truncated = True
                break
            file_hits.append(_clip(line))
            total_hits += 1

        if file_hits:
            blocks.append((name[:-3], file_hits))
        if truncated:
            break

    out = [f"在最近 {len(files)} 天的日报存档里搜索「{kw}」:共 {len(blocks)} 天提到"]
    if not blocks:
        out.append("没有找到")
    for day, hits in blocks:
        out.append(f"== {day} ==")
        out.extend(f"- {h}" for h in hits)
    if truncated:
        out.append("(结果已截断)")
    return "\n".join(out)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        print(USAGE)
        return 2
    keyword = args[0].strip()
    if not _valid_keyword(keyword):
        print(INVALID)
        return 2
    print(search(keyword))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
