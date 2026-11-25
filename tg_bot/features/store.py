#!/usr/bin/env python3
"""features/store.py — 新功能共用的小型 JSON 状态存储（线程安全、原子写）。"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone

from tg_bot.file_io import atomic_write_json

BJ = timezone(timedelta(hours=8))
_lock = threading.RLock()


def now_bj() -> datetime:
    return datetime.now(BJ)


def data_path(name: str) -> str:
    from tg_bot.config import DATA_DIR
    return os.path.join(DATA_DIR, name)


def load_json(name: str, default):
    with _lock:
        try:
            with open(data_path(name), encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, type(default)) else default
        except Exception:
            return default


def save_json(name: str, value) -> None:
    with _lock:
        atomic_write_json(data_path(name), value)


def update_json(name: str, default, mutate):
    """读-改-写一次完成，mutate(value) 返回新的值（或原地修改后返回 None）。"""
    with _lock:
        value = load_json(name, default)
        result = mutate(value)
        value = value if result is None else result
        save_json(name, value)
        return value
