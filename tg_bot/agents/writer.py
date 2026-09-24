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
  - 对话历史不再作为 messages 直接喂给模型（避免模型把历史当成可引用的事实来源），
    改成 user_content 里一段只用于理解指代的"对话背景"

返回 (reply: str, reasoning: str)
"""
from __future__ import annotations
import logging
import os
import re
from datetime import datetime, timezone, timedelta

from tg_bot.core.contracts import WriteRequest
from tg_bot.agents.curator import target_words_to_max_tokens
from tg_bot.workers.source_utils import format_sources_for_writer

log = logging.getLogger(__name__)

_SYS_WRITER = """\
你是一个信息整理助手。你会收到用户问题和一组编号的原文素材。

━━━ 写作规则 ━━━
1. 只使用素材里有的信息，不发明、不推断、不脑补。
2. 每句包含具体事实的话（数字、日期、人名、机构、排名等），句末必须标注来源：[来源N]
   多条素材支持同一句时：[来源1][来源3]。标不出来源的事实，不要写。
3. 纯过渡句不需要标注。
4. 素材里找不到的内容一律不写；素材不足以回答的部分，明确写"目前资料未明确说明 X"，
   不要用常识或印象补全。
5. 数字照抄素材原文（单位、精度都不改），不自行换算、不四舍五入、不求和求差；
   除非用户明确要求计算，且在句中写明"按来源N、来源M计算"。
6. 素材有明确发布日期时，涉及时效的说法写具体日期（如"截至7月20日"），
   不写"今天/最新/目前"，除非素材日期本身就是今天。
7. 素材之间对同一件事有冲突：分别写出各自说法并标注各自来源，不擅自裁决哪个对。

━━━ 排版规则 ━━━
1. 按以下固定顺序输出；空板块省略标题：结论、关键依据、下一步、来源。
2. 结论放在第一行，直接回答用户问题，不加"好的""以下是""综上"。
3. 关键依据只列素材直接支持的事实，每条一行并保留 [来源N] 标记。
4. 下一步只在用户需要决策、排查或操作时给出具体动作，不写泛泛建议。
5. 来源只列素材中真实出现的标题/域名/URL，不要伪造来源；闲聊不主动添加来源段。
6. 用 emoji 做分组标题，每组之间空一行；同组内多个数据用 · 隔开，时间序列用 → 连接。
7. 不用 Markdown（不加 **、__、#、---）。

━━━ 被退回时（reject_feedback 非空）━━━
- [SOFT]：保留信息点，改写措辞贴近原文
- [HARD]：删除或替换为"目前资料未明确说明"
- 未被标记的句子保持不变
- 只改问题句子，不重写整篇
"""


def _writer_temperature() -> float:
    try:
        return float(os.environ.get("WRITER_TEMPERATURE", "0.3"))
    except (TypeError, ValueError):
        return 0.3


def _bj_now_line() -> str:
    now = datetime.now(timezone(timedelta(hours=8)))
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return f"【当前日期】今天是 {now.strftime('%Y年%m月%d日')} {weekdays[now.weekday()]}（北京时间）"


def _history_background(history_context: list[dict]) -> str:
    """把对话历史压成一段"背景说明"，而不是当作 messages 直接喂给模型。

    只用于理解指代和追问对象，不能被当作事实来源引用其中的数字。
    """
    if not history_context:
        return ""
    turns = [h for h in history_context if h.get("role") in ("user", "assistant", "memory")][-8:]
    if not turns:
        return ""
    lines = []
    for turn in turns[-8:]:
        role_raw = turn.get("role")
        role = {"user": "用户", "assistant": "助手", "memory": "长期记忆"}.get(role_raw, role_raw)
        content = (turn.get("content") or "").strip().replace("\n", " ")[:200]
        if content:
            lines.append(f"{role}：{content}")
    if not lines:
        return ""
    return (
        "\n【对话背景（只用于理解指代和追问对象，不能作为事实来源，不得引用其中的数字）】\n"
        + "\n".join(lines[-8:])
    )


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
        f"{_bj_now_line()}\n\n"
        f"用户问题：{req.user_query}\n\n"
        f"原文素材（每条已编号，写作时用 [来源N] 标注出处）：\n{sources_text}\n\n"
        f"【字数要求】{lo}～{hi} 字。"
        f"素材够用时写到 {hi} 字；素材不足时写实际能写的，不要凑字数。"
    )

    if not req.evidence_sufficient:
        gaps = req.evidence_gaps or "部分信息未能采集到"
        user_content += (
            f"\n\n【素材不完整（缺口：{gaps}）】只写能被素材确认的部分，"
            "末尾用一行说明哪些点资料未能确认，不要用常识或推测补全。"
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

    # ── 对话背景（不作为 messages，只作为 user_content 里的说明段）─────
    background = _history_background(req.history_context)
    if background:
        user_content += "\n" + background

    # ── 处理 Critic 退回 ─────────────────────────────────────────────
    if req.reject_feedback:
        user_content += (
            f"\n\n【核查退回，请按以下问题修改后重新写完整回复】\n{req.reject_feedback}\n"
            "修改规则：[SOFT] 改写、[HARD] 删除、未标记保持不变。"
            "只输出修改后的完整干净正文，不写草稿标记或修改说明。"
        )

    msgs = [{"role": "user", "content": user_content}]

    max_tokens = target_words_to_max_tokens(req.target_words)

    try:
        resp = http_post(
            "https://api.deepseek.com/chat/completions",
            {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "system", "content": _SYS_WRITER}] + msgs,
                "max_tokens": max_tokens,
                "temperature": _writer_temperature(),
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
        log.info(f"✍️ Writer 完成，{len(reply)} 字，来源引用：{citation_count} 处，temp={_writer_temperature()}")
        return reply, reasoning

    except Exception as e:
        log.warning(f"writer 异常: {e}")
        return "生成回复时出错，请稍后再试。", ""
