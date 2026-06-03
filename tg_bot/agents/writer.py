#!/usr/bin/env python3
"""agents/writer.py — 写作 AI（职责单一化版）

职责（只做这三件事）：
  1. 基于 WriteRequest.sources 写正文
  2. 每个具体事实后标注 [来源N]
  3. 控制字数在 target_words 区间内

不做的事：
  - 不选材（那是 Curator 的事）
  - 不核查（那是 Critic 的事）
  - 不决定是否需要搜索

与旧 pipeline/write.py 的区别：
  - 只有一个字数信号（target_words），不再有 6 个互相冲突的 prompt
  - source 格式统一为 [来源N]，与 Critic 一致
  - prompt 从 280 行缩到核心约束

返回 (reply: str, reasoning: str)
"""
from __future__ import annotations
import logging
import re

from tg_bot.core.contracts import WriteRequest
from tg_bot.agents.curator import target_words_to_max_tokens
from tg_bot.workers.source_utils import format_sources_for_writer

log = logging.getLogger(__name__)

_SYS_WRITER = """\
你是一个信息整理助手。你会收到用户问题和一组编号的原文素材。

━━━ 写作规则 ━━━
1. 只使用素材里有的信息，不发明、不推断、不脑补。
2. 每句包含具体事实的话，在句末标注来源：[来源N]
   多条素材支持同一句时：[来源1][来源3]
3. 纯过渡句不需要标注。
4. 素材里找不到的内容一律不写。

━━━ 排版规则 ━━━
1. 用 emoji 做分组标题，每组之间空一行。
2. 同组内多个数据写在同一行，用 · 隔开。
3. 时间序列用 → 连接。
4. 第一行是最重要的结论，不加标题符号。
5. 不用 Markdown（不加 **、__、#、---）。
6. 不说"好的""以下是""综上"。

━━━ 被退回时（reject_feedback 非空）━━━
- [SOFT]：保留信息点，改写措辞贴近原文
- [HARD]：删除或替换为"目前资料未明确说明"
- 未被标记的句子保持不变
- 只改问题句子，不重写整篇
"""


def write(req: WriteRequest) -> tuple[str, str]:
    """
    执行写作。返回 (reply, reasoning)。
    reasoning 是 DeepSeek thinking 内容，供审计用。
    """
    import tg_bot.config as _cfg
    from tg_bot.tools.fetch import http_post

    # ── 构建素材文本 ──────────────────────────────────────────────────
    sources_text = format_sources_for_writer(req.sources, max_body=2500)
    if not sources_text:
        return "暂未获取到相关信息，请稍后再试或换个问法。", ""

    # ── 构建用户消息 ──────────────────────────────────────────────────
    lo, hi = req.target_words
    user_content = (
        f"用户问题：{req.user_query}\n\n"
        f"原文素材（每条已编号，写作时用 [来源N] 标注出处）：\n{sources_text}\n\n"
        f"【字数要求】{lo}～{hi} 字。"
        f"素材够用时写到 {hi} 字；素材不足时写实际能写的，不要凑字数。"
    )

    # ── 注入 style hints ──────────────────────────────────────────────
    if "intro_style" in req.style_hints:
        if hi <= 400:
            user_content += (
                "\n【简介格式】用户要求短文。用 2-3 段紧凑介绍，优先覆盖性质、位置、核心特征。"
                "不要强行展开 5 条，不要超过上方字数上限。"
            )
        else:
            user_content += (
                "\n【简介格式】请覆盖：地点/性质 · 历史/身份 · 规模/层次 · 特色 · 最突出细节。"
                "用 5 条要点展开，每条包含一个具体事实。"
            )
    if "how_to_style" in req.style_hints:
        user_content += (
            "\n【建议格式】按「提前准备 / 发生时 / 事后」分组。"
            "只写素材能直接支撑的具体动作，不写泛泛建议。"
        )
    if "single_item" in req.style_hints:
        user_content += (
            "\n【单条输出】用户明确只要一个/一条。只选一个最有趣且证据最清楚的事实作为主结论。"
            "可以附 1-2 句解释，但禁止并列列出第二、第三个冷知识/事实/例子。"
        )

    # ── 处理 Critic 退回 ─────────────────────────────────────────────
    if req.reject_feedback:
        user_content += (
            f"\n\n【核查退回，请按以下问题修改后重新写完整回复】\n{req.reject_feedback}\n"
            "修改规则：[SOFT] 改写、[HARD] 删除、未标记保持不变。"
            "只输出修改后的完整干净正文，不写草稿标记或修改说明。"
        )

    # ── 组装 messages ────────────────────────────────────────────────
    msgs = []
    if req.history_context:
        msgs.extend(req.history_context[-6:])
    msgs.append({"role": "user", "content": user_content})

    max_tokens = target_words_to_max_tokens(req.target_words)

    try:
        import tg_bot.config as _cfg
        resp = http_post(
            "https://api.deepseek.com/chat/completions",
            {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "system", "content": _SYS_WRITER}] + msgs,
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "thinking": {"type": "enabled", "budget_tokens": 500},
            },
            headers={"Authorization": f"Bearer {_cfg.DEEPSEEK_KEY}"},
            timeout=90,
        )
        if not resp or not resp.get("choices"):
            return "写作 AI 无响应，请稍后再试。", ""

        msg = resp["choices"][0]["message"]
        reasoning = (msg.get("reasoning_content") or "").strip()
        reply = (msg.get("content") or "").strip()
        citation_count = len(re.findall(r'\[来源\d+\]', reply))
        log.info(f"✍️ Writer 完成，{len(reply)} 字，来源引用：{citation_count} 处")
        return reply, reasoning

    except Exception as e:
        log.warning(f"writer 异常: {e}")
        return "生成回复时出错，请稍后再试。", ""
