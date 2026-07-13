#!/usr/bin/env python3
"""bot_utils.py — Telegram 基础工具函数（send/tg/typing/md_to_html）"""

import json, re, time, logging
from tg_bot.config import BOT_TOKEN, _ctx
from tg_bot.tools.fetch import http_get, http_post

log = logging.getLogger(__name__)


def tg(method, data=None, params=""):
    if data:
        return http_post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data)
    try:
        raw = http_get(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}?{params}")
        return json.loads(raw) if raw else None
    except:
        return None


def md_to_html(text):
    """把 DeepSeek 输出的 Markdown 转成 Telegram HTML，防止星号裸露。"""
    # 先转义 HTML 特殊字符（保留已有的 HTML 标签除外）
    # 这里采用保守策略：只转换 Markdown 格式符号，不动已有 HTML
    # 把非 Telegram 白名单的裸 HTML 标签转义（如 <文件名>、<参数> 等占位符）
    _TG_TAGS = {'b', '/b', 'i', '/i', 'u', '/u', 's', '/s',
                'code', '/code', 'pre', '/pre', 'a', '/a',
                'tg-spoiler', '/tg-spoiler', 'br'}
    def _escape_unknown_tag(m):
        tag = m.group(1).split()[0].lstrip('/')
        if tag.lower() in _TG_TAGS:
            return m.group(0)   # 白名单标签保留
        return m.group(0).replace('<', '&lt;').replace('>', '&gt;')
    text = re.sub(r'<([^>]+)>', _escape_unknown_tag, text)
    # 代码块（```...```）→ <code>
    text = re.sub(r'```[a-z]*\n?(.*?)```', lambda m: '<code>' + m.group(1).strip() + '</code>', text, flags=re.DOTALL)
    # 行内代码（`code`）→ <code>
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)
    # 标题（### ## #）→ 加粗
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    # 粗体（**text** 或 __text__）→ <b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text, flags=re.DOTALL)
    # 斜体（*text* 或 _text_，但不能和粗体冲突，先处理完粗体再来）
    text = re.sub(r'\*([^\*\n]+)\*', r'<i>\1</i>', text)
    # 分隔线（--- 或 ***）→ 横线
    text = re.sub(r'^[-*_]{3,}\s*$', '─' * 16, text, flags=re.MULTILINE)
    return text


def _send_chunk(chat_id, chunk):
    """发单块，HTML 失败自动降级纯文本。"""
    res = http_post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        {"chat_id": chat_id, "text": chunk,
         "parse_mode": "HTML", "disable_web_page_preview": True}
    )
    if res and res.get("ok"):
        return True
    log.warning(f"sendMessage HTML failed (res={res}), retrying as plain text")
    plain = re.sub(r"<[^>]+>", "", chunk)
    fallback = http_post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        {"chat_id": chat_id, "text": plain, "disable_web_page_preview": True}
    )
    return bool(fallback and fallback.get("ok"))


def _split_safe(text, limit=4000):
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = limit
        nl = text.rfind("\n", 0, cut)
        if nl > cut // 2:
            cut = nl + 1
        else:
            sp = text.rfind(" ", 0, cut)
            if sp > cut // 2:
                cut = sp + 1
        parts.append(text[:cut])
        text = text[cut:]
    return parts


def send(chat_id, text):
    if not text:
        return False
    text = md_to_html(text)
    chunks = _split_safe(text)
    delivered = True
    for i, chunk in enumerate(chunks):
        delivered = _send_chunk(chat_id, chunk) and delivered
        if i + 1 < len(chunks): time.sleep(0.3)
    return delivered


def typing(chat_id):
    tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
