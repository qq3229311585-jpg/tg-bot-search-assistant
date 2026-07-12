# tg-bot — 个人专属 Telegram 信息助手

一个跑在 VPS 上的 Telegram 机器人，接入 DeepSeek AI + 多路搜索引擎，能联网搜索、抓取原文、核查事实，并对外提供 HTTP 接口（供 Apple Watch 快捷指令等外部调用）。

---

## 项目结构

```
/usr/local/bin/
└── tg-bot-new.py              # 入口脚本（systemd 启动这个）

/usr/local/lib/python3.11/dist-packages/tg_bot/
├── __init__.py
├── bot.py                     # 核心：消息处理主逻辑、handle() 函数
├── bot_utils.py               # Telegram 工具函数：tg()、send()、md_to_html()
├── config.py                  # 所有配置、路径、密钥（从环境变量读取）
├── prompts.py                 # 各 AI 角色的 system prompt
├── storage.py                 # 历史、摘要、上下文、日志的读写
├── ask_server.py              # HTTP /ask 接口（供外部系统调用）
├── tools_impl.py              # 工具执行实现（http_get/post、API balance 等）
├── pipeline/
│   ├── disambig.py            # 第1层：意图消歧（判断是否需要搜索、提取关键词）
│   ├── gather.py              # 第2层：采集 AI（搜索 + 抓原文）+ ds_chat() 快速路径
│   ├── write.py               # 第3层：写作 AI（基于事实列表生成回复）
│   └── verify.py              # 第4层：核查 AI（事实核查、发现问题则重写）
├── tools/
│   ├── search.py              # 搜索工具：Tavily / Brave / Serper 自动回退
│   ├── fetch.py               # 抓取工具：Jina Reader 抓正文
│   ├── native.py              # 原生工具：天气、VPS 流量、GitHub Trending
│   └── definitions.py        # 工具 JSON Schema 定义（TOOLS 列表，传给 DeepSeek）
└── commands/
    ├── control.py             # 控制命令：/restart /clear 等
    ├── info.py                # 信息命令：/status /quota /diary /thinking 等
    └── quota.py               # API 配额管理

/etc/tg-bot.env                # 密钥文件（chmod 600，不要提交到 git）
/etc/systemd/system/tg-bot.service  # systemd 服务配置
/var/lib/morning-report/       # 运行时数据目录（详见下方数据文件说明）
```

---

## AI 四层流水线

```
用户消息
  │
  ▼
[第1层] 意图消歧（disambig.py）
  判断：需要搜索？还是直接回答？提取关键词
  │
  ├─ 不需要搜索 → ds_chat() 直接回答
  │
  └─ 需要搜索 ↓
  │
  ▼
[第2层] 采集 AI（gather.py）
  工具调用：Tavily / Brave / Serper 搜索
             Jina 抓原文 / Wikipedia / 天气 / GitHub
             read_daily_log / search_daily_summaries（历史查询）
  输出：fact_list（结构化事实列表）
  │
  ▼
[第3层] 写作 AI（write.py）
  基于 fact_list 生成回复
  │
  ▼
[第4层] 核查 AI（verify.py）
  对比来源核查，发现问题则重写
  │
  ▼
发送给用户
```

---

## 环境变量配置

密钥全部放在 `/etc/tg-bot.env`，格式：

```bash
BOT_TOKEN=你的_Telegram_Bot_Token
ALLOWED_CHAT=允许的_Chat_ID（只响应这一个）

# DeepSeek（支持多个 key 轮换）
DEEPSEEK_KEY_0=sk-...
DEEPSEEK_KEY_1=sk-...        # 可选，第二个 key
DEEPSEEK_VERIFY_KEY_0=sk-...  # 可选；不填时复用写作 key

# 搜索 API（三路自动回退：Tavily → Brave → Serper）
TAVILY_KEY_0=你的_Tavily_Key
TAVILY_KEY_1=你的_Tavily_Key_2        # 可选
BRAVE_KEY=BSA...
SERPER_KEY_0=...
SERPER_KEY_1=...             # 可选

# 运行目录和 HTTP API
TG_BOT_DATA_DIR=/var/lib/morning-report
DAILY_REPORT_CATEGORIES=china,global,ai_tech
DAILY_REPORT_ITEMS_PER_CATEGORY=4
DAILY_REPORT_COOLDOWN_DAYS=14
DAILY_REPORT_TIMEZONE=Asia/Shanghai
DAILY_REPORT_STATE_FILE=/var/lib/morning-report/daily_report_state.json
ASK_API_HOST=127.0.0.1
ASK_API_PORT=7799
ASK_API_TOKEN=                 # 留空则生成并保存为 0600 文件
ASK_API_MAX_BODY_BYTES=65536
ASK_API_MAX_QUERY_CHARS=4000
ASK_API_RATE_LIMIT=30
ASK_API_RATE_WINDOW_SECONDS=60
ASK_API_TRUST_PROXY=false      # 仅在可信反向代理后开启

# OpenHuman 记忆集成（可选）
OPENHUMAN_RPC_TOKEN=...
```

---

## 服务管理

```bash
# 启动 / 停止 / 重启
systemctl start tg-bot
systemctl stop tg-bot
systemctl restart tg-bot

# 启动前自检（不会启动 Telegram 轮询）
PYTHONPATH=/opt/tg-bot-search-assistant python3 /opt/tg-bot-search-assistant/scripts/tg-bot-check.py

# 查看日志（实时）
journalctl -u tg-bot -f

# 查看最近 50 条日志
journalctl -u tg-bot -n 50 --no-pager
```

---

## HTTP /ask 接口

供外部系统（Apple Watch 快捷指令、n8n、Home Assistant 等）直接调用。

应用默认只监听 `127.0.0.1:7799`。远程访问请使用仓库中的
`deploy/nginx/tg-bot.conf.example` 做 HTTPS 反向代理，不要直接暴露明文端口。
使用该代理时，将 `ASK_API_TRUST_PROXY=true` 写入 `/etc/tg-bot.env`；模板会清洗
`X-Forwarded-For`，限流才能按真实客户端地址隔离。

**兼容地址：** `POST /ask`、`POST /v1/ask`

**鉴权：** `Authorization: Bearer <token>` 或 `X-API-Key: <token>`。
token 默认存在 `/var/lib/morning-report/ask_api_token`，也可以用 `ASK_API_TOKEN` 环境变量显式提供。

```bash
# 查看当前 Token
cat /var/lib/morning-report/ask_api_token

# 修改 Token（改完重启服务生效）
echo '新token' > /var/lib/morning-report/ask_api_token
systemctl restart tg-bot
```

**请求示例：**

```bash
curl -X POST http://127.0.0.1:7799/v1/ask \
  -H "Authorization: Bearer 你的token" \
  -H "Content-Type: application/json" \
  -d '{"query": "今天有什么新闻", "brief": false}'
```

**返回格式：**

```json
{"reply": "AI 的回复内容", "ok": true, "api_version": "v1", "request_id": "..."}
```

**参数说明：**

| 参数 | 类型 | 说明 |
|------|------|------|
| query | string | 要问的问题 |
| brief | bool | true = 回复压缩到80字内（适合小屏幕）；false = 完整回复 |

> `/ask` 接口不保存对话历史，每次独立处理，用完即走。

**探活和能力发现：** `GET /health`、`GET /readyz`、`GET /version`、`GET /capabilities`。
服务端还会限制请求体、查询长度并进行进程内限流；多实例部署时请在反向代理处做全局限流。

---

## 如何添加新功能

### 添加新的搜索工具
编辑 `tools/search.py`，参考现有的 `execute_search()` 实现。

### 添加新的原生工具（不需要搜索的）
编辑 `tools/native.py`，并在 `tools/definitions.py` 里补充 JSON Schema。

### 修改 AI 提示词
编辑 `prompts.py`，各层 prompt 变量名：
- `_SYS_DISAMBIG` — 意图消歧层
- `_SYS_GATHER` — 采集层
- `_SYS_WRITE` — 写作层
- `_SYS_VERIFY` — 核查层

### 更新 Bot 能力说明（无需改代码）
编辑 `/var/lib/morning-report/features.md`，每条功能一行，以 `· ` 开头。
文件内容会在每次启动时动态注入到 AI 系统提示词，**改完立即生效，无需重启服务**。

### 添加新的 Telegram 命令（/xxx）
在 `commands/` 目录下对应文件添加，然后在 `bot.py` 的命令分发处注册。

### 修改回复截断长度
编辑 `tools/fetch.py`，搜索 `[:8000]` 修改数字。

---

## 数据文件说明

所有运行时数据存放在 `/var/lib/morning-report/`：

| 文件 / 目录 | 说明 | 可以删吗 |
|-------------|------|----------|
| chat_history.json | Telegram 对话历史（含时间戳，最近20条） | 可以，删了日志历史清空 |
| chat_summary.json | 旧对话摘要（超出20条时滚动写入） | 可以 |
| context_summary.json | 近期对话上下文（含时间戳，消歧AI用于理解追问） | 可以，删了下轮无上下文 |
| thinking.json | AI 思考过程存档（采集/写作/核查三层推理） | 可以，只是审计用 |
| tool_log.json | 跨轮工具使用日志（Bot 可回答"你查过没"） | 可以，删了历史工具记录清空 |
| daily_logs/ | 每日对话原始记录（YYYY-MM-DD.jsonl） | 可以，删了历史查询功能受影响 |
| daily_summaries/ | 每日对话摘要（YYYY-MM-DD.json，每晚自动生成） | 可以，删了但原始日志还在 |
| user_profiles.json | 用户画像（由每日摘要生成） | 可以，会自动重建 |
| features.md | Bot 功能清单，动态注入 AI 提示词 | **不要删**，删了 AI 不知道自己有什么能力 |
| today_report.txt | 今日午报内容 | 可以，删了午报功能临时失效 |
| daily_report.json | 今日机器可读事件、热度分数、来源和去重依据 | 可以，删了不影响下一次生成 |
| daily_report_state.json | 最近已发布事件指纹（默认冷却 14 天） | 谨慎，删除会让旧事件重新具备候选资格 |
| ask_api_token | HTTP 接口密钥 | 删了会自动重新生成 |
| sources/ | 搜索原文缓存 | 可以，删了缓存失效需重新抓取 |
| worklog/ | AI 每轮工作日志 | 可以，只是审计用 |
| api_quota.json | API 调用量统计 | 可以，统计清零 |
| content_cache/ | 搜索结果临时缓存 | 可以，删了缓存失效 |

### 存储写入策略

`file_io.py` 提供统一的原子写工具：

```text
写入 path.tmp.<pid> → flush/fsync → os.replace 覆盖正式文件
```

以下覆盖写文件已走原子写：`chat_history.json`、`chat_summary.json`、`context_summary.json`、`thinking.json`、`tool_log.json`、`api_quota.json`、`api_limits.json`、`tg_offset.txt`、`ask_api_token`、`daily_report.json`、`daily_report_state.json`、`today_report.txt`、`sources/*/*.json`、`sources/*/index.json`。

日报生成由 `scripts/build-daily-report.py` 完成。它按 `DAILY_REPORT_CATEGORIES` 调用新闻搜索，生成事件级 JSON 和兼容的 `today_report.txt`；所有供应商失败时不会覆盖上一份有效日报。

这样在服务重启、进程异常或 VPS 瞬断时，上述文件不会留下半截 JSON/文本；最坏情况是保留旧版本。`daily_logs/*.jsonl` 和 `worklog/*.jsonl` 属于追加日志，仍保持逐行 append。

### Schema 版本策略

当前采用“兼容旧数据 + 新记录带版本”的轻量策略：

- `tool_log.json` 仍是数组，新写入条目带 `schema_version: 1`
- `sources/YYYYMMDD/HHMMSS.json` 顶层带 `schema_version: 1`
- `sources/YYYYMMDD/index.json` 仍是数组，新写入条目带 `schema_version: 1`
- `worklog/*.jsonl` 新追加行带 `schema_version: 1`

旧记录没有 `schema_version` 是正常情况，读取逻辑保持兼容，不需要迁移历史文件。

---

## 常见问题

**Bot 没有回复？**
```bash
journalctl -u tg-bot -n 30 --no-pager
```
看日志，通常是 API key 耗尽或网络问题。

**搜索结果不好？**
检查 Tavily/Brave/Serper key 余额，在 Bot 里发 `/quota` 查看。

**回复里有乱码或格式问题？**
编辑 `bot_utils.py` 里的 `md_to_html()` 函数调整 Markdown 转换规则。

**历史查询找不到？**
检查 `/var/lib/morning-report/daily_logs/` 目录下有没有对应日期的 `.jsonl` 文件；
当天的总结在 `daily_summaries/` 下，每晚 23:55（北京时间）自动生成。

**想换服务器？**
1. 复制 `/etc/tg-bot.env` 到新服务器
2. 复制 `/var/lib/morning-report/` 目录（保留历史数据）
3. 按本文档重新部署
