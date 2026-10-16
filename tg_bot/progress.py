#!/usr/bin/env python3
"""Telegram progress presentation for chat and search execution.

事件驱动设计：面板只在真实事件（路由选定、阶段开始/完成、采集到条目、
发送、完成）时更新，不做假动画。长阶段由一个低频计时器补充"已进行 Xs"
的真实耗时信息。回答发出后整个面板删除，聊天里只留问题和答案。
"""

from __future__ import annotations

import logging
import re
import threading
import time
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
    "evidence_audit": ("🔎 核对材料覆盖…", "🔎 材料覆盖"),
    "evidence_gap": ("🔍 补查缺失信息…", "🔍 补查"),
    "curator": ("📋 整理素材中…", "📋 素材"),
    "writer": ("✍️ 生成正文中…", "✍️ 正文"),
    "critic": ("🧪 事实审核中…", "🧪 审核"),
    "review_shard": ("🧩 分段核查中…", "🧩 分段核查"),
    "patcher": ("🔩 小问题修复中…", "🔩 修复"),
}

# 当前阶段运行超过该秒数后，在面板上补充真实耗时提示
_ELAPSED_SHOW_AFTER = 8.0
# 耗时提示的刷新周期（低频，只传递真实信息，不是动画）
_ELAPSED_TICK = 10.0


class TelegramProgress:
    """Maintain one editable, event-driven progress message."""

    def __init__(
        self,
        chat_id: int,
        tg_func: Callable[[str, dict], Optional[dict]],
        *,
        enabled: bool = True,
        animate: bool = True,
        interval: float = _ELAPSED_TICK,
        max_visible_items: int = 3,
    ) -> None:
        self.chat_id = chat_id
        self.tg_func = tg_func
        self.enabled = bool(enabled and chat_id)
        # animate 现在表示"是否启用低频耗时提示"，保持旧参数兼容
        self.animate = bool(animate)
        self.interval = max(float(interval), 5.0)
        self.max_visible_items = max(int(max_visible_items), 1)

        self.message_id: Optional[int] = None
        self._lines: list[str] = []
        self._current = ""
        self._current_item = ""
        self._shard_line_index: Optional[int] = None
        self._stage_started_at = 0.0
        self._started_at = time.monotonic()
        self._last_rendered = ""
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
            current = f"▸ {self._current}"
            elapsed = time.monotonic() - self._stage_started_at
            if elapsed >= _ELAPSED_SHOW_AFTER:
                current += f"（已 {int(elapsed)}s）"
            lines.append(current)
        if self._current_item:
            lines.append(f"└ {self._current_item}")
        return "\n".join(lines).strip()

    def _update(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            text = self._render_locked()
            if not text or text == self._last_rendered:
                return
            self._last_rendered = text
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

    def _tick_loop(self) -> None:
        """低频补充当前阶段的真实耗时；阶段静止时面板不动。"""
        while not self._stop.wait(self.interval):
            with self._lock:
                if not self._current:
                    continue
                if time.monotonic() - self._stage_started_at < _ELAPSED_SHOW_AFTER:
                    continue
            self._update()

    def start(self) -> None:
        if not self.enabled or not self.animate:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._tick_loop,
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
        if stage == "evidence_gap" and detail:
            return f"✓ {done_prefix} · {detail}"
        return f"✓ {done_prefix}" + (f" · {detail}" if detail else "")

    @staticmethod
    def _parse_shard_detail(detail: str) -> tuple[int, int]:
        match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", str(detail or ""))
        if not match:
            return 0, 0
        return int(match.group(1)), int(match.group(2))

    def _stage_review_shard(self, status: str, detail: str) -> None:
        """分段核查全程只占一行：运行中显示进度，完成后收成一条 ✓。"""
        done, total = self._parse_shard_detail(detail)
        with self._lock:
            if status == "start":
                self._current = (
                    f"🧩 分段核查中…（{detail}）" if detail else "🧩 分段核查中…"
                )
                self._stage_started_at = time.monotonic()
            elif total and done < total:
                # 中途某段完成：更新同一行的进度，不新增 ✓ 行
                self._current = f"🧩 分段核查中…（{done}/{total} 已完成）"
            else:
                self._current = ""
                final = f"✓ 🧩 分段核查（{total} 段）" if total else "✓ 🧩 分段核查"
                if self._shard_line_index is not None and self._shard_line_index < len(self._lines):
                    self._lines[self._shard_line_index] = final
                else:
                    self._lines.append(final)
                    self._shard_line_index = len(self._lines) - 1
        if status == "start":
            self.start()
        self._update()

    def stage(self, stage: str, status: str, detail: str = "") -> None:
        if not self.enabled:
            return
        if stage == "gather_item":
            # 采集条目折叠进面板本身（一次编辑），不再发送/删除独立消息
            with self._lock:
                self._current_item = str(detail or "")[:80]
            self._update()
            return
        if stage == "review_shard":
            self._stage_review_shard(status, str(detail or ""))
            return

        start_label, _ = _STAGE_LABELS.get(stage, (stage, stage))
        with self._lock:
            if status == "start":
                self._current = start_label
                self._current_item = ""
                self._stage_started_at = time.monotonic()
            else:
                self._current = ""
                self._current_item = ""
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
            self._current_item = ""
            self._stage_started_at = time.monotonic()
        self._update()

    def _clear_items(self) -> None:
        # 兼容旧接口：条目已折叠进主面板，无独立消息可清理
        with self._lock:
            self._current_item = ""

    def complete(self) -> None:
        """回答已发出：面板保留在聊天里，让人能回看每一步是怎么做的。"""
        if not self.enabled:
            return
        self._stop_animation()
        elapsed = int(time.monotonic() - self._started_at)
        with self._lock:
            self._current = ""
            self._current_item = ""
            if not self._lines or not self._lines[-1].startswith("✓ 已发送"):
                self._lines.append(f"✓ 已发送（共 {elapsed}s）")
        self._update()

    def fail(self, detail: str = "处理失败") -> None:
        if not self.enabled:
            return
        self._stop_animation()
        with self._lock:
            self._current = ""
            self._current_item = ""
            self._lines.append(f"⚠️ {detail}")
        self._update()

    def close(self) -> None:
        self._stop_animation()
        self._clear_items()
