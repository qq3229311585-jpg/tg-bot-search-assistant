# 安全加固与可部署性修复设计

## 背景

当前项目已经有 Telegram 私聊入口和 HTTP `/ask` 入口，但 HTTP 服务默认监听所有网卡、请求体没有上限，配置在 import 时创建固定的 `/var/lib/morning-report`，且部分 API key 缺失时会以 `IndexError` 失败。项目仍然需要保留 Apple Watch、n8n、Home Assistant 等外部客户端的接入空间，因此不能简单删除 HTTP 接口或把所有配置写死。

## 目标

1. 保留现有 Telegram 行为和 `/ask` 兼容入口。
2. 预留版本化 HTTP 接口、健康检查、版本/能力发现和可替换鉴权/限流边界。
3. 默认安全：HTTP 只监听本机、请求体和查询长度有上限、未配置反向代理时不信任转发头。
4. 让数据目录、监听地址、端口、token 来源和运行参数可通过环境变量配置。
5. 启动失败时给出明确、可操作的配置错误，而不是 `IndexError` 或 import-time 权限错误。
6. 提供 systemd 加固模板、HTTPS 反向代理示例和启动自检脚本。

## 非目标

- 本次不迁移到 FastAPI/Flask，不引入大型 Web 框架。
- 本次不实现多用户数据库、账号体系或完整 SaaS 租户隔离。
- 本次不改变搜索/写作/核查流水线的业务逻辑。
- 不直接推送远程仓库，也不自动修改服务器上的 `/etc` 或 systemd 服务。

## 方案

### HTTP 接口层

`tg_bot/ask_server.py` 保留现有 `POST /ask`，并增加以下兼容性接口：

- `POST /v1/ask`：与 `/ask` 使用相同请求格式和处理函数。
- `GET /health`：进程存活检查，不要求 token。
- `GET /readyz`：配置和数据目录可用性检查，失败返回非 2xx。
- `GET /version`：返回应用版本和 API 版本。
- `GET /capabilities`：返回当前启用的接口和可选工具列表，便于外部客户端发现能力。

成功响应统一增加 `request_id` 和 `api_version`，保留现有 `reply`、`ok` 字段；错误响应统一包含 `error`、`request_id` 和稳定的错误类别。旧客户端只读取 `reply`/`ok` 时保持兼容。

鉴权仍以 Bearer token 为主，同时预留 `X-API-Key` 兼容头；token 可由 `ASK_API_TOKEN` 环境变量提供，否则从受保护文件读取/生成。认证实现封装为独立函数，后续可以替换为多 token 或签名认证。

监听地址和限制项：

- `ASK_API_HOST` 默认 `127.0.0.1`；外部直连必须显式改成 `0.0.0.0`，文档明确要求前置 HTTPS 和防火墙。
- `ASK_API_PORT` 默认 `7799`。
- `ASK_API_MAX_BODY_BYTES` 默认 `65536`。
- `ASK_API_MAX_QUERY_CHARS` 默认 `4000`。
- `ASK_API_RATE_LIMIT` 和 `ASK_API_RATE_WINDOW_SECONDS` 提供进程内固定窗口限流；默认值保守且可关闭/调整。
- 只有 `ASK_API_TRUST_PROXY=true` 时才解析 `X-Forwarded-For`，否则按 TCP 对端地址限流。

### 配置与数据目录

`tg_bot/config.py` 增加可测试的配置读取/校验函数：

- `TG_BOT_DATA_DIR` 默认 `/var/lib/morning-report`。
- `ensure_data_dir()` 延迟到启动阶段调用，创建目录时使用 `0700`，不在 import 阶段写磁盘。
- DeepSeek 写作 key 必须至少有一个；核查 key 缺失时回退到写作 key，并在启动诊断中明确提示。
- Tavily/Serper 变为可选 provider；没有 key 时跳过对应降级链，不再在 import 阶段索引空列表。
- `validate_config()` 返回结构化诊断，启动脚本和 `/readyz` 复用它。

### 部署

更新 systemd 示例：

- 使用专用 `tgbot` 用户、`UMask=0077`、`NoNewPrivileges=yes`。
- `ProtectSystem=strict`，仅通过 `ReadWritePaths` 放行数据目录。
- 默认 `ASK_API_HOST=127.0.0.1`，避免服务启动即公网暴露。
- 使用 `Restart=on-failure`，并保留网络启动依赖。

新增 Nginx 示例，仅代理 `/ask`、`/v1/ask`、`/health`、`/readyz`、`/version`、`/capabilities`，不暴露数据目录；TLS 证书路径使用明确占位符。

新增 `scripts/tg-bot-check.py`：不启动 Telegram 轮询，只检查环境变量、数据目录权限、端口配置和依赖导入，适合部署前运行。

### 测试

先写失败测试，再实现：

- 配置读取：自定义数据目录、缺失 key 的明确错误、可选搜索 provider。
- HTTP：`/ask` 和 `/v1/ask` 兼容、健康/就绪/版本/能力接口、Bearer 与 `X-API-Key`、错误格式、请求体/查询长度限制、限流和 request ID。
- 启动：import 不创建固定系统目录，`ensure_data_dir()` 使用安全权限。
- 回归：现有 37 个单元测试、compileall，以及启动自检脚本。

## 风险与取舍

- 进程内限流不能跨多进程/多实例共享；本次保留轻量实现，生产多实例时由 Nginx 或网关做全局限流。
- 不在应用内终止 TLS，避免引入证书生命周期管理；远程访问必须通过 HTTPS 反向代理。
- 兼容 `/ask` 会延长旧接口生命周期，但能避免 Apple Watch 等已有客户端被破坏。
