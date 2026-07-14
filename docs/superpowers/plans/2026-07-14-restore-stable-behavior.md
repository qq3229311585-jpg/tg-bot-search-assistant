# Restore Stable Bot Behavior Implementation Plan

> **For agentic workers:** Execute the steps task-by-task with tests and checkpoints.

**Goal:** Restore the bot's original readable Chinese report/reply experience while preserving security and deployment hardening.

**Architecture:** Keep the current hardened service, API authentication, file permissions, provider rotation, and deployment checks. Replace only the user-facing reply renderer and the sectioned daily-report path; keep the legacy report's categorized editorial format but remove its history reset and hard-coded credentials. Inject existing local summaries/context into both fast and search prompts.

**Tech Stack:** Python 3, unittest, systemd, Telegram Bot API, DeepSeek HTTP API.

---

### Task 1: Restore user-facing reply rendering

**Files:** `tg_bot/bot.py`, `tg_bot/tests/test_core_units.py`

- Replace `_render_display_reply` calls with the existing `clean_reply_for_user` behavior for ordinary and search replies.
- Keep raw replies and evidence in audit logs; do not expose the synthetic `【关键依据】`/`【来源】` envelope in ordinary Telegram output.
- Add a regression test proving headings and paragraphs are preserved without synthetic bullets.

### Task 2: Restore categorized daily report safely

**Files:** remote legacy report script and systemd/cron configuration; local deployment notes.

- Use the established two-message categorized report layout (weather, exchange, market, AI, proxy, community, GitHub, Steam, discovery, world, science, tips, cold fact).
- Remove the legacy `reset_bot_history()` call so report generation never deletes conversation history or summaries.
- Read Telegram/API credentials from `/etc/tg-bot.env`; remove literals from the runnable script.
- Disable the new sectioned report timer and retain exactly one daily trigger.
- Verify the report contains Chinese editorial fields and no raw event-list renderer markers.

### Task 3: Restore memory and search continuity

**Files:** `tg_bot/bot.py`, `tg_bot/tests/test_core_units.py`

- Include `load_summary()` and recent `load_context()` records in the fast-path prompt.
- Include the same memory block in the search writer context without leaking internal reasoning.
- Keep `/clear` scoped to dialog focus; never clear history, summaries, daily logs, or thinking logs.
- Add tests for memory block construction and fast-path message inclusion.

### Task 4: Make thinking/search audit readable

**Files:** `tg_bot/commands/info.py`, `tg_bot/tests/test_core_units.py`

- Preserve the command's Chinese stage/tool/verdict summary.
- Do not expose raw hidden reasoning; show enough tool/stage metadata to confirm whether search ran.
- Add tests for empty, fast, and search-backed thinking records.

### Task 5: Verify and deploy

- Run the focused regression tests, full unit suite, compile checks, and report dry-run.
- Deploy the minimal patch, restart the service, verify `/health`, `/readyz`, authenticated `/ask`, report timer, and state-file preservation.
- Trigger one report only after the layout and state checks pass; retain a rollback archive and report exact verification evidence.
