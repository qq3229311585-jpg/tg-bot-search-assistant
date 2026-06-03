#!/usr/bin/env python3
"""agents/patcher.py — 补丁 AI（只做最小改动，不重新构思）

职责（只做这一件事）：
  输入：草稿文本 + CriticReport（含 Issue 列表）
  输出：修改后的文本

不做的事：
  - 不判断哪里有问题（那是 Critic 的事）
  - 不重新搜索信息
  - 不重新构思段落结构

补丁原则：
  - [SOFT]：保留信息点，改写措辞更贴近原文（不删除）
  - [HARD]：删除整句，或替换为"目前资料未明确说明"
  - 未被标记的句子：一字不动
"""
from __future__ import annotations
import logging

from tg_bot.core.contracts import CriticReport, Source
from tg_bot.workers.source_utils import format_sources_for_writer

log = logging.getLogger(__name__)

_SYS_PATCHER = """\
你是一个文本补丁执行器。

你的任务极其有限：
- 收到一篇草稿 + 一份问题清单
- 按清单对草稿做最小改动
- 输出修改后的完整文本

补丁规则（严格执行）：
- [SOFT] 问题：保留信息点，改写措辞使其更贴近原文。不要删除整句。
- [HARD] 问题：删除整句，或替换为"目前资料未明确说明"。
- 未被列为问题的句子：一字不动，禁止润色、调整顺序、扩写。
- 禁止：在输出中写"已修改""原文""改为""←"等任何标注
- 只输出最终干净文本，不写任何说明或注释。
"""


def patch(
    reply: str,
    report: CriticReport,
    sources: list[Source],
    user_query: str = "",
) -> str:
    """
    按 CriticReport 的 issues 对 reply 做最小改动。
    失败时返回原始 reply（宁可原文也不引入新错误）。
    """
    import tg_bot.config as _cfg
    from tg_bot.tools.fetch import http_post

    if not report.issues:
        return reply

    # 构建问题清单文本
    issue_lines = []
    for iss in report.issues:
        tag = f"[{iss.severity}]"
        line = f"{tag} 原句：「{iss.sentence[:100]}」\n  原因：{iss.reason}"
        if iss.suggested_fix:
            line += f"\n  建议改法：{iss.suggested_fix}"
        if iss.evidence_excerpt:
            line += f"\n  原文片段：{iss.evidence_excerpt[:100]}"
        issue_lines.append(line)

    sources_brief = format_sources_for_writer(sources, max_body=800)

    user_msg = (
        f"【需要修改的草稿】\n{reply}\n\n"
        f"【问题清单（只改这些，其余一字不动）】\n" + "\n\n".join(issue_lines)
    )
    if sources_brief:
        user_msg += f"\n\n【原文参考（改写 SOFT 问题时参考）】\n{sources_brief}"

    try:
        resp = http_post(
            "https://api.deepseek.com/chat/completions",
            {
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": _SYS_PATCHER},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 3000,
                "temperature": 0.1,
                "thinking": {"type": "disabled"},  # patch 是机械执行，不需要推理
            },
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_VERIFY_KEY}"},
            timeout=60,
        )
        if resp and resp.get("choices"):
            patched = (resp["choices"][0]["message"].get("content") or "").strip()
            if patched:
                log.info(f"🔧 Patcher 完成，{len(patched)} 字（原 {len(reply)} 字）")
                return patched
    except Exception as e:
        log.warning(f"patcher 异常: {e}")

    log.warning("Patcher 失败，返回原稿")
    return reply
