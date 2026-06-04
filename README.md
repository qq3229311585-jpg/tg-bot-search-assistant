# tg-bot-search-assistant

一个面向个人使用的 Telegram AI 搜索助手。它把一次回答拆成多个职责清晰的小模块：意图判断、查询改写、搜索采集、来源筛选、写作、事实核查和最小修补。目标不是单纯“接一个聊天模型”，而是做一个可追溯、可检查、可迭代的信息检索 bot。

## 功能介绍

- **双路径回答**：闲聊走快速路径，知识题走搜索路径，不把所有问题都丢给同一个模型。
- **多源搜索**：支持 Wikipedia、Brave、Tavily、Serper、正文抓取和今日缓存回填，尽量把可用材料先找全。
- **层级化处理**：QueryFixer、Gather、Curator、Writer、Critic、Patcher 各做一件事，便于定位问题和逐层优化。
- **可追溯输出**：内部保留来源、工具、facts_json、worklog 和审核记录，用户侧只看到清洗后的自然回复。
- **冲突可见**：当多来源说法不一致时，系统会在内部保留分歧并供核查链使用，方便追查来源差异。
- **本地工具集成**：支持天气、VPS 流量、GitHub Trending、日历等本地能力，适合个人信息助理场景。

## 这个项目解决什么问题

普通聊天机器人容易出现两个问题：

- 该搜索时不搜索，凭模型印象回答。
- 搜了很多材料，但最后回复很短、来源不清、核查链路不可见。

这个项目的设计重点是把“搜索回答”拆开：

```text
用户问题
  -> Disambig / SearchPolicy：判断是否需要外部证据
  -> QueryFixer：生成更适合搜索的查询词
  -> Gather：调用 Wikipedia / Brave / Serper / Tavily / fetch_content 等工具
  -> Curator：筛选和排序来源，决定回复长度和风格
  -> Writer：只根据来源写正文，并在内部标注 [来源N]
  -> Critic：核查 Writer 的事实陈述
  -> Patcher：只做必要的最小修改
  -> Display cleaner：用户侧隐藏内部来源标签，审计日志保留
```

## 核心能力

- Telegram 私人 bot，只响应指定 `ALLOWED_CHAT`
- HTTP `/ask` 接口，可给快捷指令、其他本地系统或自动化调用
- 多路搜索：Wikipedia、Brave、Tavily、Serper
- 正文抓取和来源库存：保存来源、摘录、工具记录、工作日志
- 事实核查：搜索路径会进入 Critic/Verifier 审核
- 快速路径标记：纯模型回复可显示 `AI-no references`
- 今日缓存和来源追问：可以回答“刚才查了吗”“用了什么来源”
- 本地工具扩展：天气、VPS 流量、GitHub Trending、日历等

## 项目结构

```text
tg_bot/
  bot.py                  # Telegram 主循环、路由、日志和用户出口
  config.py               # 环境变量、路径、配额配置
  ask_server.py           # HTTP /ask 接口
  search_policy.py        # 代码级搜索策略
  prompts.py              # 各 AI 的系统提示词

  agents/
    query_fixer.py        # 查询改写
    curator.py            # 来源筛选和目标字数
    writer.py             # 新写作 AI
    critic.py             # 新核查 AI
    patcher.py            # 最小修补

  core/
    contracts.py          # 模块间 dataclass 契约
    pipeline.py           # 搜索 pipeline 编排

  pipeline/
    disambig.py           # 意图消歧
    gather.py             # 采集主循环
    write.py              # 旧写作兼容层
    verify.py             # 旧核查兼容层

  workers/
    gather_executor.py    # 工具执行调度
    gather_tools.py       # 工具结果转 source_index
    source_utils.py       # 来源过滤、打分、去重
    facts_builder.py      # 最小 facts_json
    display.py            # 用户可见回复清洗

  tools/
    search.py             # Brave / Tavily / Serper
    fetch.py              # HTTP、正文抓取、今日缓存读取
    native.py             # 原生工具
    calendar_tool.py      # 可选 CalDAV 日历工具

  commands/               # Telegram 命令
  tests/                  # 单元测试
```

## 安装

```bash
git clone <your-repo-url>
cd tg-bot-search-assistant
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

代码本身主要用 Python 标准库发 HTTP 请求。`requirements.txt` 里的 `caldav` / `icalendar` 只用于可选日历工具。

## 配置

复制环境变量模板：

```bash
cp .env.example /etc/tg-bot.env
chmod 600 /etc/tg-bot.env
```

至少需要：

```bash
BOT_TOKEN=...
ALLOWED_CHAT=...
DEEPSEEK_KEY_0=...
DEEPSEEK_VERIFY_KEY_0=...
BRAVE_KEY=...
TAVILY_KEY_0=...
SERPER_KEY_0=...
```

运行时数据默认写入：

```text
/var/lib/morning-report/
```

包括对话历史、工具日志、来源存档、worklog、HTTP `/ask` token 等。这些运行时数据不要提交到 GitHub。

## 启动

本地测试：

```bash
set -a
source /etc/tg-bot.env
set +a
PYTHONPATH=. python3 scripts/tg-bot-new.py
```

systemd 示例在：

```text
deploy/tg-bot.service.example
```

部署时可复制为：

```bash
cp deploy/tg-bot.service.example /etc/systemd/system/tg-bot.service
systemctl daemon-reload
systemctl enable --now tg-bot
```

## 测试

```bash
PYTHONPATH=. python3 -m unittest tg_bot.tests.test_core_units
```

## 安全说明

这个仓库不应包含任何真实 token、API key、服务器 IP、SSH 密码、CalDAV 密码或运行时数据。所有密钥均通过环境变量读取。

公开前建议再跑一次：

```bash
rg -n "sk-|tvly-|Bearer|password|密码|token|secret|key|你的真实IP|你的真实端口" .
```

命中环境变量名是正常的；命中真实值必须删除或改成环境变量。

## 当前状态

这是一个个人项目的工程化版本，已具备可运行的搜索/核查/审计链路。它仍然不是通用 SaaS 产品，部分默认路径、命令和工具偏个人 VPS 环境；如果要多人使用，建议进一步拆配置、权限、数据目录和用户隔离。
