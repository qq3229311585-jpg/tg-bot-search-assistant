#!/usr/bin/env python3
"""features/scheduler.py — 后台小调度器：提醒到点推送、话题追踪定时检查、预算提醒。"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

_started = threading.Event()


def run_once(chat_id, send, *, tick: int) -> None:
    from tg_bot.features.remind import pop_due
    for message in pop_due():
        send(chat_id, message)
    # 话题追踪每 10 分钟看一次是否有到期话题（真正的检索频率由 WATCH_INTERVAL_HOURS 控制）
    if tick % 20 == 0:
        from tg_bot.features.watch import check_watches
        for message in check_watches():
            send(chat_id, message)


def start(chat_id, send, *, interval: float = 30.0) -> None:
    if _started.is_set():
        return
    _started.set()

    def _loop():
        tick = 0
        while True:
            try:
                run_once(chat_id, send, tick=tick)
            except Exception as exc:
                log.warning("feature scheduler tick failed: %s", exc)
            tick += 1
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="feature-scheduler").start()
    log.info("⏰ 新功能调度器已启动（提醒 / 话题追踪）")
