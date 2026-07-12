# 研究发现

## 现有链路

- `tg_bot/evidence.py:build_today_report_pack` 目前只读取本地 `today_report.txt`，把全文和板块摘要包装成一个 `LOCAL_TODAY_REPORT` 来源，没有事件级候选、热度分数或跨天记忆。
- `tg_bot/workers/gather_executor.py:_execute_today_report` 同样只加载单份报告文本，若没有来源条目则追加本地来源。
- `tg_bot/workers/source_utils.py:score_source` 已有工具、内容长度、域名权威性、查询相关性评分，但没有时效性、热度、事件聚类或日报去重维度。
- `tg_bot/workers/display.py:clean_reply_for_user` 只清理内部来源标记与空白，没有统一的“结论/依据/建议/来源”输出契约。
- 搜索/写作链路已有 `source_index`、`facts_json`、来源去重和审计日志，可作为扩展接口，不需要重建 pipeline。

## 设计约束

- 回复结构必须隐藏内部推理，只展示可验证结论、证据摘要、必要的下一步和来源。
- 日报去重不能只按 URL；同一事件的不同媒体报道应通过规范化标题/实体/时间窗口合并。
- “热度”必须可解释：优先采用来源数量、独立域名数、时间新鲜度、显式互动/趋势字段；无热度字段时不能把搜索排名当真实热度。
- 需要保留兼容字段和旧命令行为，新增字段应能被旧调用方忽略。
- 用户尚未指定日报地域/主题范围，下一步需确认；若未回复，默认采用“中国 + 全球 + AI/技术”三栏，每栏设置上限与多样性约束。

## 外部接口核查

- Brave News Search 支持 `freshness=pd`（24 小时）和 `page_age`/日期相关元数据，适合做新鲜度过滤，但搜索结果排序本身不能当作热度指标。
- Tavily Search 支持 `topic=news`、`time_range`/日期范围和结果 `score`，适合补充近实时新闻与相关性评分；文档没有承诺社会热度字段。
- Serper News 返回日期字段但主要是搜索聚合结果；应作为降级来源，不把排名/日期单独解释成热度。
- 热度信号应优先使用多来源独立域名覆盖、同事件出现次数、近 24 小时新鲜度、可选的显式互动字段；没有显式热度字段时只能标记“多源关注”，不能声称“全网最热”。

## 当前代码图谱信号

- `http_post`、`send`、`atomic_write_json` 是高扇入热点；变更日报/回复时优先复用现有存储和发送边界。
- 知识图谱项目已索引：728 nodes / 2540 edges，状态 ready。

## 板块保留与轮换补充发现

- `scripts/build-daily-report.py` 当前只查询 `china`、`global`、`ai_tech` 三个类别，并会重新写完整 `today_report.txt`；因此直接扩展类别会丢掉原有天气、汇率、行情、代理、Hacker News、GitHub、冷知识等外部板块。
- 仓库内已有 `execute_weather` 和 `execute_github_trending` 原生工具，但没有 Steam、代理更新、Hacker News、汇率或行情的专用日报采集器；这些板块原本依赖外部写入的 `today_report.txt`。
- 采用板块注册表：快照板块不应用事件冷却，事件板块按自身 section id + 事件指纹保存历史。新采集候选为空时保留旧板块文本；候选存在但全部因冷却被过滤时只保留板块标题并明确跳过重复内容。
