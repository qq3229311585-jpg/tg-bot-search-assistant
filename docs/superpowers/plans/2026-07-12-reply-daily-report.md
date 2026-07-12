# 回复结构与日报热度实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** 让用户回复具有稳定的结论/证据/行动/来源结构，并让日报候选按事件指纹、14 天冷却和可解释热度分数生成，兼容现有 `today_report.txt`、`/recap` 与搜索接口。

**Architecture:** 新增两个边界清晰的纯 Python 模块：`response.py` 负责把旧纯文本或结构化字段渲染为用户回复；`daily_report.py` 负责候选规范化、事件聚类、热度评分、历史选择和报告渲染。`storage.py` 只负责版本化 JSON 状态和原子文件读写；`scripts/build-daily-report.py` 负责网络采集与 CLI 编排，失败时保留上一份有效报告。

**Tech Stack:** Python 3.11+ 标准库（dataclasses、hashlib、datetime、urllib、json）、现有 `tg_bot.tools.search`、现有 `atomic_write_json`、unittest。

---

### Task 1: 建立回复结构纯函数与失败测试

**Files:**
- Create: `tg_bot/response.py`
- Modify: `tg_bot/tests/test_core_units.py`

- [ ] **Step 1: 写失败测试**

在 `test_core_units.py` 末尾增加 `ReplyStructureTests`，覆盖：

```python
class ReplyStructureTests(unittest.TestCase):
    def test_empty_conclusion_uses_safe_fallback(self):
        envelope = self.response.ReplyEnvelope(conclusion="", evidence=("事实 A",))
        text = self.response.render_reply(envelope)
        self.assertTrue(text.startswith("目前资料不足，无法确认"))
        self.assertIn("关键依据", text)

    def test_normalize_reply_preserves_first_paragraph_as_conclusion(self):
        envelope = self.response.normalize_reply("第一段结论。\n\n第二段事实。")
        self.assertEqual(envelope.conclusion, "第一段结论。")
        self.assertEqual(envelope.evidence, ("第二段事实。",))

    def test_sources_are_hidden_for_answer_but_visible_for_search(self):
        source = {"title": "来源标题", "domain": "example.com", "url": "https://example.com/a"}
        hidden = self.response.render_reply(self.response.ReplyEnvelope("结论", sources=(source,)))
        visible = self.response.render_reply(self.response.ReplyEnvelope("结论", sources=(source,), mode="search"))
        self.assertNotIn("example.com", hidden)
        self.assertIn("example.com", visible)

    def test_render_reply_does_not_emit_reasoning_and_respects_limit(self):
        envelope = self.response.ReplyEnvelope("结论", evidence=("x" * 2000,), actions=("下一步",))
        text = self.response.render_reply(envelope, max_chars=240)
        self.assertLessEqual(len(text), 240)
        self.assertNotIn("reasoning", text.lower())
```

Import the module under test in `setUp` with `importlib.import_module("tg_bot.response")`; do not call network code.

- [ ] **Step 2: 运行测试确认红灯**

Run: `PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units.ReplyStructureTests`

Expected: import failure because `tg_bot.response` does not exist.

- [ ] **Step 3: 实现最小纯函数**

Create `ReplyEnvelope` with tuple normalization in `__post_init__`; implement `_clean`, `_safe_conclusion`, `_split_paragraphs`, `_clip`, `normalize_reply`, and `render_reply`. `render_reply` must emit Chinese headings only when the corresponding tuple is non-empty, clamp confidence to the four allowed values, and never accept/render a `reasoning` field.

- [ ] **Step 4: 运行测试确认绿灯**

Run the same unittest command. Expected: 4 tests pass.

- [ ] **Step 5: 提交**

```bash
git add tg_bot/response.py tg_bot/tests/test_core_units.py
git commit -m "feat: add structured reply renderer"
```

### Task 2: 把结构化回复接入写作与日报显示

**Files:**
- Modify: `tg_bot/agents/writer.py`
- Modify: `tg_bot/bot.py`
- Modify: `tg_bot/evidence.py`
- Modify: `tg_bot/commands/control.py`
- Modify: `tg_bot/tests/test_core_units.py`

- [ ] **Step 1: 写失败测试**

Add tests asserting writer prompt contains the four section contract, `build_today_report_pack` exposes `mode="report"`, and `/recap` output includes the report title without internal `[来源N]` markers.

- [ ] **Step 2: 运行红灯测试**

Run: `PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units.ReplyIntegrationTests`

Expected: failures because the prompt and report pack do not expose the new contract.

- [ ] **Step 3: 实现接入**

Extend `_SYS_WRITER` with exact headings `结论 / 关键依据 / 下一步 / 来源` and instruct the model to omit empty sections. In `evidence.py`, set report envelope mode to `report`, preserve `source_index` and `facts_json`, and render only the envelope at the user boundary. In `bot.py` and `commands/control.py`, route ordinary search replies through `normalize_reply` only after verification succeeds; keep raw replies in audit logs.

- [ ] **Step 4: 运行相关测试**

Run: `PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units.ReplyStructureTests tg_bot.tests.test_core_units.ReplyIntegrationTests`

Expected: all new integration tests pass and existing reply tests remain green.

- [ ] **Step 5: 提交**

```bash
git add tg_bot/agents/writer.py tg_bot/bot.py tg_bot/evidence.py tg_bot/commands/control.py tg_bot/tests/test_core_units.py
git commit -m "feat: integrate structured reply output"
```

### Task 3: 日报候选、事件指纹与热度排序

**Files:**
- Create: `tg_bot/daily_report.py`
- Modify: `tg_bot/tests/test_core_units.py`

- [ ] **Step 1: 写失败测试**

Add `DailyReportTests` with deterministic candidate fixtures. Test that two titles from different domains cluster when token Jaccard is at least `0.65`, tracking parameters do not change the fingerprint, a 10-day-old published event is filtered by a 14-day cooldown, an official update after 24 hours is returned with `status == "update"`, missing explicit heat renormalizes the score, and selection keeps category quotas plus at most two entries from the same domain.

- [ ] **Step 2: 运行红灯测试**

Run: `PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units.DailyReportTests`

Expected: import failure because `tg_bot.daily_report` does not exist.

- [ ] **Step 3: 实现候选模型和纯函数**

Implement `NewsCandidate` and `ReportEvent` exactly as described in the design spec. Add `_canonical_url`, `_normalize_title`, `_token_jaccard`, `normalize_candidate`, `event_fingerprint`, `cluster_candidates`, `score_event`, `select_events`, and `render_daily_report`. Use timezone-aware datetimes, return scores rounded to two decimals, and include `heat_basis` labels such as `新鲜`, `3 个独立来源`, `官方来源`, `多源关注`.

- [ ] **Step 4: 运行绿灯测试**

Run the same unittest command. Expected: all event, score, cooldown, update, and diversity tests pass.

- [ ] **Step 5: 提交**

```bash
git add tg_bot/daily_report.py tg_bot/tests/test_core_units.py
git commit -m "feat: rank and deduplicate daily news events"
```

### Task 4: 日报状态存储与采集 CLI

**Files:**
- Modify: `tg_bot/config.py`
- Modify: `tg_bot/storage.py`
- Create: `scripts/build-daily-report.py`
- Modify: `tg_bot/tests/test_core_units.py`

- [ ] **Step 1: 写失败测试**

Add tests for bounded environment parsing, round-tripping versioned state in a temporary directory, corrupt-state backup/recovery, and CLI generation preserving the previous TXT file when every provider raises an exception.

- [ ] **Step 2: 运行红灯测试**

Run: `PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units.DailyReportStorageTests`

Expected: failures because the new environment variables, storage functions, and CLI are absent.

- [ ] **Step 3: 实现配置和状态**

Add bounded config values for categories, per-category items, cooldown days, timezone, and state path. Add `load_daily_report_state(path=None)` and `save_daily_report_state(state, path=None)` using `atomic_write_json`; on invalid JSON rename the file with a UTC timestamp suffix and return schema version 1 with an empty event map.

- [ ] **Step 4: 实现 CLI**

`build-daily-report.py` must expose `build_report(candidates, now, state)` for tests and a `main()` that queries each configured category through `execute_search(query, search_type="news")`, converts result records into candidates, calls `cluster_candidates`/`select_events`, writes `daily_report.json`, then atomically writes `today_report.txt`. A provider exception is collected in a diagnostics list; if all providers fail, keep the previous TXT and return exit code 1.

- [ ] **Step 5: 运行绿灯测试**

Run: `PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units.DailyReportStorageTests` and `python3 scripts/build-daily-report.py --help`. Expected: tests pass and CLI help exits 0 without importing Telegram polling.

- [ ] **Step 6: 提交**

```bash
git add tg_bot/config.py tg_bot/storage.py scripts/build-daily-report.py tg_bot/tests/test_core_units.py
git commit -m "feat: add persistent daily report builder"
```

### Task 5: 部署、文档与兼容验证

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tg_bot/README.md`
- Create: `deploy/tg-bot-daily-report.service.example`
- Create: `deploy/tg-bot-daily-report.timer.example`
- Modify: `tg_bot/tests/test_core_units.py`

- [ ] **Step 1: 写失败测试**

Add a text/config test asserting the examples document the 13:00 Asia/Shanghai timer, the state file, cooldown, and no-overwrite-on-failure behavior.

- [ ] **Step 2: 实现文档与 unit**

Document setup commands, environment bounds, state migration, report JSON schema, `/recap` compatibility, and a manual dry-run command. The service must run as `tgbot`, load `/etc/tg-bot.env`, and the timer must use `OnCalendar=*-*-* 13:00:00 Asia/Shanghai` with `Persistent=true`.

- [ ] **Step 3: 运行完整验证**

Run:

```bash
python3 -m compileall -q tg_bot scripts
PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units
git diff --check
PYTHONPATH=. python3 scripts/tg-bot-check.py
```

Expected: compile succeeds; all tests pass; diff check is empty; self-check either reports valid configuration or clearly reports missing deployment secrets without a traceback.

- [ ] **Step 4: 更新知识图谱**

Run `index_repository` in moderate mode with persistence for the worktree, then query `search_graph` for `render_reply`, `select_events`, and `build_report` to confirm the new symbols are indexed.

- [ ] **Step 5: 提交最终变更**

```bash
git add .env.example README.md tg_bot/README.md deploy/tg-bot-daily-report.service.example deploy/tg-bot-daily-report.timer.example tg_bot/tests/test_core_units.py
git commit -m "docs: document daily report operations"
```
