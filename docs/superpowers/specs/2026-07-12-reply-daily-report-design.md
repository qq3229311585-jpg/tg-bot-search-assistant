# 回复结构与日报热度设计

日期：2026-07-12

## 目标

把现有 Telegram 回复升级为稳定、清晰、可追溯的输出；把日报从“读取外部单份文本”升级为可生成、可去重、可解释排序的事件报告。

默认日报范围为中国要闻、全球要闻、AI/技术；每天北京时间 13:00 生成，每栏 3–5 条。若用户通过环境变量调整范围或条数，仍受安全上限约束。

## 当前问题

1. `tg_bot/agents/writer.py` 只有通用排版提示，回复没有稳定的结论、证据、行动和来源边界。
2. `tg_bot/evidence.py` 与 `tg_bot/workers/gather_executor.py` 只读取 `/var/lib/morning-report/today_report.txt`，没有候选事件、跨天历史或热度分数。
3. `tg_bot/storage.py:update_today_index` 只按单次搜索条目 ID 去重，无法识别跨媒体同一事件。
4. 新闻接口可以提供日期/新鲜度/相关性，但没有统一社会热度字段；不能把供应商排序直接宣称为热度。

## 设计方案

### A. 统一回复结构

新增 `tg_bot/response.py`，提供纯函数：

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ReplyEnvelope:
    conclusion: str
    evidence: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    sources: tuple[dict, ...] = ()
    confidence: str = "unknown"
    mode: str = "answer"

def render_reply(envelope: ReplyEnvelope, *, max_chars: int = 3800) -> str: ...
def normalize_reply(text: str, *, sources=(), mode="answer") -> ReplyEnvelope: ...
```

渲染规则：

- `conclusion` 永远排第一；不能为空，缺失时使用“目前资料不足，无法确认”。
- `evidence` 最多 4 条，每条只保留可由素材支持的事实，不放内部思考过程。
- `actions` 只有用户需要决策、排查或操作时出现；闲聊不强行添加。
- `sources` 在搜索、新闻和日报模式显示 1–5 条标题/域名/URL；闲聊模式保留审计字段但不主动显示链接。
- `confidence` 只允许 `high`、`medium`、`low`、`unknown`，渲染为“把握：…”；不把模型自评当事实。
- 超长时按 conclusion → evidence → actions → sources 的优先级截断，并保留完整结构的标题，不截断 URL 中间部分。
- `normalize_reply` 兼容旧模型纯文本：识别已有标题，无法识别时把第一段作为 conclusion，其余段作为 evidence，不重写事实。

搜索回答与日报摘要都通过该渲染器；原始模型回复、`facts_json`、`source_index` 继续写审计日志。

### B. 日报候选与事件指纹

新增 `tg_bot/daily_report.py`，不依赖 Telegram 网络循环，便于 CLI、cron、systemd timer 和测试调用。

```python
@dataclass(frozen=True)
class NewsCandidate:
    category: str
    title: str
    summary: str
    url: str
    domain: str
    published_at: str | None = None
    relevance: float = 0.0
    explicit_heat: float | None = None
    source: str = ""

@dataclass(frozen=True)
class ReportEvent:
    event_id: str
    category: str
    title: str
    summary: str
    sources: tuple[NewsCandidate, ...]
    heat_score: float
    heat_basis: tuple[str, ...]
    status: str = "new"  # new | update

def normalize_candidate(raw: dict, category: str, source: str) -> NewsCandidate: ...
def event_fingerprint(candidate: NewsCandidate) -> str: ...
def cluster_candidates(candidates: list[NewsCandidate]) -> list[ReportEvent]: ...
def score_event(event: ReportEvent, now: datetime) -> tuple[float, tuple[str, ...]]: ...
def select_events(events, history, *, per_category=4, cooldown_days=14): ...
def render_daily_report(events, generated_at: datetime) -> str: ...
```

指纹规则：URL 先去跟踪参数、统一 host 和路径；标题转小写、去标点/日期/媒体前缀、分词并移除常见停用词；用规范化标题 token 集与 URL slug 生成 SHA-256 前 16 位。相似标题 token Jaccard >= 0.65 或 URL slug 相同视为同一事件，保留最多 5 个独立域名来源。

### C. 热度评分与选择

事件评分固定在 0–100，便于日志和回归测试：

```text
热度 = 35% 新鲜度 + 25% 独立来源覆盖 + 15% 权威性
     + 15% 查询相关性 + 10% 显式热度信号（缺失时重新归一化）
```

- 新鲜度：发布 6 小时内 100 分，24 小时内线性降到 60，超过窗口淘汰；没有时间字段的候选最多 40 分。
- 独立来源覆盖：不同域名数量 capped at 5；重复转载不增加分数。
- 权威性：官方、政府、主流媒体和项目原始公告使用现有域名白名单；只依据来源类型，不依据搜索排名。
- 查询相关性：使用候选 API 的 relevance/score，缺失时为 0.5。
- 显式热度：只接受供应商明确提供的互动/趋势字段；缺失时从其他四项按权重重新归一化，并标记“多源关注”而不是“全网最热”。

选择器先过滤冷却窗口内的 `event_id`，同一事件若出现新的官方公告或关键数字变化且距上次发布至少 24 小时，则以 `status=update` 允许再次出现。之后按类别配额、域名多样性和主题多样性做贪心选择；不足时不拿旧事件填充，而是在报告尾部注明“今日新鲜候选不足”。

### D. 持久化与兼容

新增配置：

```text
DAILY_REPORT_CATEGORIES=china,global,ai_tech
DAILY_REPORT_ITEMS_PER_CATEGORY=4
DAILY_REPORT_COOLDOWN_DAYS=14
DAILY_REPORT_TIMEZONE=Asia/Shanghai
DAILY_REPORT_STATE_FILE=<data_dir>/daily_report_state.json
DAILY_REPORT_STATUS_FILE=<data_dir>/daily_report_status.json
```

`daily_report_state.json` 使用版本化结构：

```json
{
  "schema_version": 1,
  "events": {
    "a1b2c3d4e5f60708": {
      "last_published": "2026-07-12T13:00:00+08:00",
      "first_seen": "2026-07-12T09:10:00+08:00",
      "title": "…",
      "sources": ["reuters.com", "apnews.com"],
      "heat_score": 82.4
    }
  }
}
```

`daily_report_status.json` 单独记录 `fresh` 或 `stale_previous`、生成时间、事件数量和供应商诊断；全供应商失败时不覆盖上一次 TXT/JSON，但会写入 `stale_previous`，便于监控发现日报已过期。

旧 `today_report.txt` 继续作为 `/recap` 和 `read_today_report` 的兼容产物；新增 `daily_report.json` 保存机器可读候选与热度依据。状态文件采用原子写入，损坏时备份为 `.corrupt.<timestamp>` 并从空状态恢复，同时记录告警。

### E. 运行入口与失败策略

新增 `scripts/build-daily-report.py`：读取环境变量、调用候选采集器、生成 JSON/TXT 并原子替换。systemd timer 示例每天 13:00 运行；失败时不覆盖上一份有效日报，并向 stderr 返回结构化错误。

如果某一搜索供应商失败，继续使用其他供应商；新闻采集层返回结构化的发布时间、相关性、供应商和诊断，不从展示文本反解析热度。若所有供应商失败，生成“今日采集失败，沿用上一份报告供回顾”的状态记录，不覆盖旧文件、不伪造新事件。没有足够新鲜事件时宁缺毋滥。

## 测试策略

- `test_core_units.py` 增加 `ReplyStructureTests`：空结论兜底、标题识别、来源显示策略、长度截断和不泄露 reasoning。
- 增加 `DailyReportTests`：URL/标题规范化、同事件聚类、14 天冷却、官方更新重现、缺失热度字段归一化、类别/域名配额和旧报告兼容。
- CLI 测试使用临时目录和注入的候选，不访问真实网络；单独保留现有 API 测试。
- 完成后运行 `compileall`、完整 unittest、`git diff --check` 和 `scripts/tg-bot-check.py`。

## 不在本次范围

- 不引入新的付费热度 API、社交媒体爬虫或数据库服务。
- 不改变 Telegram 权限模型、HTTP API 认证或已有搜索工具名称。
- 不把模型 reasoning 或内部来源标记暴露给用户。
