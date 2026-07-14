#!/usr/bin/env python3
"""Telegram progress presentation for chat and search execution."""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional


log = logging.getLogger(__name__)


_ROUTE_LABELS = {
    "fast": "🧠 纯模型（知识库）",
    "search": "🔍 搜索路径",
    "local_tool": "🛠 本地工具",
    "report": "📋 读取日报",
    "system": "⚙️ 系统指令",
}

_STAGE_LABELS = {
    "query_fixer": ("🔧 查询改写中…", "🔧 改写"),
    "gather": ("🌐 搜索采集中…", "🌐 采集"),
    "curator": ("📋 整理素材中…", "📋 素材"),
    "writer": ("✍️ 生成正文中…", "✍️ 正文"),
    "critic": ("🧪 事实审核中…", "🧪 审核"),
    "patcher": ("🔩 小问题修复中…", "🔩 修复"),
}

_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class TelegramProgress:
    """Maintain one editable progress message plus three temporary tool items."""

    def __init__(
        self,
        chat_id: int,
        tg_func: Callable[[str, dict], Optional[dict]],
        *,
        enabled: bool = True,
        animate: bool = True,
        interval: float = 0.8,
        max_visible_items: int = 3,
    ) -> None:
        self.chat_id = chat_id
        self.tg_func = tg_func
        self.enabled = bool(enabled and chat_id)
        self.animate = bool(animate)
        self.interval = max(float(interval), 0.2)
        self.max_visible_items = max(int(max_visible_items), 1)

        self.message_id: Optional[int] = None
        self._lines: list[str] = []
        self._current = ""
        self._item_ids: list[int] = []
        self._frame = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _call(self, method: str, data: dict) -> Optional[dict]:
        if not self.enabled:
            return None
        try:
            return self.tg_func(method, data)
        except Exception as exc:
            log.debug("Telegram 进度操作失败（非致命）: %s", exc)
            return None

    def _render_locked(self) -> str:
        lines = list(self._lines)
        if self._current:
            frame = _SPINNER[self._frame % len(_SPINNER)]
            lines.append(f"{frame} {self._current}")
        return "\n".join(lines).strip()

    def _update(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            text = self._render_locked()
            if not text:
                return
            if self.message_id is None:
                response = self._call("sendMessage", {
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_notification": True,
                })
                if isinstance(response, dict) and response.get("ok"):
                    result = response.get("result") or {}
                    if isinstance(result, dict) and result.get("message_id") is not None:
                        self.message_id = int(result["message_id"])
                return
            self._call("editMessageText", {
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "text": text,
            })

    def _spin_loop(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                if not self._current:
                    continue
                self._frame += 1
            self._update()

    def start(self) -> None:
        if not self.enabled or not self.animate:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._spin_loop,
                daemon=True,
                name="telegram-progress",
            )
            self._thread.start()

    def _stop_animation(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def set_route(self, lane: str) -> None:
        if not self.enabled:
            return
        label = _ROUTE_LABELS.get(lane, f"🛣 {lane}")
        with self._lock:
            line = f"✓ {label}"
            if not self._lines or self._lines[0] != line:
                self._lines.insert(0, line)
        self._update()

    @staticmethod
    def _done_line(stage: str, detail: str) -> str:
        _, done_prefix = _STAGE_LABELS.get(stage, (stage, stage))
        if stage == "gather":
            return f"✓ {done_prefix} · {detail}条素材" if detail else f"✓ {done_prefix}"
        if stage == "query_fixer":
            return f"✓ {done_prefix} · {detail}" if detail else f"✓ {done_prefix}"
        if stage == "curator":
            return f"✓ {done_prefix} · {detail}" if detail else f"✓ {done_prefix}"
        if stage in ("writer", "patcher"):
            return f"✓ {done_prefix} · {detail}字" if detail else f"✓ {done_prefix}"
        if stage == "critic":
            icon = "✅" if detail == "pass" else ("🔧" if "patch" in detail else "⚠️")
            return f"✓ {done_prefix} · {icon} {detail}" if detail else f"✓ {done_prefix}"
        return f"✓ {done_prefix}" + (f" · {detail}" if detail else "")

    def _send_item(self, detail: str) -> None:
        if not self.enabled or not detail:
            return
        with self._lock:
            if len(self._item_ids) >= self.max_visible_items:
                old_message_id = self._item_ids.pop(0)
                self._call("deleteMessage", {
                    "chat_id": self.chat_id,
                    "message_id": old_message_id,
                })
            response = self._call("sendMessage", {
                "chat_id": self.chat_id,
                "text": detail,
                "disable_notification": True,
            })
            if isinstance(response, dict) and response.get("ok"):
                result = response.get("result") or {}
                if isinstance(result, dict) and result.get("message_id") is not None:
                    self._item_ids.append(int(result["message_id"]))

    def stage(self, stage: str, status: str, detail: str = "") -> None:
        if not self.enabled:
            return
        if stage == "gather_item":
            self._send_item(detail)
            return

        start_label, _ = _STAGE_LABELS.get(stage, (stage, stage))
        with self._lock:
            if status == "start":
                self._current = start_label
                self._frame += 1
            else:
                self._current = ""
                self._lines.append(self._done_line(stage, str(detail or "")))
        if status == "start":
            self.start()
        self._update()

    def sending(self) -> None:
        if not self.enabled:
            return
        self._stop_animation()
        with self._lock:
            self._current = "📤 正在发送中…"
        self._update()

    def _clear_items(self) -> None:
        with self._lock:
            item_ids = list(self._item_ids)
            self._item_ids.clear()
        for message_id in item_ids:
            self._call("deleteMessage", {
                "chat_id": self.chat_id,
                "message_id": message_id,
            })

    def complete(self) -> None:
        if not self.enabled:
            return
        self._stop_animation()
        with self._lock:
            self._current = ""
            if not self._lines or self._lines[-1] != "✓ 已发送":
                self._lines.append("✓ 已发送")
        self._update()
        self._clear_items()

    def fail(self, detail: str = "处理失败") -> None:
        if not self.enabled:
            return
        self._stop_animation()
        with self._lock:
            self._current = ""
            self._lines.append(f"⚠️ {detail}")
        self._update()
        self._clear_items()

    def close(self) -> None:
        self._stop_animation()
        self._clear_items()
