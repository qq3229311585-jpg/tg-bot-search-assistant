#!/usr/bin/env python3
"""core/contracts.py — 模块间通信的强类型契约

每个 agent/worker 的输入输出只能是这里定义的 dataclass。
检查错误时：找到出错模块的输入 dataclass，对照字段就能定位问题。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional


# ── 来源条目（Searcher / Fetcher → Curator → Writer）────────────────────────

@dataclass
class Source:
    id: str
    url: str
    domain: str
    title: str
    snippet: str
    full_content: str = ""      # Fetcher 回填，空串=未抓取
    tool: str = ""              # 来源工具名: web_search / fetch_content / wikipedia_lookup / …
    query: str = ""             # 触发此来源的搜索词
    score: float = 0.0          # Curator 打分后设置
    fetched_at: Optional[datetime] = None

    @property
    def body(self) -> str:
        """写作/核查时使用的主要文本（优先全文，其次摘要）"""
        return (self.full_content or self.snippet or "").strip()

    def is_empty(self) -> bool:
        return not self.body


# ── 查询变体（QueryFixer → Searcher）────────────────────────────────────────

@dataclass
class QueryPlan:
    original: str               # 用户原始问题
    variants: list[str]         # 1-3 个改写后的搜索查询变体
    intent: Literal["chat", "tool", "search", "history", "system"]
    keywords: list[str]         # 核心关键词（用于 source 相关性过滤）
    suggested_tool: str = ""    # 消歧层建议的直接工具（可为空）
    user_deferred: bool = False # 用户说"你来定"——不再追问


# ── 写作请求（Curator → Writer）─────────────────────────────────────────────

@dataclass
class WriteRequest:
    user_query: str
    sources: list[Source]       # 已由 Curator 排序、编号（index+1 = 来源编号）
    target_words: tuple[int, int]   # (min, max) 字数区间
    history_context: list[dict] = field(default_factory=list)  # 最近几轮对话
    style_hints: list[str] = field(default_factory=list)       # ["use_emoji", "intro_style"]
    reject_feedback: str = ""       # Critic 退回时的具体问题（重写用）
    verifier_checks: list[dict] = field(default_factory=list)  # Critic 原始 check 列表


# ── 核查报告（Critic 输出）──────────────────────────────────────────────────

@dataclass
class Issue:
    sentence: str               # 有问题的原句
    source_ref: str             # 引用的来源编号，如 "来源3"
    severity: Literal["SOFT", "HARD"]
    reason: str                 # 一句话说明问题
    suggested_fix: str = ""     # Patcher 可参考的修改建议
    evidence_excerpt: str = ""  # 原始素材中的对应原文


@dataclass
class CriticReport:
    verdict: Literal["pass", "patch", "rewrite", "unknown"]
    issues: list[Issue] = field(default_factory=list)
    # pass: 无问题，直接发送
    # patch: 有 SOFT/HARD 问题，交 Patcher 做最小改动
    # rewrite: 问题太多，交 Writer 重写
    # unknown: 审核模型/JSON 格式失败，记录但不强制重写


# ── 补丁指令（Critic → Patcher）────────────────────────────────────────────

@dataclass
class PatchInstruction:
    original_sentence: str
    new_sentence: Optional[str]     # None = 删除整句
    reason: str


# ── 模块开关（从环境变量 / tg-bot.env 读取）─────────────────────────────────

@dataclass
class PipelineConfig:
    query_fixer: bool = True    # False = 直接用原始 query
    curator: bool = True        # False = source 按 score 简单排序
    critic: bool = True         # False = Writer 出稿直接发
    patcher: bool = True        # False = Critic REJECT → 重新跑 Writer
    cache: bool = True          # False = 不用今日索引缓存
    max_rewrites: int = 2       # 审核修正轮次上限；pipeline 会按风险进一步收紧

    @classmethod
    def from_env(cls) -> "PipelineConfig":
        import os
        def _bool(k, default=True):
            return os.environ.get(k, str(default)).lower() not in ("false", "0", "no")
        return cls(
            query_fixer=_bool("PIPELINE_QUERY_FIXER"),
            curator=_bool("PIPELINE_CURATOR"),
            critic=_bool("PIPELINE_CRITIC"),
            patcher=_bool("PIPELINE_PATCHER"),
            cache=_bool("PIPELINE_CACHE"),
            max_rewrites=int(os.environ.get("PIPELINE_MAX_REWRITES", "2")),
        )
