#!/usr/bin/env python3
"""config.py — 所有常量、路径、密钥配置（密钥从环境变量读取）"""

import json, os, ssl
from tg_bot.file_io import atomic_write_json
from datetime import datetime, timezone, timedelta

def _require(key):
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"缺少环境变量 {key}，请检查 /etc/tg-bot.env")
    return v

def _getenv_list(*keys):
    return [v for k in keys if (v := os.getenv(k))]

BOT_TOKEN    = _require("BOT_TOKEN")
ALLOWED_CHAT = int(_require("ALLOWED_CHAT"))

# ── DeepSeek key 池 ──────────────────────────────────────────────────
# 角色分配：
#   写作 AI (ds_chat / regenerate_reply)  → DEEPSEEK_KEYS 轮换
#   核查 AI (verify_reply / patch_by_verifier) → DEEPSEEK_VERIFY_KEYS 轮换
# thinking 策略：
#   ① 意图消歧 (_pre_check)  — disabled（固定 JSON，对错一眼看出）
#   ② 采集 AI (ds_chat)      — enabled（工具选择溯源用）
#   ③ 写作 AI (ds_chat)      — enabled（幻觉溯源最关键）
#   ④ 核查 AI (verify_reply) — enabled（不开则决策不透明）
DEEPSEEK_KEYS = _getenv_list("DEEPSEEK_KEY_0", "DEEPSEEK_KEY_1")
_ds_key_idx = 0

DEEPSEEK_VERIFY_KEYS = _getenv_list("DEEPSEEK_VERIFY_KEY_0", "DEEPSEEK_VERIFY_KEY_1")
_ds_verify_idx = 0

# 兼容旧代码引用
DEEPSEEK_KEY        = DEEPSEEK_KEYS[0]
DEEPSEEK_VERIFY_KEY = DEEPSEEK_VERIFY_KEYS[0]

def _next_ds_key() -> str:
    """写作 AI：轮换到下一个 key（遇到 rate limit 时调用）"""
    global _ds_key_idx, DEEPSEEK_KEY
    _ds_key_idx = (_ds_key_idx + 1) % len(DEEPSEEK_KEYS)
    DEEPSEEK_KEY = DEEPSEEK_KEYS[_ds_key_idx]
    return DEEPSEEK_KEY

def _next_verify_key() -> str:
    """核查 AI：轮换到下一个 key"""
    global _ds_verify_idx, DEEPSEEK_VERIFY_KEY
    _ds_verify_idx = (_ds_verify_idx + 1) % len(DEEPSEEK_VERIFY_KEYS)
    DEEPSEEK_VERIFY_KEY = DEEPSEEK_VERIFY_KEYS[_ds_verify_idx]
    return DEEPSEEK_VERIFY_KEY

BRAVE_KEY    = _require("BRAVE_KEY")
TAVILY_KEYS  = _getenv_list("TAVILY_KEY_0", "TAVILY_KEY_1")
_tavily_idx  = 0

SERPER_KEYS  = _getenv_list("SERPER_KEY_0", "SERPER_KEY_1", "SERPER_KEY_2")
_serper_idx  = 0
SERPER_KEY   = SERPER_KEYS[0]

def _next_serper_key() -> str:
    global _serper_idx, SERPER_KEY
    _serper_idx = (_serper_idx + 1) % len(SERPER_KEYS)
    SERPER_KEY  = SERPER_KEYS[_serper_idx]
    return SERPER_KEY

# OpenHuman 记忆树写入（每轮对话自动 ingest 到 telegram-bot namespace）
OPENHUMAN_RPC_URL   = "http://127.0.0.1:7788/rpc"
OPENHUMAN_RPC_TOKEN = os.getenv("OPENHUMAN_RPC_TOKEN", "")
OPENHUMAN_NS        = "telegram-bot"

DATA_DIR     = "/var/lib/morning-report"
HISTORY_FILE = DATA_DIR + "/chat_history.json"
SUMMARY_FILE = DATA_DIR + "/chat_summary.json"
REPORT_FILE  = DATA_DIR + "/today_report.txt"
THINKING_FILE = DATA_DIR + "/thinking.json"
TOOLLOG_FILE  = DATA_DIR + "/tool_log.json"
CONTEXT_FILE  = DATA_DIR + "/context_summary.json"
FOCUS_FILE    = DATA_DIR + "/dialog_focus.json"
MAX_HISTORY  = 20
MAX_THINKING = 30
MAX_TOOLLOG  = 10
MAX_CONTEXT  = 3
QUOTA_FILE      = DATA_DIR + "/api_quota.json"
LIMITS_FILE     = DATA_DIR + "/api_limits.json"
SOURCES_DIR     = DATA_DIR + "/sources"
WORKLOG_DIR     = DATA_DIR + "/worklog"

# ── 每日记忆系统 ─────────────────────────────────────────────────
DAILY_LOGS_DIR      = DATA_DIR + "/daily_logs"
DAILY_SUMMARIES_DIR = DATA_DIR + "/daily_summaries"
USER_PROFILES_FILE  = DATA_DIR + "/user_profiles.json"
FEATURES_FILE       = DATA_DIR + "/features.md"
MAX_DAILY_LOGS_DAYS = 90
MAX_SOURCES_DAYS = 30
MAX_WORKLOG_DAYS = 30

_API_LIMITS_DEFAULT = {
    "tavily_0": 1000, "tavily_1": 1000,
    "brave": 1000, "serper": 7500,
}

def _load_api_limits():
    limits = dict(_API_LIMITS_DEFAULT)
    try:
        custom = json.loads(open(LIMITS_FILE, encoding="utf-8").read())
        limits.update(custom)
    except:
        pass
    return limits

def _save_api_limit(key, value):
    try:
        custom = json.loads(open(LIMITS_FILE, encoding="utf-8").read())
    except:
        custom = {}
    custom[key] = value
    os.makedirs(DATA_DIR, exist_ok=True)
    atomic_write_json(LIMITS_FILE, custom)

API_FREE_LIMITS = _load_api_limits()
_quota_warnings: list = []

ASK_API_PORT  = 7799
ASK_TOKEN_FILE = DATA_DIR + "/ask_api_token"

ANYANG_LAT = 36.0975
ANYANG_LON = 114.3923
WMO_ZH = {
    0:"晴",1:"基本晴",2:"多云",3:"阴",
    45:"雾",48:"冻雾",51:"小毛毛雨",53:"毛毛雨",55:"大毛毛雨",
    61:"小雨",63:"中雨",65:"大雨",71:"小雪",73:"中雪",75:"大雪",
    77:"冰粒",80:"阵雨",81:"中阵雨",82:"强阵雨",
    85:"阵雪",86:"强阵雪",95:"雷阵雨",96:"雷暴夹雹",99:"强雷暴夹雹"
}

_DIRECT_API_TOOLS = {"check_weather", "vps_traffic", "wikipedia_lookup", "github_trending"}
OFFSET_FILE = DATA_DIR + "/tg_offset.txt"
_ctx = ssl.create_default_context()
os.makedirs(DATA_DIR, exist_ok=True)
