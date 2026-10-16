# tg-bot — Telegram 搜索助手

联网搜索、抓取原文、交叉核查的 Telegram 机器人。运行于 VPS systemd 服务，同时提供 HTTP API（/ask）供外部调用。

## 聊天处理流水线

```
disambig.py → 意图消歧：需要搜索还是直接回答？
  │
  ├─ 搜索路由 ──→ search_policy.py → core/router.py
  │                  决定是否联网、调用本地工具等
  │
  └─ 直接回答 ──→ fast_chat() / ds_chat()
  
搜索路由 ──→ core/pipeline.py 串联 Searcher/Curator/Writer/Critic/Patcher
  │
  ├─ pipeline/gather.py（采集层）
  │    同轮多工具已并行执行；工具调用见 tools/definitions.py
  │
  ├─ agents/evidence_auditor.py（证据审计）
  │    + evidence_audit.py：检查来源充分性；不足时补查，零认可时降级不拒答
  │
  ├─ agents/curator.py（Curator 筛选）
  │    按相关度/可信度排序，direct:// 直连条目豁免短文过滤
  │
  ├─ agents/writer.py（写作层）
  │    生成正文，标注 [来源N]；时效数据须标日期，社区来源须标注来源
  │
  └─ agents/critic.py（Critic 审核）
       分段核查已并行；问题分类 SOFT（措辞调整）/ HARD（删除）
       通不过时触发 agents/patcher.py 补救
  
发送给用户（降级回答附"未完全交叉核实"标注）
```

## 核心模块路由

| 模块 | 职责 |
|------|------|
| `bot.py` | 消息主循环、命令分发 |
| `pipeline/disambig.py` | 判断需不需要搜索 |
| `search_policy.py` | 路由策略：搜索/快速/本地工具 |
| `core/pipeline.py` | 搜索流程编排（Searcher→Curator→Writer→Critic→Patcher） |
| `progress.py` | 进度面板（事件驱动无动画，分段核查单行显示，完成后保留） |

## 日报生成

`scripts/build-daily-report.py` 编排日报采集：

**板块** — 见 `report_sections.py`：
- 天气（紧凑格式）/ 汇率（USD、JPY）/ 美股（纳指、标普500 带周末收盘标注）
- 中国、全球、AI 要闻 + 4 个储备板块
- 代理释出 / HN（带 AI 说明）/ Steam（精选 3 款带介绍）/ 冷知识

**核查流程** — 见 `report_research.py`（两阶段）：
- 第一阶段：初搜聚合（Brave、Tavily、Serper）
- 第二阶段：入选事件再查 2 次、抓 ≤3 篇正文；严格总结后由独立 verify key 核查
- 规则：单一权威域名可过，推荐 2 个独立域名；单事件失败丢弃，研究链失败内容归零但快照可发

## HTTP API

`ask_server.py` 提供 `/ask` 接口，不保存对话历史、每次独立处理。

支持：Bearer token、X-API-Key 鉴权；限流、请求体大小、查询长度校验均在进程内。
多实例部署时在反向代理处做全局限流。

## 部署

systemd 服务 `tg-bot.service` 启动 `scripts/tg-bot-new.py`（入口脚本）。
日报定时任务 `tg-bot-daily-report.timer`。

日报关键环境变量（完整清单见根目录 `.env.example`）：
`DAILY_REPORT_PUSH`（生成后是否推送）、`DAILY_REPORT_COOLDOWN_DAYS`（事件冷却期）、
`DAILY_REPORT_MAX_WORKERS`（采集并发数）。
运行状态落盘于数据目录的 `daily_report_state.json`（事件冷却状态）与
`daily_report_status.json`（本次生成/推送结果）。

启动前自检：`python3 /path/to/tg-bot-check.py`（不会启动 Telegram 轮询）。

## 测试

```bash
python3 -m unittest tg_bot.tests.test_core_units
```

## 修改提示词和能力

编辑 `prompts.py`：`_SYS_DISAMBIG`、`_SYS_GATHER`、`_SYS_WRITE`、`_SYS_VERIFY`。
编辑 `agents/writer.py`：`_SYS_WRITER`；编辑 `agents/critic.py`：`_SYS_CRITIC`。

动态能力说明编辑 `/var/lib/morning-report/features.md`（每行 `· 功能`），无需重启立即生效。

## 添加新功能

- **搜索工具**：编辑 `tools/search.py`
- **本地工具**：编辑 `tools/native.py` + `tools/definitions.py`（JSON Schema）
- **Telegram 命令**：在 `commands/` 下新增，在 `bot.py` 注册
