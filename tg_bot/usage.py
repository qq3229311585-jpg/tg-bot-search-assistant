#!/usr/bin/env python3
"""usage.py — LLM token 用量与花费账本

所有 DeepSeek 调用都经过 ``tools.fetch.http_post``，那里会在拿到响应后调用
``record_llm_usage``，所以这里不需要改动任何 agent 就能统计每个环节的花费。

- 环节（stage）优先取 ``llm_stage()`` 上下文里的标签，没有时根据调用栈里最近的
  业务模块自动推断（如 ``agents.critic`` → ``critic``）。
- 账本按天写入 ``$TG_BOT_DATA_DIR/llm_usage/YYYY-MM-DD.jsonl``，一行一次调用。
- 单价可用环境变量覆盖（元 / 百万 token），默认值只是估算，用于横向对比优化前后。
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_BJ = timezone(timedelta(hours=8))
_stage_var: contextvars.ContextVar[str] = contextvars.ContextVar("llm_stage", default="")
_request_var: contextvars.ContextVar[str] = contextvars.ContextVar("llm_request", default="")
_lock = threading.Lock()

# 模块名 → 环节名；越靠前越优先
_MODULE_STAGES = (
    ("agents.query_fixer", "query_fixer"),
    ("agents.critic", "critic"),
    ("agents.patcher", "patcher"),
    ("agents.writer", "writer"),
    ("pipeline.disambig", "disambig"),
    ("pipeline.verify", "verify_legacy"),
    ("pipeline.write", "write_legacy"),
    ("core.pipeline", "length_fix"),
    ("features", "feature"),
    ("pipeline.gather", "gather"),
    ("tg_bot.gather", "gather"),
    ("tg_bot.verify", "verify_legacy"),
    ("tg_bot.write", "write_legacy"),
    ("tg_bot.bot", "chat"),
)


def _price(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def prices() -> dict:
    """元 / 百万 token。默认值为估算，部署时请按账单实际价格设置环境变量。"""
    return {
        "input_hit": _price("LLM_PRICE_INPUT_HIT", 0.2),
        "input_miss": _price("LLM_PRICE_INPUT_MISS", 2.0),
        "output": _price("LLM_PRICE_OUTPUT", 3.0),
    }


def _usage_dir() -> str:
    base = os.getenv("TG_BOT_DATA_DIR", "/var/lib/morning-report").strip() or "/var/lib/morning-report"
    return os.path.join(base, "llm_usage")


@contextlib.contextmanager
def llm_stage(name: str):
    """把一段代码里的模型调用都记到 ``name`` 环节下。"""
    token = _stage_var.set(name)
    try:
        yield
    finally:
        _stage_var.reset(token)


@contextlib.contextmanager
def llm_request(name: str):
    """标记一次用户请求（如 chat / daily_report / feature:quiz），便于按请求汇总。"""
    token = _request_var.set(name)
    try:
        yield
    finally:
        _request_var.reset(token)


def _infer_stage() -> str:
    stage = _stage_var.get()
    if stage:
        return stage
    frame = sys._getframe(2)
    depth = 0
    while frame is not None and depth < 25:
        name = frame.f_globals.get("__name__", "")
        if name.startswith("tg_bot") and not name.endswith(("tools.fetch", "usage")):
            for needle, label in _MODULE_STAGES:
                if needle in name:
                    if label == "gather" and frame.f_code.co_name in ("fast_chat", "summarize_for_context"):
                        return frame.f_code.co_name
                    return label
        frame = frame.f_back
        depth += 1
    return "other"


def estimate_cost(prompt_hit: int, prompt_miss: int, completion: int, price: dict | None = None) -> float:
    p = price or prices()
    return (prompt_hit * p["input_hit"] + prompt_miss * p["input_miss"] + completion * p["output"]) / 1_000_000


def record_llm_usage(payload: dict, response: dict) -> None:
    """从 DeepSeek 响应里提取 usage 并写账本。任何异常都吞掉，不能影响主流程。"""
    try:
        if os.getenv("LLM_USAGE_LEDGER", "true").strip().lower() in ("0", "false", "no", "off"):
            return
        usage = (response or {}).get("usage") or {}
        if not usage:
            return
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or max(prompt - hit, 0))
        reasoning = int(((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
        now = datetime.now(_BJ)
        row = {
            "ts": now.isoformat(timespec="seconds"),
            "stage": _infer_stage(),
            "request": _request_var.get() or "",
            "model": (payload or {}).get("model", ""),
            "prompt": prompt,
            "hit": hit,
            "miss": miss,
            "completion": completion,
            "reasoning": reasoning,
            "cost": round(estimate_cost(hit, miss, completion), 6),
        }
        path = os.path.join(_usage_dir(), now.strftime("%Y-%m-%d") + ".jsonl")
        with _lock:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # pragma: no cover - 账本永远不能拖垮主流程
        log.debug("usage ledger write failed: %s", exc)


def load_usage(days: int = 1, *, now: datetime | None = None) -> list[dict]:
    """读取最近 ``days`` 天（含今天）的账本行。"""
    now = now or datetime.now(_BJ)
    rows: list[dict] = []
    for offset in range(max(days, 1) - 1, -1, -1):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        path = os.path.join(_usage_dir(), day + ".jsonl")
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            continue
        except FileNotFoundError:
            continue
    return rows


def summarize_usage(rows: list[dict]) -> dict:
    """按环节汇总：调用次数、token、缓存命中率、估算花费。"""
    by_stage: dict[str, dict] = {}
    total = {"calls": 0, "prompt": 0, "hit": 0, "completion": 0, "cost": 0.0}
    for row in rows:
        stage = row.get("stage") or "other"
        bucket = by_stage.setdefault(stage, {"calls": 0, "prompt": 0, "hit": 0, "completion": 0, "cost": 0.0})
        for target in (bucket, total):
            target["calls"] += 1
            target["prompt"] += int(row.get("prompt") or 0)
            target["hit"] += int(row.get("hit") or 0)
            target["completion"] += int(row.get("completion") or 0)
            target["cost"] += float(row.get("cost") or 0.0)
    return {"total": total, "by_stage": dict(sorted(by_stage.items(), key=lambda kv: -kv[1]["cost"]))}
