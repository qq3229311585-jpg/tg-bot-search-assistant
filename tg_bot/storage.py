#!/usr/bin/env python3
"""storage.py — 所有文件读写函数"""

import json, os, logging, threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

from tg_bot.config import (
    HISTORY_FILE, SUMMARY_FILE, REPORT_FILE, DAILY_REPORT_STATE_FILE, THINKING_FILE, TOOLLOG_FILE,
    CONTEXT_FILE, FOCUS_FILE, QUOTA_FILE, LIMITS_FILE, SOURCES_DIR, WORKLOG_DIR,
    MAX_HISTORY, MAX_THINKING, MAX_TOOLLOG, MAX_CONTEXT,
    MAX_SOURCES_DAYS, MAX_WORKLOG_DAYS,
    _API_LIMITS_DEFAULT, API_FREE_LIMITS, _quota_warnings,
    DATA_DIR,
)

from tg_bot.file_io import atomic_write_json, atomic_write_text

log = logging.getLogger(__name__)
_quota_lock = threading.Lock()


@contextmanager
def _quota_file_lock():
    """Coordinate quota read/modify/write across bot and report processes."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - deployment target is Unix
        yield
        return
    lock_path = f"{QUOTA_FILE}.lock"
    parent = os.path.dirname(lock_path)
    try:
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ── 对话历史 ──────────────────────────────────────────────────────────
def load_history():
    try: return json.loads(open(HISTORY_FILE, encoding="utf-8").read())
    except Exception as e:
        log.debug(f"load_history: {e}")
        return []

def save_history(h):
    atomic_write_json(HISTORY_FILE, h)

def load_summary():
    try: return open(SUMMARY_FILE, encoding="utf-8").read().strip()
    except Exception as e:
        log.debug(f"load_summary: {e}")
        return ""

def save_summary(s):
    atomic_write_text(SUMMARY_FILE, s)

def load_report():
    try: return open(REPORT_FILE, encoding="utf-8").read().strip()
    except Exception as e:
        log.debug(f"load_report: {e}")
        return ""


def _empty_daily_report_state():
    return {"schema_version": 1, "events": {}}


def load_daily_report_state(path=None):
    """Load versioned daily-report history, recovering safely from corruption."""
    path = path or DAILY_REPORT_STATE_FILE
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict) or not isinstance(state.get("events"), dict):
            raise ValueError("daily report state must contain an events object")
        state["schema_version"] = int(state.get("schema_version", 1))
        return state
    except FileNotFoundError:
        return _empty_daily_report_state()
    except Exception as exc:
        backup = f"{path}.corrupt.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        try:
            os.replace(path, backup)
        except OSError:
            log.warning("无法备份损坏的日报状态文件 %s: %s", path, exc)
        else:
            log.warning("日报状态文件损坏，已备份到 %s: %s", backup, exc)
        return _empty_daily_report_state()


def save_daily_report_state(state, path=None):
    """Atomically persist the daily-report event history."""
    path = path or DAILY_REPORT_STATE_FILE
    payload = dict(state or {})
    payload["schema_version"] = 1
    payload["events"] = dict(payload.get("events") or {})
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    atomic_write_json(path, payload)


# ── 对话上文摘要（消歧用）────────────────────────────────────────────

# ── 对话焦点（Dialog Focus）─────────────────────────────────────────
def load_focus():
    """Return current pending dialog focus, or {} if none."""
    try:
        return json.loads(open(FOCUS_FILE, encoding="utf-8").read())
    except Exception:
        return {}

def save_focus(focus: dict):
    """Persist dialog focus state."""
    import time as _t
    focus["updated_ts"] = _t.time()
    atomic_write_json(FOCUS_FILE, focus)

def clear_focus():
    """Clear focus after task completion."""
    atomic_write_json(FOCUS_FILE, {})

# ─────────────────────────────────────────────────────────────────────
def load_context():
    """返回最近 MAX_CONTEXT 轮的对话摘要列表 [{user, assistant}, ...]"""
    try: return json.loads(open(CONTEXT_FILE, encoding="utf-8").read())
    except Exception as e:
        log.debug(f"load_context: {e}")
        return []

def save_context(user_text, assistant_summary):
    """追加一轮记录,超出 MAX_CONTEXT 时滚动丢弃最旧的."""
    items = load_context()
    import time as _time
    items.append({"user": user_text[:200], "assistant": assistant_summary[:60], "ts": _time.time()})
    items = items[-MAX_CONTEXT:]
    try:
        atomic_write_json(CONTEXT_FILE, items)
    except Exception as e:
        log.warning(f"save_context 失败: {e}")


# ── 思考日志 ──────────────────────────────────────────────────────────
def load_thinking():
    try: return json.loads(open(THINKING_FILE, encoding="utf-8").read())
    except Exception as e:
        log.debug(f"load_thinking: {e}")
        return []

def save_thinking_entry(user_text, rounds):
    """rounds = [{round, reasoning, content, tool_calls}, ...]"""
    log_list = load_thinking()
    bj_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    log_list.append({"ts": bj_str, "user": user_text[:200], "rounds": rounds})
    log_list = log_list[-MAX_THINKING:]
    try:
        atomic_write_json(THINKING_FILE, log_list)
    except Exception as e:
        log.warning(f"thinking 写入失败: {e}")


def save_write_thinking(user_text, reasoning):
    """
    写作 AI 的 reasoning_content 存档（每轮写作后调用）.
    reasoning: deepseek 返回的 reasoning_content 字符串.
    """
    if not reasoning:
        return
    log_list = load_thinking()
    bj_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    log_list.append({
        "ts": bj_str,
        "role": "write_ai",
        "user": user_text[:200],
        "reasoning": reasoning[:3000],
    })
    log_list = log_list[-MAX_THINKING:]
    try:
        atomic_write_json(THINKING_FILE, log_list)
    except Exception as e:
        log.warning(f"write thinking 写入失败: {e}")

def save_verifier_thinking(user_text, verdict, reasoning, attempt=0):
    """
    核查 AI 的 thinking 存档.
    verdict: "pass" / "reject"
    attempt: 0=首次核查,1/2/3=第N次重写后的核查,99=patch
    """
    log_list = load_thinking()
    bj_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    log_list.append({
        "ts": bj_str,
        "role": "verifier",
        "user": user_text[:200],
        "verdict": verdict,
        "attempt": attempt,
        "reasoning": reasoning[:3000] if reasoning else "",
    })
    log_list = log_list[-MAX_THINKING:]
    try:
        atomic_write_json(THINKING_FILE, log_list)
    except Exception as e:
        log.warning(f"verifier thinking 写入失败: {e}")


# ── 工具使用记录 ─────────────────────────────────────────────────────
def load_toollog():
    try: return json.loads(open(TOOLLOG_FILE, encoding="utf-8").read())
    except Exception as e:
        log.debug(f"load_toollog: {e}")
        return []

def save_toollog_entry(user_text, pre_searched, model_tools, confidence=None,
                       reply_preview="", reasoning_preview="", search_snippets=None,
                       route_info=None, verify_status="", reference_mode="",
                       evidence_flags=None, failed_urls=None):
    """记录本轮做了哪些工具操作,以及本轮回复、思考和搜索摘要"""
    items = load_toollog()
    bj_str = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
    items.append({
        "schema_version": 1,
        "ts": bj_str,
        "user": user_text[:60],
        "pre_searched": pre_searched or "",
        "model_tools": model_tools or [],
        "confidence": confidence,
        "reply_preview": (reply_preview or "")[:200],          # 本轮助手回复前200字
        "reasoning_preview": (reasoning_preview or "")[:300],  # 本轮思考前300字
        "search_snippets": search_snippets or [],               # 本轮搜索原始摘要（Method C备用）
        "route_info": route_info or {},
        "verify_status": verify_status or "",
        "reference_mode": reference_mode or "",
        "evidence_flags": evidence_flags or [],
        "failed_urls": failed_urls or [],
    })
    items = items[-MAX_TOOLLOG:]
    try:
        atomic_write_json(TOOLLOG_FILE, items)
    except Exception as e:
        log.warning(f"tool_log 写入失败: {e}")

def fmt_toollog_for_prompt(n=3):
    """格式化最近 n 轮工具使用记录（含思考摘要和回复摘要）,用于注入系统提示"""
    items = load_toollog()[-n:]
    if not items: return ""
    lines = []
    for it in items:
        route_info = it.get("route_info") or {}
        route = route_info.get("route", "")
        route_s = "搜索回答" if route == "search" else ("快速回答" if route == "fast" else "未记录")
        reason_s = route_info.get("reason", "")
        verify_s = it.get("verify_status") or ""
        ref_mode = it.get("reference_mode") or ""
        ref_flags = it.get("evidence_flags") or []
        parts_ = []
        if it.get("pre_searched"):
            ps = it["pre_searched"]
            label = "代查缓存" if ps.startswith("缓存:") else "代查Wiki"
            parts_.append(f"{label}({ps.lstrip('缓存:').lstrip('wiki:')})")
        if it.get("model_tools"):
            parts_.append(f"自调:{'+'.join(it['model_tools'])}")
        else:
            parts_.append("自调:无")
        conf = it.get("confidence")
        conf_s = f" [置信{conf}]" if conf is not None else ""
        block = f"  {it['ts']}「{it['user']}」→ 路由:{route_s}"
        if reason_s:
            block += f" | 原因:{reason_s[:80]}"
        if ref_mode:
            flags_s = f"({'+'.join(ref_flags)})" if ref_flags else ""
            block += f" | 引用:{ref_mode}{flags_s}"
        block += f" | 工具:{', '.join(parts_)}{conf_s}"
        if verify_s:
            block += f" | 核查:{verify_s}"
        if it.get("failed_urls"):
            block += f"\n    抓取失败URL: {', '.join(it['failed_urls'][:3])}"
        if it.get("reasoning_preview"):
            block += f"\n    思考: {it['reasoning_preview'][:150]}"
        if it.get("reply_preview"):
            block += f"\n    回复: {it['reply_preview'][:150]}"
        lines.append(block)
    return "\n".join(lines)


# ── API 配额跟踪 ──────────────────────────────────────────────────────
def _quota_month():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")

QUOTA_NO_RESET = {"serper"}   # 一次性额度,不随月份重置

def load_quota():
    try:
        with open(QUOTA_FILE, encoding="utf-8") as handle:
            d = json.load(handle)
        if d.get("month") != _quota_month():
            # 换月：只重置按月计费的 key,一次性额度保留
            old_counts = d.get("counts", {})
            new_counts = {k: v for k, v in old_counts.items() if k in QUOTA_NO_RESET}
            d = {"month": _quota_month(), "counts": new_counts, "warned": {},
                 "warn_pct": d.get("warn_pct", 80)}
    except:
        d = {"month": _quota_month(), "counts": {}, "warned": {}, "warn_pct": 80}
    return d

def save_quota(d):
    try:
        atomic_write_json(QUOTA_FILE, d)
    except Exception as e:
        log.warning(f"quota 写入失败: {e}")

def inc_quota(key):
    """记录一次 API 调用；首次超阈值时加入预警队列"""
    import tg_bot.config as _cfg
    with _quota_lock, _quota_file_lock():
        d = load_quota()
        d.setdefault("counts", {})[key] = d["counts"].get(key, 0) + 1
        cnt   = d["counts"][key]
        limit = _cfg.API_FREE_LIMITS.get(key, 0)
        if limit > 0:
            warn_pct = d.get("warn_pct", 80)
            warned   = d.setdefault("warned", {})
            warn_at  = int(limit * warn_pct / 100)
            if cnt >= warn_at and not warned.get(key):
                warned[key] = True
                _cfg._quota_warnings.append(
                    f"⚠️ API 配额预警\n"
                    f"{key}：本月已用 {cnt}/{limit}（{cnt * 100 // limit}%）\n"
                    f"当前预警阈值 {warn_pct}%,发 /quota set <数字> 可调整."
                )
        save_quota(d)

def fmt_quota():
    """格式化配额概览,用于 /quota 命令"""
    import tg_bot.config as _cfg
    d        = load_quota()
    counts   = d.get("counts", {})
    warn_pct = d.get("warn_pct", 80)
    lines    = [f"📊 API 配额 · {d['month']}\n"]
    tavily_keys = [f"tavily_{i}" for i in range(len(_cfg.TAVILY_KEYS))]
    for key in tavily_keys + ["brave", "serper"]:
        limit   = _cfg.API_FREE_LIMITS.get(key, 1000)
        cnt     = counts.get(key, 0)
        pct     = cnt * 100 // limit
        dead    = min(pct // 10, 10)          # 🪦 数量（已用）
        alive   = 10 - dead                   # 🌱 数量（剩余）
        bar     = "🪦" * dead + "🌱" * alive
        remaining = limit - cnt
        warn    = "  ⚠️" if cnt >= int(limit * warn_pct / 100) else ""
        if key.startswith("tavily_"):
            label = f"Tavily {key.split('_')[1]}"
        elif key == "serper":
            label = "Serper ∞"
        else:
            label = key.capitalize()
        lines.append(f"{label}\n{bar}  {pct}%  ·  还剩 {remaining} 次{warn}\n")
    lines.append(f"∞ 一次性总额  ·  预警阈值 {warn_pct}%")
    return "\n".join(lines)


# ── 工作日志（两个 AI 均记录） ────────────────────────────────────────
def save_worklog_entry(entry: dict):
    """
    每轮对话追加一条工作日志到 WORKLOG_DIR/<YYYYMMDD>.jsonl.
    entry 格式：
      ts, user, main{rounds,tools,source_count,fetch_count},
      verifier{rounds:[{verdict,summary}], rewrites, final}, reply_len
    """
    os.makedirs(WORKLOG_DIR, exist_ok=True)
    bj_today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    path = os.path.join(WORKLOG_DIR, f"{bj_today}.jsonl")
    entry = dict(entry)
    entry.setdefault("schema_version", 1)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"save_worklog_entry 失败: {e}")

def fmt_worklog(date_str=""):
    """
    格式化某天工作日志（date_str 为空取今天）.
    返回可发送的文本.
    """
    if not date_str:
        date_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    path = os.path.join(WORKLOG_DIR, f"{date_str}.jsonl")
    try:
        lines = open(path, encoding="utf-8").read().strip().splitlines()
    except FileNotFoundError:
        return f"📋 {date_str} 没有工作日志."
    except Exception as e:
        return f"读取失败: {e}"

    entries = []
    for ln in lines:
        try: entries.append(json.loads(ln))
        except: pass
    if not entries:
        return f"📋 {date_str} 日志为空."

    out = [f"📋 工作日志  {date_str}  共 {len(entries)} 轮\n"]
    for e in entries[-20:]:  # 最多展示最近 20 条
        m  = e.get("main", {})
        v  = e.get("verifier", {})
        vf = v.get("final", "skip")
        vr = v.get("rewrites", 0)
        verdict_tag = {"pass":"✅","failed":"❌","skip":"⏭","pending":"⏳"}.get(vf, vf)
        tools_str = ", ".join(dict.fromkeys(m.get("tools", [])))[:50] or "无"
        ref_mode = m.get("reference_mode", "")
        ref_flags = m.get("evidence_flags") or []
        ref_str = f"  引用:{ref_mode}{'('+ '+'.join(ref_flags) +')' if ref_flags else ''}" if ref_mode else ""
        out.append(
            f"  {e.get('ts','')}  「{e.get('user','')[:30]}」\n"
            f"    主bot: {m.get('rounds',0)}轮/{m.get('source_count',0)}条来源/{m.get('fetch_count',0)}篇正文  工具:{tools_str}{ref_str}\n"
            f"    审核: {verdict_tag} 重写{vr}次  回复{e.get('reply_len',0)}字"
        )
    return "\n".join(out)


# ── 自动清理（定时删除过期文件） ──────────────────────────────────────
def auto_cleanup():
    """
    清理过期文件：
      来源存档（SOURCES_DIR/）：按日子目录,删除超过 MAX_SOURCES_DAYS 天的目录
      工作日志（WORKLOG_DIR/） ：删除超过 MAX_WORKLOG_DAYS 天的 .jsonl 文件
    在 bot 启动时及每 100 次请求后调用.
    """
    bj_now = datetime.now(timezone(timedelta(hours=8)))
    deleted = []

    # 来源存档：SOURCES_DIR/<YYYYMMDD>/ 子目录,整目录删除
    try:
        cutoff_src = (bj_now - timedelta(days=MAX_SOURCES_DAYS)).strftime("%Y%m%d")
        for day in os.listdir(SOURCES_DIR):
            day_path = os.path.join(SOURCES_DIR, day)
            if os.path.isdir(day_path) and day.isdigit() and day < cutoff_src:
                import shutil
                try:
                    shutil.rmtree(day_path)
                    deleted.append(f"src/{day}/")
                except: pass
            # 兼容旧格式：直接在根目录下的 .json 文件
            elif day.endswith(".json") and day[:8] < cutoff_src:
                try:
                    os.remove(day_path)
                    deleted.append(f"src/{day}")
                except: pass
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"auto_cleanup sources 失败: {e}")

    # 工作日志：WORKLOG_DIR 下是 YYYYMMDD.jsonl
    try:
        cutoff_wl = (bj_now - timedelta(days=MAX_WORKLOG_DAYS)).strftime("%Y%m%d")
        for fn in os.listdir(WORKLOG_DIR):
            if fn.endswith(".jsonl") and fn[:8] < cutoff_wl:
                try:
                    os.remove(os.path.join(WORKLOG_DIR, fn))
                    deleted.append(f"wl/{fn}")
                except: pass
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"auto_cleanup worklog 失败: {e}")

    if deleted:
        log.info(f"🗑 自动清理 {len(deleted)} 个过期文件: {deleted[:5]}{'…' if len(deleted)>5 else ''}")


# ── 来源存档（按日分子目录：SOURCES_DIR/YYYYMMDD/HHMMSS.json） ────────
def save_sources_file(user_text, source_index, tool_results, fetched_pages, reply,
                      facts_json=None, reference_mode="", evidence_flags=None):
    """按日归档：SOURCES_DIR/<YYYYMMDD>/<HHMMSS>.json"""
    if not source_index and not fetched_pages:
        return None
    bj_now  = datetime.now(timezone(timedelta(hours=8)))
    day_dir = os.path.join(SOURCES_DIR, bj_now.strftime("%Y%m%d"))
    os.makedirs(day_dir, exist_ok=True)
    fname   = bj_now.strftime("%H%M%S") + ".json"
    path    = os.path.join(day_dir, fname)
    data    = {
        "schema_version": 1,
        "ts":             bj_now.strftime("%Y-%m-%d %H:%M:%S"),
        "user":           user_text,
        "results":        source_index,   # 三层结构（含 id/title/snippet/full_content）
        "sources":        source_index,   # 兼容旧字段
        "search_results": tool_results,
        "fetched_pages":  fetched_pages,
        "facts_json":     facts_json or {},
        "reference_mode": reference_mode or "",
        "evidence_flags": evidence_flags or [],
        "ai_reply":       reply,
    }
    try:
        atomic_write_json(path, data)
        update_today_index(source_index, session_user=user_text)  # 追加到当日标题索引
        log.info(f"📁 来源存档：{bj_now.strftime('%Y%m%d')}/{fname}"
                 f"（{len(source_index)}条来源,{len(fetched_pages)}篇正文）")
        return path
    except Exception as e:
        log.warning(f"save_sources_file 失败: {e}")
        return None

def load_today_index():
    """加载今日搜索结果标题索引（供 gather_ai 注入上下文）"""
    day  = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    path = os.path.join(SOURCES_DIR, day, "index.json")
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except:
        return []

def update_today_index(entries, session_user=""):
    """将本轮新增的结果条目追加到今日索引（去重）.
    session_user：触发本次搜索的用户原话,用于跨语言关键词匹配."""
    if not entries:
        return
    day     = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    day_dir = os.path.join(SOURCES_DIR, day)
    os.makedirs(day_dir, exist_ok=True)
    path    = os.path.join(day_dir, "index.json")
    try:
        existing = json.loads(open(path, encoding="utf-8").read())
    except:
        existing = []
    existing_ids = {e["id"] for e in existing}
    for e in entries:
        if e.get("id") and e["id"] not in existing_ids:
            # query: web_search 时是搜索词,fetch_content 时是 URL → 都保留
            #        但若是 URL 形式,用 session_user 补一份在 query 字段,保证关键词匹配
            _q = e.get("query", "")
            if _q.startswith("http"):
                _q = (session_user[:30] + " | " + _q)[:60]
            # snippet_head: 去掉开头的「[正文来源：URL]」前缀,保留真实正文
            _sn = (e.get("snippet") or "")
            import re as _re_inner
            _sn = _re_inner.sub(r"^\[正文来源：[^\]]+\]\s*", "", _sn)
            existing.append({
                "schema_version": 1,
                "id":           e["id"],
                "query":        _q[:60],
                "title":        e.get("title", ""),
                "domain":       e.get("domain", ""),
                "snippet_head": _sn[:150],
                "session_user": session_user[:60],
            })
            existing_ids.add(e["id"])
    atomic_write_json(path, existing)

def list_sources_files(n=10):
    """列出最近 n 条来源存档,跨日期目录扫描"""
    entries = []
    try:
        day_dirs = sorted(
            [d for d in os.listdir(SOURCES_DIR)
             if os.path.isdir(os.path.join(SOURCES_DIR, d)) and d.isdigit()],
            reverse=True
        )
        for day in day_dirs:
            day_path = os.path.join(SOURCES_DIR, day)
            for fn in sorted(os.listdir(day_path), reverse=True):
                if not fn.endswith(".json"): continue
                rel  = f"{day}/{fn}"
                path = os.path.join(day_path, fn)
                try:
                    d = json.loads(open(path, encoding="utf-8").read())
                    entries.append({
                        "filename": rel,
                        "ts":       d.get("ts", ""),
                        "user":     d.get("user", "")[:40],
                        "sources":  len(d.get("sources", [])),
                        "fetched":  len(d.get("fetched_pages", [])),
                    })
                except: pass
                if len(entries) >= n:
                    return entries
    except FileNotFoundError:
        pass
    return entries

def _extract_text_preview(content, chars=300):
    """从抓取正文中跳过头部URL声明行和图片行,取第一段有意义的文字."""
    if not content:
        return ""
    lines = content.splitlines()
    text_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("[正文来源：") or line.startswith("!["):
            continue
        if line.startswith("http") and len(line.split()) == 1:
            continue  # 纯 URL 行
        text_lines.append(line)
        if sum(len(l) for l in text_lines) >= chars:
            break
    return " ".join(text_lines)[:chars]

def read_sources_file(filename, detail=None):
    """读取来源存档.
    filename: YYYYMMDD/HHMMSS.json
    detail: None=总览 | 'full'=全部正文 | int=第N条正文
    """
    path = os.path.join(SOURCES_DIR, filename)
    try:
        d = json.loads(open(path, encoding="utf-8").read())
    except:
        return "文件不存在或无法读取."
    src_list = d.get("results") or d.get("sources") or []

    # ── 单条详情 ──────────────────────────────────────────────────────
    if isinstance(detail, int):
        idx = detail - 1
        if idx < 0 or idx >= len(src_list):
            return f"没有第 {detail} 条,共 {len(src_list)} 条来源."
        s = src_list[idx]
        fc = s.get("full_content") or ""
        text = _extract_text_preview(fc, chars=3000) if fc else s.get("snippet","") or "（无正文）"
        lines = [
            f"📄 第{detail}条  {s.get('domain','')}",
            f"🔗 {s.get('url','')}",
            f"搜索词：{s.get('query','')}  工具：{s.get('tool','')}",
            "",
            text,
        ]
        return "\n".join(lines)

    # ── 全部正文 ──────────────────────────────────────────────────────
    if detail == "full":
        lines = [f"📁 {d.get('ts','')}  ❓ {d.get('user','')[:50]}\n"]
        for i, s in enumerate(src_list, 1):
            fc = s.get("full_content") or ""
            text = _extract_text_preview(fc, chars=1200) if fc else s.get("snippet","") or "（无正文）"
            lines.append(f"── [{i}] {s.get('domain','')} ──")
            lines.append(text)
            lines.append("")
        return "\n".join(lines)

    # ── 总览（默认）──────────────────────────────────────────────────
    lines = [
        f"📁 来源存档  {d.get('ts','')}",
        f"❓ {d.get('user','')}",
        f"\n🔗 搜索来源（{len(src_list)} 条）",
        f"📖 /source 【文件名】 2  → 看第2条原文,full → 全部原文\n",
    ]
    for i, s in enumerate(src_list, 1):
        fc = s.get("full_content") or ""
        preview = _extract_text_preview(fc, chars=150) if fc else _extract_text_preview(s.get("snippet",""), chars=150)
        lines.append(f"  [{i}] {s.get('domain','')}  {s.get('tool','')}")
        if s.get("url"):
            lines.append(f"      {s['url'][:70]}")
        if preview:
            lines.append(f"      {preview}")
    lines.append(f"\n🤖 AI 回复（{len(d.get('ai_reply',''))} 字）")
    lines.append(d.get("ai_reply", "")[:600])
    return "\n".join(lines)


# ── 每日记忆：原始流水账 ──────────────────────────────────────────────
def append_daily_log(role: str, content: str):
    """
    每条 Telegram 对话实时追加到 daily_logs/YYYY-MM-DD.jsonl.
    仅记录主对话（http_mode 微信走独立 wx_history,不记这里）.
    """
    import os as _os
    from tg_bot.config import DATA_DIR
    from datetime import datetime, timezone, timedelta
    bj = timezone(timedelta(hours=8))
    bj_today = datetime.now(bj).strftime("%Y-%m-%d")
    log_dir = DATA_DIR + "/daily_logs"
    _os.makedirs(log_dir, exist_ok=True)
    path = _os.path.join(log_dir, f"{bj_today}.jsonl")
    ts = datetime.now(bj).strftime("%H:%M:%S")
    entry = {"ts": ts, "role": role, "content": content[:2000]}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"append_daily_log 写入失败: {e}")


# ── 每日记忆：工具查询函数 ────────────────────────────────────────────
def search_daily_summaries(keyword: str) -> str:
    import os as _os
    from tg_bot.config import DATA_DIR
    summaries_dir = DATA_DIR + "/daily_summaries"
    if not _os.path.isdir(summaries_dir):
        return "暂无任何日总结记录"
    hits = []
    for fname in sorted(_os.listdir(summaries_dir), reverse=True):
        if not fname.endswith(".json"):
            continue
        date_str = fname[:-5]
        try:
            raw = open(_os.path.join(summaries_dir, fname), encoding="utf-8").read()
            if keyword.lower() in raw.lower():
                d = json.loads(raw)
                snippet = d.get("summary", "")[:120]
                hits.append(f"• {date_str}：{snippet}…")
        except Exception:
            continue
    if not hits:
        return f"在所有日总结中未找到关键词「{keyword}」"
    result = f"找到 {len(hits)} 天的记录（关键词：{keyword}）:\n" + "\n".join(hits[:10])
    if len(hits) > 10:
        result += f"\n…（共 {len(hits)} 条,只显示最近10条）"
    return result


def read_daily_summary(date_str: str) -> str:
    """先查总结文件,没有再看原始日志,都没有才说无记录."""
    import os as _os
    from tg_bot.config import DATA_DIR
    sum_path = _os.path.join(DATA_DIR + "/daily_summaries", f"{date_str}.json")
    log_path = _os.path.join(DATA_DIR + "/daily_logs", f"{date_str}.jsonl")
    # ① 有总结文件
    if _os.path.exists(sum_path):
        try:
            d = json.loads(open(sum_path, encoding="utf-8").read())
            parts = [f"📅 {date_str} 日总结"]
            if d.get("topics"):
                parts.append("话题：" + "、".join(d["topics"]))
            parts.append(d.get("summary", "（无总结内容）"))
            return "\n\n".join(parts)
        except Exception as e:
            return f"读取总结失败: {e}"
    # ② 有原始日志但还没生成总结
    if _os.path.exists(log_path):
        return (f"{date_str} 尚未生成日总结（由 daily_summary_gen.py 的定时任务生成）,"
                f"但有原始对话记录,可用 read_daily_log 查看详情.")
    # ③ 两者都没有
    return (f"{date_str} 无任何对话记录"
            f"（该日期早于系统上线,或当天未使用 bot）")


def read_daily_log(date_str: str, max_msgs: int = 60) -> str:
    """读取某天的原始对话流水账（daily_logs/YYYY-MM-DD.jsonl）."""
    import os as _os
    from tg_bot.config import DATA_DIR
    fpath = _os.path.join(DATA_DIR + "/daily_logs", f"{date_str}.jsonl")
    if not _os.path.exists(fpath):
        return f"未找到 {date_str} 的原始对话记录（该日期早于系统上线,或当天无对话）"
    msgs = []
    with open(fpath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    msgs.append(json.loads(line))
                except Exception:
                    pass
    if not msgs:
        return f"{date_str} 的日志文件为空"
    lines = [f"📋 {date_str} 原始对话（共 {len(msgs)} 条）\n"]
    for m in msgs[:max_msgs]:
        role = "用户" if m.get("role") == "user" else "AI"
        lines.append(f"[{m.get('ts','')}] {role}：{m.get('content','')[:300]}")
    if len(msgs) > max_msgs:
        lines.append(f"\n…（共 {len(msgs)} 条,已截取前 {max_msgs} 条）")
    return "\n".join(lines)
