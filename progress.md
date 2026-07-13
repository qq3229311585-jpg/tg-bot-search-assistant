# 进度记录

## 2026-07-12

- 已建立目标：优化回复结构与日报搜索的去重/热度质量。
- 已读取 brainstorming、planning-with-files、writing-plans、TDD、verification-before-completion 技能。
- 已通过 codebase-memory-mcp 确认项目索引可用并完成架构/符号检索。
- 已创建本地 `task_plan.md`、`findings.md`，尚未修改生产代码。
- 已核对 Brave News、Tavily News、Serper News 的可用字段：可稳定使用新鲜度/日期/相关性，多源覆盖可作为可解释热度代理，但没有统一的社会热度字段。
- 已按默认范围完成正式设计 spec：`docs/superpowers/specs/2026-07-12-reply-daily-report-design.md`。
- 已创建实现计划：`docs/superpowers/plans/2026-07-12-reply-daily-report.md`。
- 已按 TDD 完成 `tg_bot/response.py` 与 bot/日报显示接入，结构测试和集成测试通过。
- 已完成 `tg_bot/daily_report.py`：事件指纹、标题聚类、14 天冷却、官方更新、热度评分和类别/域名多样性选择。
- 已完成 `scripts/build-daily-report.py`、版本化状态存储、失败保留旧报告策略，以及 systemd service/timer 示例和文档。
- 已根据独立代码审查修复旧闻淘汰、标题改写去重、来源编号映射、时区、状态清理、结构化 provider、Serper key 轮换、损坏状态和 dry-run 写入边界。
- 最终验证：`Ran 91 tests ... OK`；compileall、git diff --check、配置自检和 CLI help 均通过；知识图谱 ready（860 nodes / 2891 edges），工作树干净。
- 新目标开始：保留所有原有日报板块，按板块区分快照与事件轮换。
- 已确认仓库内没有 Steam/代理/Hacker News/汇率/行情专用日报采集器；新增 `tg_bot/report_sections.py` 注册表和可注册 collector 接口，默认保留外部 `today_report.txt` 板块。
- 已接入 `build-daily-report.py`：事件板块按自身候选和冷却选择，候选被冷却时输出“跳过重复”，快照/未接入板块从旧报告兼容保留；JSON/status 增加板块元数据。
- 新增板块轮换与 legacy 保留测试，定向测试已通过；全量测试需在允许 loopback bind 的环境复跑。
- 已补齐空候选快照刷新、过期候选不恢复旧段、严格板块历史匹配、每板块冷却状态保留、旧 `DAILY_REPORT_CATEGORIES` 显式兼容，以及 bot/evidence 中 Steam 和中国/全球板块说明。
- 最终非 HTTP 全量回归：`90 tests ... OK`；日报/存储定向回归：`30 tests ... OK`；此前允许 loopback 的完整回归：`98 tests ... OK`。compileall、diff check、启动自检、CLI help 均通过。最近一次成功图谱索引为 930 nodes / 3131 edges，最新重复索引因会话额度限制未能运行。

## 2026-07-14 二次体检与实施

- 完成第二轮审计：确认日报定时服务默认只落盘、不自动发送；正文抓取入口存在 SSRF 风险；多板块采集串行；`/readyz` 不检查日报新鲜度。
- 新增 `validate_fetch_url`：仅允许 HTTP(S)，拒绝 userinfo、localhost、环回/私网/链路本地/保留地址，并对域名 DNS 解析结果 fail-closed 校验。
- `bot_utils.send`/`_send_chunk` 现在返回真实投递布尔值，HTML 失败会根据纯文本降级结果判断最终状态。
- 日报采集改为 `DAILY_REPORT_MAX_WORKERS`（1–8，默认 4）有界并发，按请求顺序合并候选和诊断，单板块异常不影响其他板块。
- 新增 `content_sha256`、可选 `DAILY_REPORT_PUSH`/`--push` 和 Telegram 推送幂等状态（`sent`/`failed`/`skipped_unchanged`），默认行为仍只生成文件。
- `/readyz` 现在检查 `daily_report_status.json`，缺失状态仅告警，明确过期/损坏/上次保留旧日报则报告未就绪；新增 `DAILY_REPORT_MAX_STALE_HOURS`。
- 已补齐二次体检测试、环境示例、README、systemd 注释。编译和非网络单元通过；允许 loopback 的完整回归 `113 tests ... OK`。
- 根据独立复审继续加固：本地正文抓取将 DNS 解析结果固定到 socket，避免再次按 hostname 解析；远端 Tavily/12ft 默认关闭并明确 opt-in；日报推送同日复跑复用已发送状态、失败重试不提交冷却；Tavily/Serper key 和配额文件增加线程/跨进程锁。
- 最终允许 loopback 的全量回归：`114 tests ... OK`；定向回归、compileall、py_compile、CLI `--help` 和 `git diff --check` 均通过。

## 2026-07-14 远端部署

- 远端旧版原以 root 运行 `/usr/local/bin/tg-bot-new.py`；已备份到 `/root/tg-bot-backup-20260714-2602019`。
- 已部署到 `/opt/tg-bot-search-assistant`，新增专用 `tgbot` 用户，运行数据 `/var/lib/morning-report` 改为 700/600 权限，环境文件保持 600。
- 已安装并启用 `tg-bot.service`、`tg-bot-daily-report.timer`；日报服务设置 `DAILY_REPORT_PUSH=true`，远端提取保持关闭。
- 发现并修复 systemd 未设置 `PYTHONPATH` 导致加载旧全局模块的问题，提交 `9931241` 后重新同步并重启。
- 部署验证：Bot 运行用户为 `tgbot`，HTTP 仅监听 `127.0.0.1:7799`，`/health`、`/readyz` 成功，未授权 `/ask` 返回 401；日报首次运行生成 22 个事件，状态为 `fresh`，推送状态为 `sent`。
