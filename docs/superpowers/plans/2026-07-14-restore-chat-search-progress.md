# Restore Chat and Search Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the VPS June 4 chat/search progress experience and a detailed safe execution trace, then deploy and push a fresh daily report.

**Architecture:** Keep Telegram progress presentation in a small `TelegramProgress` controller. Pass optional callbacks through the existing gather and search pipeline, so HTTP mode remains silent. Build `/thinking` from deterministic route/tool metadata instead of verbatim model reasoning.

**Tech Stack:** Python 3.11+, `unittest`, Telegram Bot HTTP API, systemd.

---

### Task 1: Telegram progress controller

**Files:**
- Create: `tg_bot/progress.py`
- Test: `tg_bot/tests/test_core_units.py`

- [ ] **Step 1: Write failing controller tests**

```python
def test_progress_creates_and_edits_one_status_message():
    calls = []
    progress = TelegramProgress(1, lambda method, data: fake_tg(calls, method, data))
    progress.set_route("search")
    progress.stage("gather", "start")
    progress.stage("gather", "done", "3")
    assert calls[0][0] == "sendMessage"
    assert any(method == "editMessageText" for method, _ in calls)

def test_progress_tool_items_keep_three_visible_messages():
    calls = []
    progress = TelegramProgress(1, lambda method, data: fake_tg(calls, method, data))
    for index in range(4):
        progress.stage("gather_item", "start", f"搜索{index}")
    deletes = [data["message_id"] for method, data in calls if method == "deleteMessage"]
    assert deletes == [2]
```

- [ ] **Step 2: Run the focused tests and confirm they fail because `TelegramProgress` does not exist**

Run: `python3 -m unittest tg_bot.tests.test_core_units.TelegramProgressTests -v`

- [ ] **Step 3: Implement the minimal controller**

```python
class TelegramProgress:
    def set_route(self, lane):
        self._lines.append("✓ " + ROUTE_LABELS.get(lane, lane))
        self._update()

    def stage(self, stage, status, detail=""):
        if stage == "gather_item":
            self._send_item(detail)
            return
        self._set_stage_line(stage, status, detail)
        self._update()

    def sending(self):
        self._current = "📤 正在发送中…"
        self._update()

    def complete(self):
        self._current = ""
        self._lines.append("✓ 已发送")
        self._update()
        self.close()

    def fail(self, detail):
        self._current = ""
        self._lines.append("⚠️ " + detail)
        self._update()
        self.close()
```

- [ ] **Step 4: Run the focused tests and confirm they pass**

Run: `python3 -m unittest tg_bot.tests.test_core_units.TelegramProgressTests -v`

### Task 2: Restore pipeline progress callbacks

**Files:**
- Modify: `tg_bot/pipeline/gather.py`
- Modify: `tg_bot/core/pipeline.py`
- Test: `tg_bot/tests/test_core_units.py`

- [ ] **Step 1: Write failing tests for gather item and pipeline stage callbacks**

```python
def test_send_tool_status_prefers_callback_over_telegram():
    events = []
    _send_tool_status(None, fail_tg, fail_typing, "web_search", {"query": "今日新闻"}, status_cb=lambda *e: events.append(e))
    assert events == [("gather_item", "start", "🔍 采集搜索（1/1）：今日新闻")]
```

- [ ] **Step 2: Run focused tests and confirm the missing callback arguments fail**

Run: `python3 -m unittest tg_bot.tests.test_core_units.SearchProgressCallbackTests -v`

- [ ] **Step 3: Add `status_cb` and `progress_cb` plumbing**

```python
def gather_ai(user_text, keywords, chat_id=None, pre_results=None, history_ctx=None,
              focus_task=None, retry_hint=False, prev_searches=None,
              pre_source_entries=None, status_cb=None):
    _send_tool_status(chat_id, tg, typing, fn, args, used, quota,
                      prefix="采集", status_cb=status_cb)

def run_search_pipeline(user_text, keywords, chat_id=None, config=None,
                        history_context=None, pre_results=None,
                        pre_source_entries=None, retry_hint=False,
                        prev_searches=None, focus_task=None,
                        suggested_length="", progress_cb=None):
    _prog("query_fixer", "start")
    _prog("query_fixer", "done", query_variants[0][:30])
    _prog("gather", "start")
    raw_sources_list, meta = gather_ai(
        user_text, gather_keywords, chat_id=None if progress_cb else chat_id,
        pre_results=pre_results, pre_source_entries=pre_source_entries,
        retry_hint=retry_hint, prev_searches=prev_searches,
        focus_task=focus_task, status_cb=progress_cb,
    )
    _prog("gather", "done", str(len(raw_sources_list or [])))
```

- [ ] **Step 4: Run focused tests and confirm they pass**

Run: `python3 -m unittest tg_bot.tests.test_core_units.SearchProgressCallbackTests -v`

### Task 3: Reconnect progress to Telegram chat

**Files:**
- Modify: `tg_bot/bot.py`
- Test: `tg_bot/tests/test_core_units.py`

- [ ] **Step 1: Write a failing integration test**

```python
def test_search_handle_passes_progress_callback_and_marks_sent():
    events = []
    fake_pipeline = lambda *args, progress_cb=None, **kwargs: (
        progress_cb("gather", "start", ""),
        progress_cb("gather", "done", "2"),
        ("回答", "pass", {"source_index": []}),
    )[-1]
    reply, verdict, meta = fake_pipeline(progress_cb=lambda *event: events.append(event))
    assert reply == "回答"
    assert events == [("gather", "start", ""), ("gather", "done", "2")]
```

- [ ] **Step 2: Confirm the test fails before implementation**

Run: `python3 -m unittest tg_bot.tests.test_core_units.ReplyIntegrationTests.test_search_handle_passes_progress_callback_and_marks_sent -v`

- [ ] **Step 3: Instantiate and use `TelegramProgress`**

```python
progress = TelegramProgress(chat_id, tg, enabled=not http_mode)
progress.set_route(lane_decision.name)
reply, verify_status, meta = run_search_pipeline(
    text, keywords, chat_id=chat_id, history_context=history_context,
    pre_results=pre_results, pre_source_entries=pre_source_entries,
    retry_hint=_retry_hint, prev_searches=_prev_searches,
    focus_task=_focus_task, suggested_length=suggested_length,
    progress_cb=progress.stage,
)
progress.sending()
send(chat_id, display_reply)
progress.complete()
```

- [ ] **Step 4: Verify focused integration tests pass**

### Task 4: Restore detailed safe `/thinking` chain

**Files:**
- Modify: `tg_bot/commands/info.py`
- Test: `tg_bot/tests/test_core_units.py`

- [ ] **Step 1: Extend the existing failing digest test**

```python
assert "第1轮：web_search、fetch_content" in digest
assert "路线：搜索回答" in digest
assert "web_search(今日新闻)" in digest
assert "private verifier reasoning" not in digest
```

- [ ] **Step 2: Run the digest test and confirm the new expectations fail**

Run: `python3 -m unittest tg_bot.tests.test_core_units.ThinkingDigestTests -v`

- [ ] **Step 3: Format deterministic stages from thinking and tool logs**

```python
def _format_thinking_digest(th, tool_log=None, *, max_chars=2600):
    # Select the newest tool-log query, list route, per-round tools, tool calls,
    # source/fetch flags and verifier result; never append `reasoning` fields.
    latest = (tool_log or [{}])[-1]
    user = latest.get("user") or next((e.get("user") for e in reversed(th) if e.get("user")), "")
    related = [entry for entry in th if entry.get("user") == user]
    return format_execution_lines(user, latest, related, max_chars=max_chars)
```

- [ ] **Step 4: Run the digest tests and confirm they pass**

### Task 5: Verify, deploy, and push a fresh report

**Files:**
- Update generated graph artifact after indexing.

- [ ] **Step 1: Run syntax and full tests**

Run: `python3 -m py_compile tg_bot/bot.py tg_bot/progress.py tg_bot/pipeline/gather.py tg_bot/core/pipeline.py tg_bot/commands/info.py`

Run: `python3 -m unittest discover -s tg_bot/tests -p 'test_*.py'`

- [ ] **Step 2: Commit the behavior restore**

```bash
git add tg_bot docs/superpowers
git commit -m "fix: restore chat search progress"
```

- [ ] **Step 3: Deploy only changed runtime files, restart, and check health**

Expected: `tg-bot.service` active, `/health` and `/readyz` OK, unauthenticated `/ask` returns 401.

- [ ] **Step 4: Start the report oneshot and inspect status**

Expected: `tg-bot-daily-report.service` result success and `daily_report_status.json` reports `fresh` with push `sent`.

- [ ] **Step 5: Re-index and commit graph artifacts**

Expected: clean worktree after the graph artifact commit.
