# 安全加固与可部署性修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留 Telegram 与现有 `/ask` 兼容性，同时增加版本化 HTTP 接口、安全默认值、可配置运行目录、明确启动诊断和加固部署模板。

**Architecture:** 在现有标准库 `http.server` 之上增加小型 API 适配层，不引入 Web 框架。配置集中在 `tg_bot.config`，HTTP 处理器只依赖配置诊断、认证、限流和请求执行函数；systemd/Nginx 只提供部署隔离，不改变业务流水线。

**Tech Stack:** Python 3 标准库、`unittest`、`http.server`、systemd、Nginx。

---

## 文件地图

- Modify: `tg_bot/config.py` — 环境变量、配置诊断、数据目录初始化。
- Modify: `tg_bot/ask_server.py` — API 路由、认证、请求限制、限流、request ID。
- Modify: `tg_bot/bot.py` — 启动阶段调用数据目录初始化和配置校验。
- Modify: `tg_bot/tests/test_core_units.py` — 配置和 API 行为回归测试。
- Create: `scripts/tg-bot-check.py` — 不启动 bot 的部署前自检。
- Modify: `deploy/tg-bot.service.example` — 专用用户、最小权限和可配置 API 绑定。
- Create: `deploy/nginx/tg-bot.conf.example` — HTTPS 反向代理示例。
- Modify: `.env.example` — 新配置项和安全默认值。
- Modify: `README.md`、`tg_bot/README.md` — API、部署和自检文档。

### Task 1: 建立基线和测试工具

**Files:**
- Test: `tg_bot/tests/test_core_units.py`

- [ ] **Step 1: Run baseline checks**

Run from repository root:

```bash
python3 -m compileall -q tg_bot scripts
BOT_TOKEN=test ALLOWED_CHAT=1 DEEPSEEK_KEY_0=test BRAVE_KEY=test \
  PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units
```

Expected: compile succeeds and the existing unit suite reports 37 tests with 0 failures.

- [ ] **Step 2: Add test-only helpers for isolated environment reloads**

Add a context manager in `test_core_units.py` that temporarily sets/removes environment variables and removes `tg_bot.config` from `sys.modules`; it must restore the original environment in `finally` and never write to `/var/lib/morning-report`.

- [ ] **Step 3: Run the focused test module**

Run:

```bash
PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units -v
```

Expected: the original tests still pass before production behavior changes.

- [ ] **Step 4: Commit the test harness**

```bash
git add tg_bot/tests/test_core_units.py
git commit -m "test: add isolated configuration test helpers"
```

### Task 2: Make configuration import-safe and deployable

**Files:**
- Test: `tg_bot/tests/test_core_units.py`
- Modify: `tg_bot/config.py`
- Modify: `tg_bot/bot.py`

- [ ] **Step 1: Write failing configuration tests**

Add tests covering:

```python
def test_config_uses_custom_data_dir_without_import_side_effect(self):
    with temporary_env(TG_BOT_DATA_DIR=temp_dir, required_keys=True):
        cfg = reload_config()
        self.assertEqual(cfg.DATA_DIR, temp_dir)
        self.assertFalse(os.path.exists(temp_dir))

def test_config_reports_missing_deepseek_key(self):
    with temporary_env(BOT_TOKEN="x", ALLOWED_CHAT="1", BRAVE_KEY="x"):
        with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_KEY_0"):
            reload_config()

def test_config_allows_search_provider_keys_to_be_empty(self):
    with temporary_env(required_keys=True, TAVILY_KEY_0="", SERPER_KEY_0=""):
        cfg = reload_config()
        self.assertEqual(cfg.TAVILY_KEYS, [])
        self.assertEqual(cfg.SERPER_KEYS, [])

def test_ensure_data_dir_creates_private_directory(self):
    with temporary_env(required_keys=True, TG_BOT_DATA_DIR=temp_dir):
        cfg = reload_config()
        cfg.ensure_data_dir()
        self.assertEqual(stat.S_IMODE(os.stat(temp_dir).st_mode), 0o700)
```

- [ ] **Step 2: Run the new tests and confirm expected failures**

Run:

```bash
PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units.ConfigTests -v
```

Expected: failures show import-time directory creation and empty provider indexing.

- [ ] **Step 3: Implement minimal configuration changes**

In `config.py`:

1. Read `DATA_DIR` from `TG_BOT_DATA_DIR` with the existing path as default.
2. Require at least one writing DeepSeek key with a clear `RuntimeError`.
3. Set `DEEPSEEK_VERIFY_KEYS = configured_verify_keys or DEEPSEEK_KEYS`.
4. Set `SERPER_KEY = SERPER_KEYS[0] if SERPER_KEYS else ""` and make rotation a no-op when empty.
5. Make `ensure_data_dir()` call `os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)` and tighten permissions when possible.
6. Remove the unconditional `os.makedirs(DATA_DIR, exist_ok=True)` at module import.
7. Add `validate_config()` returning `{\"ok\": bool, \"errors\": [...], \"warnings\": [...]}` without raising.

In `bot.py`, call `ensure_data_dir()` and raise a readable error from `validate_config()` before starting the HTTP server or Telegram polling.

- [ ] **Step 4: Run focused tests and full regression**

Run:

```bash
PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units.ConfigTests -v
PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units
```

Expected: new configuration tests pass and the existing suite remains green.

- [ ] **Step 5: Commit the configuration change**

```bash
git add tg_bot/config.py tg_bot/bot.py tg_bot/tests/test_core_units.py
git commit -m "fix: make runtime configuration import-safe"
```

### Task 3: Add compatible, bounded HTTP API behavior

**Files:**
- Test: `tg_bot/tests/test_core_units.py`
- Modify: `tg_bot/ask_server.py`

- [ ] **Step 1: Write failing HTTP tests**

Add tests using an in-process `HTTPServer` on an ephemeral port and a stubbed `tg_bot.bot.handle`, covering:

```python
def test_v1_ask_alias_preserves_reply_shape(self): ...
def test_health_version_and_capabilities_are_public(self): ...
def test_readyz_reports_configuration_failure(self): ...
def test_bearer_and_x_api_key_are_accepted(self): ...
def test_request_body_and_query_limits_return_413_or_400(self): ...
def test_rate_limit_returns_429_after_threshold(self): ...
def test_request_id_is_echoed_or_generated(self): ...
```

The test must assert that the legacy `/ask` response still contains `reply` and `ok`.

- [ ] **Step 2: Run the focused HTTP tests and confirm they fail**

Run:

```bash
PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units.ApiServerTests -v
```

Expected: failures identify missing routes, limits, and auth helpers.

- [ ] **Step 3: Implement the API layer**

In `ask_server.py`:

1. Add environment-backed constants for host, port, maximum body/query sizes, rate limit, rate window, and proxy trust.
2. Add pure helpers `get_request_id`, `client_identity`, `authenticate`, `json_error`, and `check_rate_limit`.
3. Route `POST /ask` and `POST /v1/ask` through the same handler.
4. Add public `GET /health`, `GET /readyz`, `GET /version`, and `GET /capabilities` handlers.
5. Reject oversized `Content-Length` before reading the body; reject oversized query strings with a stable 413/400 JSON error.
6. Accept `Authorization: Bearer ...` and `X-API-Key: ...` using `hmac.compare_digest`.
7. Only honor `X-Forwarded-For` when `ASK_API_TRUST_PROXY=true`.
8. Return `request_id` and `api_version` while preserving `reply`/`ok`.
9. Bind `HTTPServer` to configurable `ASK_API_HOST`, defaulting to `127.0.0.1`.

- [ ] **Step 4: Run focused HTTP tests and full regression**

Run:

```bash
PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units.ApiServerTests -v
PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units
```

Expected: all API and existing tests pass.

- [ ] **Step 5: Commit the API hardening**

```bash
git add tg_bot/ask_server.py tg_bot/tests/test_core_units.py
git commit -m "fix: harden and version the ask api"
```

### Task 4: Add deployment self-check and service hardening

**Files:**
- Create: `scripts/tg-bot-check.py`
- Modify: `deploy/tg-bot.service.example`
- Create: `deploy/nginx/tg-bot.conf.example`

- [ ] **Step 1: Write failing self-check tests**

Add tests that invoke the script with a temporary `TG_BOT_DATA_DIR`, assert exit code 0 for a complete environment, and assert a nonzero exit plus the missing variable name for an incomplete environment.

- [ ] **Step 2: Run self-check tests and confirm failure**

Run:

```bash
PYTHONPATH=. python3 -m unittest tg_bot.tests.DeploymentCheckTests -v
```

Expected: the script is missing or does not report structured diagnostics.

- [ ] **Step 3: Implement the self-check**

Make `scripts/tg-bot-check.py` import `validate_config`, ensure the configured data directory, print one line per error/warning, and exit `0` only when `ok` is true. It must never start `tg_bot.bot.main` or open a listening socket.

- [ ] **Step 4: Harden the systemd template**

Add `User=tgbot`, `Group=tgbot`, `UMask=0077`, `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ReadWritePaths=/var/lib/morning-report`, `PrivateTmp=yes`, `Restart=on-failure`, and `Environment=ASK_API_HOST=127.0.0.1`. Keep the existing network dependency and document the required `tgbot` user/data-directory setup.

- [ ] **Step 5: Add the reverse-proxy example**

Create an Nginx server block with TLS certificate placeholders, proxy timeouts, request-size limit, and locations for `/ask`, `/v1/ask`, `/health`, `/readyz`, `/version`, and `/capabilities`. Do not proxy arbitrary paths.

- [ ] **Step 6: Run deployment checks**

Run:

```bash
python3 -m py_compile scripts/tg-bot-check.py
PYTHONPATH=. python3 scripts/tg-bot-check.py
```

Expected: the first command succeeds; the second reports missing deployment secrets rather than a traceback.

- [ ] **Step 7: Commit deployment changes**

```bash
git add scripts/tg-bot-check.py deploy/tg-bot.service.example deploy/nginx/tg-bot.conf.example tg_bot/tests/test_core_units.py
git commit -m "ops: add deployment checks and service hardening"
```

### Task 5: Document configuration and interfaces

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tg_bot/README.md`

- [ ] **Step 1: Document every new variable and default**

Document `TG_BOT_DATA_DIR`, `ASK_API_HOST`, `ASK_API_PORT`, `ASK_API_TOKEN`, `ASK_API_MAX_BODY_BYTES`, `ASK_API_MAX_QUERY_CHARS`, `ASK_API_RATE_LIMIT`, `ASK_API_RATE_WINDOW_SECONDS`, and `ASK_API_TRUST_PROXY`.

- [ ] **Step 2: Document API compatibility and reverse proxy requirements**

Show curl examples for `/ask` and `/v1/ask`, list public health/discovery endpoints, explain that direct `0.0.0.0` exposure is discouraged, and point to the Nginx example.

- [ ] **Step 3: Document deployment self-check and service user**

Add exact commands for creating `tgbot`, preparing the data directory, running `scripts/tg-bot-check.py`, installing the systemd unit, and checking `/readyz`.

- [ ] **Step 4: Commit documentation**

```bash
git add .env.example README.md tg_bot/README.md
git commit -m "docs: document configurable api deployment"
```

### Task 6: Final verification and handoff

**Files:**
- No new files; verify all changed files and commits.

- [ ] **Step 1: Run the complete verification set**

```bash
python3 -m compileall -q tg_bot scripts
PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units -v
git diff --check HEAD~4..HEAD
git status --short
```

Expected: compile succeeds, all tests pass, diff check is clean, and only intended files are changed.

- [ ] **Step 2: Inspect the final diff**

Confirm no real secrets, server IPs, or generated runtime data were added; confirm `/ask` remains compatible and default bind is localhost.

- [ ] **Step 3: Report exact verification evidence**

Provide the local repository path, commit list, test count, interface list, and any deployment steps that still require the user’s server-specific values. Do not claim remote publication.

