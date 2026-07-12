# 回复结构与日报热度优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** 让 Telegram 回复结构稳定清晰、证据可追溯，并让日报按事件指纹跨天去重、按新鲜度与热度优先选择高价值事件。

**Architecture:** 在现有搜索/证据链上增加纯函数式的回复结构化与日报候选排序层；持久化日报事件指纹与已发布窗口，避免重复；把热度判定拆成可解释的多信号评分，缺少热度证据时降级而不是臆测。保留现有工具接口，通过兼容字段扩展。

**Tech Stack:** Python 标准库、现有 `tg_bot` pipeline、JSON 状态文件、unittest。

---

### Phase 1: 研究与设计（已完成）

- [x] 盘点现有回复生成、日报生成、来源评分和持久化边界。
- [x] 核对 Brave/Tavily/Serper 新闻接口的日期、相关性和热度可用字段。
- [x] 明确日报主题范围、发送时段、去重窗口和“热度”信号优先级。
- [x] 写设计说明并完成自检：`docs/superpowers/specs/2026-07-12-reply-daily-report-design.md`。

### Phase 2: 回复结构（TDD）

- [x] 为结构化回复契约写失败测试：结论、关键依据、行动建议、来源四段；闲聊和工具结果保持兼容。
- [x] 实现统一结构化渲染与长度/降级策略，不泄露内部推理。
- [x] 将搜索回答和日报摘要接入结构化渲染，保留原始审计字段。
- [x] 运行相关单元测试并重构重复逻辑。

### Phase 3: 日报事件去重与热度排序（TDD）

- [x] 为事件指纹、跨天去重、同事件更新合并、热度排序写失败测试。
- [x] 添加候选标准化、指纹生成、热度分数和多样性配额函数。
- [x] 扩展日报状态存储，记录最近发布指纹、首次/最近出现时间、来源与分数。
- [x] 更新日报采集/编排，优先新鲜高热事件，来源不足时明确降级。

### Phase 4: 文档、部署与验证

- [x] 更新环境变量、日报格式、数据迁移和运维说明。
- [ ] 运行完整测试、编译检查、差异检查和自检脚本。
- [ ] 更新代码知识图谱，记录接口和数据流变化。

### Review follow-up

- [x] 修复超过 24 小时旧闻仍可能入选的问题。
- [x] 修复跨天标题改写绕过去重、状态无界增长、来源编号丢失、来源标题误归入依据、时区配置失效和单源热度误标。
- [x] 增加结构化新闻供应商边界与 `stale_previous` 状态文件。

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| 直接用工作树绝对路径查询知识图谱失败 | 1 | 改用已索引项目名 `Users-secondcomputer-Documents-Codex-2026-07-12-new-chat-work-tg-bot-search-assistant` |
| brainstorming 技能路径误用 `.system/brainstorming` | 1 | 使用 `/Users/secondcomputer/.codex/skills/brainstorming/SKILL.md` |
