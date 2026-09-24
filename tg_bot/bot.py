#!/usr/bin/env python3
"""bot.py — Telegram 消息处理主循环"""

import json, re, time, logging, threading
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request

from tg_bot.config import (
    FEATURES_FILE,
    BOT_TOKEN, ALLOWED_CHAT, MAX_HISTORY,
    THINKING_FILE, TOOLLOG_FILE, OFFSET_FILE, DATA_DIR,
    QUOTA_FILE, API_FREE_LIMITS,
    _quota_warnings,
    SOURCES_DIR,
    ensure_data_dir, validate_config,
)
from tg_bot.bot_utils import tg, send, typing, md_to_html
from tg_bot.file_io import atomic_write_text
from tg_bot.detailed_log import save_detailed_log
from tg_bot.storage import (
    append_daily_log,
    load_history, save_history, load_summary, save_summary, load_report,
    load_context, save_context, load_thinking, load_toollog, save_thinking_entry,
    load_focus, save_focus, clear_focus,
    save_write_thinking, save_toollog_entry, fmt_toollog_for_prompt,
    load_quota, save_quota, fmt_quota, save_worklog_entry, fmt_worklog,
    auto_cleanup, save_sources_file, load_today_index, list_sources_files,
    read_sources_file,
)
from tg_bot.memory import build_memory_context
from tg_bot.progress import TelegramProgress
from tg_bot.tools.native import execute_api_balance
from tg_bot.tools.fetch import execute_read_cache
from tg_bot.pipeline import (
    _pre_check, gather_ai, write_ai, ds_chat, fast_chat,
    verify_reply, patch_by_verifier, summarize_for_context,
    build_execution_report,
)
from tg_bot.evidence import (
    should_use_vps_traffic, build_vps_traffic_pack,
    should_use_today_report, build_today_report_pack,
)
from tg_bot.workers.display import clean_reply_for_user, append_verification_note
from tg_bot.workers.source_utils import cache_match_score
from tg_bot.lanes.router import decide_lane
from tg_bot.commands.info import (
    handle_thinking, handle_tools, handle_sources,
    handle_source_detail, handle_worklog, handle_diary,
)
from tg_bot.commands.quota import handle_balance, handle_quota, handle_quota_set
from tg_bot.commands.control import (
    handle_start, handle_help, handle_clear, handle_recap, handle_ask,
)
from tg_bot.ask_server import (
    _load_or_create_ask_token, _run_ask_server, ASK_TOKEN_FILE,
)
from tg_bot.search_policy import decide_search_policy, _SOCIAL_RE
import tg_bot.ask_server as _ask_server_mod
import tg_bot.config as _cfg

log = logging.getLogger(__name__)

_NO_REFERENCES_MARK = "（AI-no references）"



def _mark_no_references(reply):
    """Mark replies that were generated without tool/reference access."""
    reply = (reply or "").rstrip()
    if not reply or _NO_REFERENCES_MARK in reply:
        return reply
    return reply + "\n\n" + _NO_REFERENCES_MARK


def _render_display_reply(raw_reply, *, meta=None, mode="answer"):
    """Keep the original plain-text layout while hiding source markers."""
    return clean_reply_for_user(raw_reply or "")


def _run_search_with_progress(progress, runner, *args, **kwargs):
    """Attach the visible progress callback to one search-pipeline run."""
    kwargs["progress_cb"] = progress.stage
    return runner(*args, **kwargs)


def _send_reply_with_progress(progress, send_func, chat_id, text):
    """Send the final reply and leave the visible progress message truthful."""
    progress.sending()
    delivered = send_func(chat_id, text)
    if delivered is False:
        progress.fail("发送失败，请稍后重试")
    else:
        progress.complete()
    return delivered


def _build_fast_system_prompt(parts):
    """快速路径专用系统提示：只用通用人设/格式/记忆部分，替换掉搜索路径专用规则。

    快速路径没有任何工具（fast_chat 不带 tools 定义），所以不能沿用完整版
    parts 里"先调 wikipedia_lookup""调 web_search"这类工具流程指令——那些指令
    在快速路径里根本执行不了，只会让模型自相矛盾。这里显式告诉模型它此刻没有
    工具，遇到需要查证的问题要引导用户去 /ask，而不是编造"我去查一下"。
    也不再有"给自己打把握分（0-100）"这种自评机制——不确定就是不确定，
    没有分数能让不确定的内容变得可信。
    """
    reminder = (
        "【当前回答路径】你现在直接使用自己的知识回答，没有调用任何搜索工具，"
        "没有查询外部网络。回答时不要出现「根据搜索结果」「据我查询」"
        "「刚才搜索到」「我去查一下」「正在搜索」等措辞；如涉及可能变化的信息，"
        "要明确说明没有实时核验。\n"
        "你此刻没有任何工具，不能搜索：只回答常识、闲聊、情感陪伴、计算、写作、代码类内容。\n"
        "遇到需要最新信息、具体数据、冷门事实的问题：直接说明「这个需要查资料，"
        "发送 /ask + 问题 我去搜」，可以给出你知道的大致方向，但不给具体数字、日期、排名——"
        "没有核实过的数字说了也是猜的。"
    )
    return "\n".join(list(parts or []) + [reminder])


# ── 上下文搜索继承（B2）：抽成纯函数，方便单独测试 ──────────────────────────
_NO_INHERIT_SPEECH_ACTS = {"social_chat", "emotion", "meta_question", "rewrite"}
_INHERIT_FOLLOWUP_RE = re.compile(
    r"(多说|继续|再说|展开|详细|说说|为什么|怎么|咋|啊原因|"
    r"哪|能说|讲讲|告诉我|还有|然后|接着|更多|深入|具体|"
    r"举例|比如|真的吗|是这样吗|那时候|后来|结果|影响)"
)


def _should_inherit_search_context(text, pre, route_info):
    """判断当前这句是否应该继承上一轮的搜索上下文（前提：上一轮确实用过工具）。

    收紧规则：闲聊/情绪/元问题/改写类话语行为，或命中社交短句正则，一律不继承；
    只有明确的"延续同一话题"信号（topic_continuity / continue_previous / 追问正则）
    才继承。不用"≤25 字"当继承条件——短句不等于追问。
    """
    text = (text or "").strip()
    pre = pre or {}
    route_info = route_info or {}
    if route_info.get("category") in ("元问题", "改写", "系统查询", "闲聊", "情感闲聊"):
        return False
    if pre.get("speech_act") in _NO_INHERIT_SPEECH_ACTS:
        return False
    if _SOCIAL_RE.match(text):
        return False
    if pre.get("topic_continuity") == "continue_same_topic":
        return True
    if pre.get("speech_act") == "continue_previous":
        return True
    return bool(_INHERIT_FOLLOWUP_RE.search(text))


_BLOCKED_SOURCE_DOMAINS = {"baike.baidu.com", "www.baike.baidu.com"}


def _is_blocked_source(entry):
    entry = entry or {}
    domain = (entry.get("domain") or "").lower()
    title = (entry.get("title") or "").lower()
    query = (entry.get("query") or "").lower()
    blob = " ".join([domain, title, query])
    return (
        domain in _BLOCKED_SOURCE_DOMAINS
        or "baike.baidu.com" in blob
        or "百度百科" in blob
    )


def _has_evidence(meta=None):
    meta = meta or {}
    return bool(
        meta.get("source_index")
        or meta.get("fetched_pages")
        or meta.get("tool_calls_summary")
    )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ── 概念问题检测 + Wikipedia 强制预查 ────────────────────────────────
_CONCEPT_RE = re.compile(
    r'(啥是|什么是|是什么|是啥|咋理解|怎么理解|怎么定义|的概念|的定义|的原理|的意思'
    r'|解释.{0,4}|介绍.{0,4}|讲讲.{0,4}|说说.{0,4}|告诉我.{0,4}'
    r'|what\s+is|what\s+are|explain|define)',
    re.IGNORECASE
)
_STRIP_RE = re.compile(
    r'(啥是|什么是|是什么|是啥|咋理解|怎么理解|怎么定义|的概念|的定义|的原理|的意思'
    r'|解释|介绍|讲讲|说说|告诉我|what\s+is|what\s+are|explain|define'
    r'|你知道(是啥|是什么|吗|不|啊|吧)?'
    r'|[？?！!，,。.\s呢吧啊哦嘛]+)',
    re.IGNORECASE
)

# 知识域关键词——触发后不管问法，直接查 Wikipedia
_KNOWLEDGE_RE = re.compile(
    r'(神话|传说|典故|神兽|妖怪|仙侠|成语|俗语|谚语|歇后语|诗词|古诗|文言|古文'
    r'|朝代|王朝|皇帝|历史|古代|古典|文明|民族|战役|古国'
    r'|来历|由来|起源|发明|发现|进化|原理|定律|公式|元素|反应'
    r'|天文|星座|星系|黑洞|行星|物理|化学|生物|医学|解剖|基因|病毒'
    r'|地理|地貌|山脉|河流|海洋|气候|地质'
    r'|法律|法规|条文|宪法|刑法)',
    re.IGNORECASE
)
# 来源追问检测——触发时注入上轮搜索摘要（Method C）
_SOURCE_QUERY_RE = re.compile(
    r'(查了没|查过没|查没查|哪来的|哪里来的|你查了吗|你引用|有没有查|搜了没|搜过没'
    r'|来源|出处|依据|根据什么|从哪搜|查到的|是搜的|瞎编的|没搜|没查'
    r'|你怎么知道|哪篇|哪个网站|参考了|参考的)',
    re.IGNORECASE
)

# 清洗口语噪声，剩下的作为 Wikipedia 搜索词
_NOISE_RE = re.compile(
    r'(我记得|我觉得|我感觉|听说|好像|应该|可能|那不是|这不是|不就是'
    r'|你说|对吧|是吧|对不对|是不是|有没有|咋回事|咋说'
    r'|[？?！!，,。.\s「」『』【】〔〕（）()]+)',
    re.IGNORECASE
)

# 缓存命中评分复用 workers.source_utils.cache_match_score（同一份规则，
# 不再各写一份带写死特判的副本——B5 要求去掉针对个别问题的写死加分）。

def _detect_concept(text):
    """明确概念/定义类问法 → 返回核心词"""
    if not _CONCEPT_RE.search(text):
        return ""
    concept = _STRIP_RE.sub(" ", text).strip()
    concept = re.sub(r'^(我想知道|你能不能|帮我|我想问|请问|能不能告诉我)\s*', '', concept).strip()
    concept = re.sub(r'[\s]+(不|吗|啊|吧|哦|嘛|呀|是|对|么)\s*$', '', concept).strip()
    concept = re.sub(r'\s+', ' ', concept).strip()
    return concept[:40] if len(concept) >= 2 else ""

def _detect_knowledge(text):
    """话题涉及知识域（不管问法）→ 返回清洗后的搜索词"""
    if not _KNOWLEDGE_RE.search(text):
        return ""
    q = _NOISE_RE.sub(" ", text).strip()
    q = re.sub(r'\s+', ' ', q).strip()
    return q[:40] if len(q) >= 2 else ""


def sync_to_openhuman_memory(user_text, reply_text, source="telegram",
                             tools_used=None, sources=None):
    """
    把本轮对话写入 OpenHuman 记忆树。失败不抛异常，仅记日志。
    source: "telegram" 或 "openhuman"（区分对话发起渠道）
    """
    from tg_bot.config import OPENHUMAN_RPC_URL, OPENHUMAN_RPC_TOKEN, OPENHUMAN_NS, _ctx
    try:
        ts = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
        key = f"{source}-{ts}"
        # content 用结构化文本，便于 OpenHuman 之后召回时阅读
        content_parts = [
            f"【用户问题】\n{user_text}",
            f"【助手回复】\n{reply_text}",
        ]
        if tools_used:
            content_parts.append(f"【调用工具】{', '.join(tools_used[:8])}")
        if sources:
            src_lines = [f"  - {s.get('domain','')}: {s.get('title','')[:50]}"
                         for s in sources[:5] if s.get('domain')]
            if src_lines:
                content_parts.append("【参考来源】\n" + "\n".join(src_lines))
        content = "\n\n".join(content_parts)

        title = user_text[:60].replace("\n", " ")
        tags  = [source, "chat"]
        if tools_used:
            tags.append("with-tools")

        body = json.dumps({
            "jsonrpc": "2.0", "method": "openhuman.memory_doc_ingest", "id": 1,
            "params": {
                "namespace": OPENHUMAN_NS,
                "key": key, "title": title, "content": content,
                "source_type": source, "tags": tags,
                "metadata": {"ts": ts, "channel": source},
            }
        }).encode()
        req = Request(OPENHUMAN_RPC_URL, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENHUMAN_RPC_TOKEN}",
        })
        with urlopen(req, context=_ctx, timeout=10) as r:
            d = json.loads(r.read())
            if d.get("error"):
                log.warning(f"📤 写入 OpenHuman 记忆失败: {d['error']}")
            else:
                doc_id = d.get("result", {}).get("result", {}).get("documentId", "")
                log.info(f"📤 写入 OpenHuman 记忆 ✓ doc={doc_id[:20]} ({source})")
    except Exception as e:
        log.debug(f"OpenHuman 记忆写入跳过（可能未运行）: {e}")


def summarize(msgs):
    text = "\n".join(
        f'{"你" if m["role"]=="user" else "助手"}：{m["content"]}'
        for m in msgs if m["role"] in ("user", "assistant")
    )
    return fast_chat(
        [{"role": "user", "content": f"请用150字以内总结以下对话的主要内容和结论：\n\n{text}"}],
        system="你是总结助手，用简洁中文提炼对话要点。",
        max_tokens=250, temp=0.3
    )



def _now_ts() -> str:
    """返回当前北京时间 ISO 字符串，用于历史记录时间戳"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S")

def _fmt_time_ago(ts_str: str) -> str:
    """把历史消息的时间戳格式化为'X分钟前/X小时前'"""
    if not ts_str:
        return ""
    try:
        msg_time = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8)))
        now = datetime.now(timezone(timedelta(hours=8)))
        diff = int((now - msg_time).total_seconds())
        if diff < 60:
            return "刚刚"
        elif diff < 3600:
            return f"{diff // 60}分钟前"
        elif diff < 86400:
            h = diff // 3600
            m = (diff % 3600) // 60
            return f"{h}小时{m}分钟前" if m else f"{h}小时前"
        else:
            return f"{diff // 86400}天前"
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════════
# 路由分四层，先后顺序固定，谁也不覆盖谁；以后要加新规则先看清楚该插在哪一层，
# 不要在 handle() 里再叠一层临时判断：
#   ① 消歧（pipeline._pre_check / pipeline.disambig）
#      模型对这句话做语义判断，输出 pre dict（needs_search、speech_act、
#      topic_continuity 等）。模型不可用时落到 _heuristic_pre 兜底，
#      永远不会返回 None（不会再"默认搜索"地悄悄兜底）。
#   ② SearchPolicy（search_policy.decide_search_policy）
#      用确定性正则规则复核①的结论，能修正消歧层的明显误判（如"你讲吧"被
#      误判为需要搜索）。这一层的输出是 route_info，包含最终 route。
#   ③ 上下文继承（本函数内，紧跟 SearchPolicy 之后）
#      仅当②判定不需要搜索、但上一轮确实用过工具，且当前这句明确是"延续同一
#      话题的追问"（topic_continuity / continue_previous / 追问关键词命中）
#      时，才强制转回搜索路径。闲聊/情绪/元问题/改写/社交短句一律不继承。
#   ④ Lane（lanes.router.decide_lane）
#      前三层只回答"要不要搜索"；这一层把结果落到具体执行路径：
#      fast / search / local_tool（vps_traffic）/ report（today_report）/
#      system（系统查询、日历）。bot.py 之后按 lane 分发到对应分支。
# ══════════════════════════════════════════════════════════════════════════


def handle(chat_id, text, http_mode=False, brief=False):
    """
    处理一条消息。
    http_mode=True 时（OpenHuman 等外部接口调用）：
      · 不发任何 Telegram 状态消息（typing/采集中/重写中等）
      · 不写入 Telegram 的 history.json / context（避免污染对话历史）
      · 仍写 worklog/sources/toollog（这些是审计记录，公用）
      · 返回 reply 字符串，由调用方自行处理
    """
    text = (text or "").strip()

    # 闭包：在 http_mode 下变 no-op
    def _typing():
        if not http_mode:
            typing(chat_id)

    _typing()

    history = load_history()

    summary = load_summary()
    memory_turns = load_context()
    profile_text = ""
    try:
        profiles = json.loads(open(_cfg.USER_PROFILES_FILE, encoding="utf-8").read())
        for profile in reversed(profiles if isinstance(profiles, list) else []):
            if isinstance(profile, dict) and profile.get("profile"):
                profile_text = str(profile["profile"]).strip()[:1200]
                break
    except Exception:
        pass
    memory_context = build_memory_context(summary, memory_turns, profile=profile_text)
    report  = load_report()

    bj_now   = datetime.now(timezone(timedelta(hours=8)))
    bj_str   = bj_now.strftime("%Y年%m月%d日 %H:%M（北京时间）")
    weekdays = ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"]
    bj_week  = weekdays[bj_now.weekday()]

    # 从 features.md 动态读取功能清单，注入系统提示词
    try:
        _feat_lines = open(FEATURES_FILE, encoding="utf-8").readlines()
        _feat_items = [l.strip() for l in _feat_lines
                       if l.strip().startswith("· ")]
        _features_block = (
            "【已上线功能清单】\n"
            "以下是本 bot 当前具备的能力，用户问你能干什么时可以介绍：\n"
            + "\n".join(_feat_items) + "\n"
            + "注意：不要无中生有地说自己有不在上面列表里的功能。"
        )
    except Exception:
        _features_block = ""

    parts = [
        "══════════════════════════════════════\n"
        "【角色定位】\n"
        "你是用户的专属信息助手「小助」，只对用户一个人说话。\n"
        "你的回复只给用户看，不是写给任何 AI 或系统看的。\n"
        "系统里有一个「监管 AI」会在你回复后做质量检查，但那是它的事：\n"
        "  · 你不需要理会它、不需要回应它、不需要向它解释任何事\n"
        "  · 但如果用户问起监管AI/核查/审核相关问题，\n"
        "    你应该如实说明：走了快速路径时没有经过核查，走了搜索路径时经过了核查。\n"
        "    不要搧塞用户的合理追问。\n"
        "  · 如果你收到标注了【系统指令】或【审核退回】的消息，那是系统给你的修改要求，\n"
        "    照着改就行，改完之后直接把修改版发给用户，不需要提及审核过程\n"
        "══════════════════════════════════════\n\n"
        "【最高指令·优先级高于一切】\n"
        "不确定的事情，不说。\n"
        "完全没把握的直接说不知道，需要查资料的引导用户发 /ask。\n"
        "宁可说'我不知道'，绝不输出没有把握的内容。\n"
        "══════════════════════════════════════\n\n"
        f"【当前时间】今天是 {bj_str} {bj_week}。训练数据可能滞后，以此处时间为准。\n"
        "  → 用户问'几点了'/'现在几点'/'现在几时'/'当前时间'等，直接说出上面的时间，\n"
        "    不要说'我没有获取系统时间的能力'——那是错的，时间就在上面。\n\n"
        "你叫小助，只有用户主动问身份时才说；平时不主动提及。\n"
        "用中文回答，像朋友聊天，信息密度高，不废话。不涉及政治宗教民族敏感话题。\n\n"
        "【回复效率】\n"
        "· 先给结论，再给细节——不要铺垫、不要重复用户的问题\n"
        "· 回复开头不说'好的''当然''让我来帮你'等无意义起手式\n"
        "· 每个独立的要点单独成段，段间留空行，让眼睛好读\n"
        "· 列举 3 条以上时用短横线「-」或数字分行，不要挤成一坨\n"
        "· 能用一句话说清的不写两句，能用具体数字的不写'一些/很多'\n"
        "· 最后不加'希望对你有帮助''如有疑问欢迎继续问'之类的结尾废话\n\n"
        "【格式】\n"
        "纯文本 + emoji（适度），不用任何 Markdown（不加粗、不斜体、不用#标题、不用---）。\n\n"
        "【回复格式】\n"
        "正文里不写任何来源标签，不写（推测）（估计值）（经验补充）（Wiki）等括号注释。\n"
        "来源信息只在你自己心里记着，用于被追问时如实说明，不出现在正文里。\n\n"
        "【禁止伪造来源】\n"
        "绝对不能在正文里编一个看起来像网站的名字（如'KitchenToolsTips'等）当作来源出处。\n"
        "来源只记在心里，被问时再说，不主动写进回复。\n\n"
        "【配置修改请求】\n"
        "如果用户要求修改 API 上限、配额参数等 bot 配置（如'把 Brave 上限改成 1000'），\n"
        "如实告知：我没有权限在运行时修改自身配置，这类修改需要改代码。\n"
        "可以告诉用户：预警阈值可以用 /quota set <数字> 调整；其他上限需要让开发者改代码。\n\n"
        "【来源诚实】\n"
        "没调工具就答的，说'我印象里'，不能暗示有过搜索行为。\n"
        "被追问'查了没/哪来的'，必须对照下方【近期工具记录】如实说明，不能自行猜测或编造。\n"
        "  · 记录里有搜索工具 → 说'搜过了'\n"
        "  · 记录里工具列表为空 → 说'那条没有搜索，是我自己的理解'\n"
        "  · 记录里显示超时/采集异常 → 如实说'当时搜索了但没采集到有效结果'\n\n"
        "【禁止捏造】\n"
        "举例打比方用'比如/举个例子/就好比'引出，绝不把自己编的内容安在用户头上。\n"
        "引用用户的话只能引用对话记录里真实出现的原文。\n\n",
        _features_block,
        "\n【对话记忆（仅用于保持连续性）】\n"
        + memory_context
        + "\n如果记忆中有相关记录，优先利用记录回答，不要声称自己完全没有记忆；"
        "没有相关记录时，再如实说明只能查看已保存的日志或摘要。",
        "\n【午报说明】\n"
        "每天北京时间 13:00 自动生成并推送一份「午报」给用户。\n"
        "午报固定包含以下板块：\n"
        "  🌤 天气预报（安阳，含早/午/晚/夜四段）\n"
        "  💱 汇率（USD/CNY 及近期走势分析）\n"
        "  📈 行情速览（BTC / ETH 等）\n"
        "  📰 中国要闻 / 全球要闻（当天重要事件）\n"
        "  🤖 AI 速报（当日 AI 领域重要新闻，含点评）\n"
        "  🔒 代理圈动态（sing-box / Xray-core 等工具更新）\n"
        "  🗣️ 圈子在聊（HackerNews 热议话题）\n"
        "  🛠 GitHub 热榜（当日 star 增长最快的仓库）\n"
        "  🎮 Steam 降价优惠（游戏折扣与促销）\n"
        "  ⚡ 今日冷知识（科学/历史趣味小知识）\n"
        "用户问'午报几点发/什么时候发'→ 回答：每天北京时间 13:00。\n"
        "用户问'今天午报有什么/回顾午报/午报里的XX'→ 调用 read_today_report 工具读取全文后回答。\n"
        "如果午报还没生成（当天 13:00 前被问到）→ 告知'今天的午报还没发，每天 13:00 推送'。\n",
    ]

    # 注入近 5 轮工具使用记录，供「查了没」类追问如实回答
    try:
        _tl = json.loads(open(TOOLLOG_FILE, encoding="utf-8").read())
        _recent = _tl[-5:] if len(_tl) >= 5 else _tl
        if _recent:
            _tl_lines = [
                "【近期工具记录】",
                "（仅供被问到「查了没/用了什么工具/哪来的」时如实回答，平时不要主动提及，",
                " 记录里有工具 ≠ 做错了，没工具 ≠ 做错了，如实描述即可）",
            ]
            for _e in _recent:
                _tools = _e.get("model_tools") or []
                _tool_str = "、".join(_tools[:6]) if _tools else "（无工具调用）"
                _rp = _e.get("reply_preview", "")[:40]
                _tl_lines.append(
                    f"  [{_e.get('ts','')}] 问：{_e.get('user','')[:30]}"
                    f"\n    工具：{_tool_str}"
                    f"\n    回复预览：{_rp}"
                )
            parts.append("\n" + "\n".join(_tl_lines))
    except Exception:
        pass

    # ══ 第一层：意图消歧 ══════════════════════════════════════════════
    _ctx_turns = memory_turns
    _focus = load_focus()
    pre = _pre_check(text, ctx=_ctx_turns, focus=_focus)
    needs_search = True   # 默认走搜索路径
    keywords     = []
    query_type   = "其他"
    pre_searched = ""     # 兼容后续 worklog 字段
    route_info   = {
        "route": "search",
        "reason": "默认搜索路径",
        "category": "默认",
        "disambig_needs_search": True,
        "policy_override": False,
    }

    _retry_hint    = False
    _prev_searches = []
    if pre:
        needs_search = pre["needs_search"]
        keywords     = pre["keywords"]
        query_type   = pre["query_type"]
        _retry_hint    = pre.get("retry_hint", False)
        _prev_searches = pre.get("prev_searches", [])
        if not pre["clear"]:
            # ── 熔断检查：反问已 >= 2 次则强制转开放搜索，不再反问 ──
            _cur_count = _focus.get("clarify_count", 0) if _focus else 0
            if _cur_count >= 2:
                log.info(f"🔥 反问熔断（已问 {_cur_count} 次），强制转开放搜索")
                pre["clear"]         = True
                pre["needs_search"]  = True
                pre["user_deferred"] = True
                pre["focus_action"]  = "defer"
                if not pre.get("goal"):
                    pre["goal"] = _focus.get("goal", text) if _focus else text
                # 清空焦点，进入下方正常搜索流程
                clear_focus()
                _focus = {}
            else:
                # 意图不清晰 → 反问用户，并写入焦点状态
                clarify = pre["clarify_question"] or "能说清楚一点吗？"
                import time as _fts
                save_focus({
                    "active": True,
                    "goal": pre.get("goal") or text,
                    "original_query": (_focus or {}).get("original_query", text),
                    "missing_slot": pre.get("missing_slot", ""),
                    "clarify_count": _cur_count + 1,
                    "user_deferred": False,
                    "topic_anchor": pre.get("topic_anchor") or pre.get("keywords", []),
                    "created_ts": (_focus or {}).get("created_ts") or _fts.time(),
                })
                if http_mode:
                    return clarify
                tg("sendMessage", {"chat_id": chat_id, "text": clarify})
                history.append({"role": "user",      "content": text, "ts": _now_ts()})
                history.append({"role": "assistant",  "content": clarify, "ts": _now_ts()})
                save_history(history[-MAX_HISTORY:])
                log.info(f"🤔 意图不清晰，反问（第 {_cur_count+1} 次）：{clarify[:60]}")
                _ctx_summary = summarize_for_context(clarify)
                save_context(text, _ctx_summary)
                return

    try:
        _tool_log_items = load_toollog() or []
        _last_toollog = _tool_log_items[-1] if _tool_log_items else None
    except Exception:
        _last_toollog = None
    route_info = decide_search_policy(text, pre, ctx_turns=_ctx_turns, last_toollog=_last_toollog)
    needs_search = (route_info.get("route") == "search")
    if route_info.get("policy_override"):
        log.info(f"🧭 SearchPolicy 覆盖消歧: route={route_info.get('route')} reason={route_info.get('reason')}")
    else:
        _stool = (pre or {}).get("suggested_tool", "")
        log.info(f"🧭 SearchPolicy: route={route_info.get('route')} reason={route_info.get('reason')}" + (f" | 建议工具={_stool}" if _stool and _stool != 'web_search' else ""))
    if needs_search and not keywords:
        keywords = route_info.get("keywords_hint") or []
        if keywords:
            log.info(f"🧭 SearchPolicy 关键词兜底: {keywords}")

    # 上下文搜索继承：前文用过工具 + 当前明确是在延续/追问同一话题 → 强制走搜索路
    if not needs_search and _ctx_turns and _should_inherit_search_context(text, pre, route_info):
        try:
            _tl = load_toollog()
            if _tl:
                _last = _tl[-1]
                _last_had_tools = bool(_last.get("model_tools")) or bool(_last.get("pre_searched"))
                if _last_had_tools:
                    needs_search = True
                    route_info.update({
                        "route": "search",
                        "reason": "上轮使用过搜索/工具，当前是追问，继承搜索上下文",
                        "category": "上下文追问",
                        "policy_override": True,
                    })
                    if not keywords:
                        _prev_u = _last.get("user", "")
                        keywords = [w for w in re.split(r"[，。？！,.?!\s]+", _prev_u) if len(w) >= 2][:5]
                    log.info(f"🔗 上下文继承：前文有工具，当前追问 → 强制搜索 kw={keywords}")
        except Exception as _ctx_e:
            log.debug(f"上下文继承检查失败: {_ctx_e}")
    # ―――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――――

    history.append({"role": "user", "content": text, "ts": _now_ts()})
    if not http_mode:
        append_daily_log("user", text)
    meta         = {"rounds": [], "tool_calls_summary": [], "tool_results": [],
                    "fetched_pages": [], "source_index": []}
    _wl_verifier = {"rounds": [], "rewrites": 0, "final": "skip"}
    write_reasoning = ""
    fact_list    = ""  # 已废弃
    verify_status = "skip"
    reference_mode = "pure_model"
    evidence_flags = []
    local_evidence_pack = None
    local_evidence_kind = ""

    if not needs_search and should_use_vps_traffic(text, pre=pre, route_info=route_info):
        try:
            local_evidence_pack = build_vps_traffic_pack(text)
            local_evidence_kind = "vps_traffic"
            meta = {
                "rounds": local_evidence_pack.get("rounds", []),
                "tool_calls_summary": local_evidence_pack.get("tool_calls_summary", []),
                "tool_results": local_evidence_pack.get("tool_results", []),
                "fetched_pages": local_evidence_pack.get("fetched_pages", []),
                "source_index": local_evidence_pack.get("source_index", []),
                "facts_json": local_evidence_pack.get("facts_json", {}),
                "reply_mode": local_evidence_pack.get("reply_mode", "report"),
            }
            fact_list = local_evidence_pack.get("fact_list", "")
            reference_mode = local_evidence_pack.get("reference_mode", "evidence_backed")
            evidence_flags = local_evidence_pack.get("evidence_flags", [])
            log.info("🧾 本地证据路径：已调用 vps_traffic，进入证据核查分支")
        except Exception as _vps_e:
            log.warning(f"vps_traffic 证据构建失败: {_vps_e}")
            local_evidence_pack = None
            local_evidence_kind = ""
    elif not needs_search and should_use_today_report(text, pre=pre, route_info=route_info):
        try:
            local_evidence_pack = build_today_report_pack(text, report_text=report)
            local_evidence_kind = "today_report"
            meta = {
                "rounds": local_evidence_pack.get("rounds", []),
                "tool_calls_summary": local_evidence_pack.get("tool_calls_summary", []),
                "tool_results": local_evidence_pack.get("tool_results", []),
                "fetched_pages": local_evidence_pack.get("fetched_pages", []),
                "source_index": local_evidence_pack.get("source_index", []),
                "facts_json": local_evidence_pack.get("facts_json", {}),
            }
            fact_list = local_evidence_pack.get("fact_list", "")
            reference_mode = local_evidence_pack.get("reference_mode", "evidence_backed")
            evidence_flags = local_evidence_pack.get("evidence_flags", [])
            log.info("🧾 本地证据路径：已调用 read_today_report，进入证据核查分支")
        except Exception as _report_e:
            log.warning(f"read_today_report 证据构建失败: {_report_e}")
            local_evidence_pack = None
            local_evidence_kind = ""

    lane_decision = decide_lane(
        needs_search=needs_search,
        route_info=route_info,
        pre=pre,
        local_evidence_kind=local_evidence_kind,
    )
    route_info["lane"] = lane_decision.name
    route_info["lane_reason"] = lane_decision.reason
    if lane_decision.evidence_kind:
        route_info["lane_evidence_kind"] = lane_decision.evidence_kind
    log.info(f"🛣️ Lane: {lane_decision.name} reason={lane_decision.reason}")

    progress = TelegramProgress(chat_id, tg, enabled=not http_mode)
    progress.set_route(lane_decision.name)

    # 系统查询（问 bot 自身功能/配置，不含日历）：功能清单已经在系统提示里，
    # 不需要搜索也不需要走完整 pipeline，直接快速路径回答即可；但它不是"没有
    # 依据的模型自由发挥"，所以标记为 system_info，不加"AI-no references"。
    _is_system_info = (lane_decision.name == "system" and query_type == "系统查询")

    # ── 纯快速路径：不搜索、不调用工具、不进入新 pipeline ───────────────────
    if lane_decision.name == "fast" or _is_system_info:
        is_fast_path = True
        meta = {
            "rounds": [],
            "tool_calls_summary": [],
            "tool_results": [],
            "fetched_pages": [],
            "source_index": [],
            "failed_urls": [],
            "facts_json": {},
        }
        verify_status = "skip"
        reference_mode = "system_info" if _is_system_info else "pure_model"
        evidence_flags = []
        # history 在进入 lane 分支之前已经 append 过当前用户消息一次（约在消歧层之前），
        # 这里用 history[:-1][-8:] 取"当前消息之前"的最近 8 轮，再手动补一次当前消息，
        # 避免当前消息在喂给模型的上下文里出现两次。
        _fast_messages = [
            {"role": m.get("role"), "content": m.get("content", "")}
            for m in history[:-1][-8:]
            if m.get("role") in ("user", "assistant")
        ]
        _fast_messages.append({"role": "user", "content": text})
        progress.stage("writer", "start", "")
        if _is_system_info:
            # 系统查询用完整人设+功能清单提示，不套用"你没有工具"的快速路径话术
            # ——那套话术是给真正的闲聊/知识问答准备的，系统查询答的是 bot 自己。
            _sys_prompt = "\n".join(list(parts or []))
        else:
            _sys_prompt = _build_fast_system_prompt(parts)
        reply = fast_chat(
            _fast_messages,
            system=_sys_prompt,
            max_tokens=900,
            temp=0.5,
        )
        write_reasoning = ""
        if not _is_system_info:
            reply = _mark_no_references(reply)
        display_reply = _render_display_reply(reply, meta=meta, mode="answer")
        progress.stage("writer", "done", str(len(display_reply)))

        # 当前用户消息已经在进入本分支前写入过 history/daily_log 一次，这里只补写
        # assistant 的回复，不再重复写 user 那一条。
        history.append({"role": "assistant", "content": display_reply, "ts": _now_ts()})
        if not http_mode:
            append_daily_log("assistant", display_reply)

        save_worklog_entry({
            "ts":        datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
            "user":      text[:60],
            "main": {
                "rounds": 0,
                "tools": [],
                "source_count": 0,
                "fetch_count": 0,
                "fact_count": 0,
                "reference_mode": reference_mode,
                "evidence_flags": evidence_flags,
                "route": route_info,
                "verify_status": verify_status,
            },
            "verifier": _wl_verifier,
            "route": route_info,
            "reference_mode": reference_mode,
            "evidence_flags": evidence_flags,
            "reply_len": len(display_reply),
        })
        save_toollog_entry(
            text,
            pre_searched="",
            model_tools=[],
            confidence=None,
            reply_preview=display_reply[:200],
            reasoning_preview="",
            search_snippets=[],
            route_info=route_info,
            verify_status=verify_status,
            reference_mode=reference_mode,
            evidence_flags=evidence_flags,
            failed_urls=[],
        )
        try:
            save_detailed_log(
                user_text=text,
                meta=meta,
                write_reply=reply,
                write_reasoning=write_reasoning,
                verifier_result=_wl_verifier,
                source_index=[],
                suggested_length="",
                route_info=route_info,
                pre=pre,
            )
        except Exception as _dl_e:
            log.warning(f"详细日志写入异常: {_dl_e}")

        if http_mode:
            return display_reply

        if len(history) > MAX_HISTORY:
            save_history(history[-MAX_HISTORY:])
        else:
            save_history(history)
        save_context(text, summarize_for_context(display_reply))
        clear_focus()
        for w in list(_cfg._quota_warnings):
            send(chat_id, w)
        _cfg._quota_warnings.clear()
        _send_reply_with_progress(progress, send, chat_id, display_reply)
        sync_to_openhuman_memory(
            user_text=text, reply_text=display_reply, source="telegram",
            tools_used=[],
            sources=[],
        )
        return

    # ── 搜索/证据路径：采集 → 写作 → 核查 ───────────────────────────────

    # ══ 四层路径：采集 → 写作 → 核查 ═════════════════════════════
    is_fast_path = False

    # ── 代码层预查：从今日缓存中读取相关内容注入采集AI ─────────────
    pre_results  = None
    pre_searched = ""
    pre_source_entries = []
    if keywords:
        _today_idx = load_today_index()
        if _today_idx:
            _kw_low = [k.lower() for k in keywords]
            _relevant = [
                e for e in _today_idx
                if not _is_blocked_source(e)
                and cache_match_score(
                    e.get("query","") + " " + e.get("title","") + " " +
                    e.get("snippet_head","") + " " + e.get("session_user",""),
                    _kw_low,
                ) >= 2
            ]
            if _relevant:
                _ids = [e["id"] for e in _relevant[:6]]
                _raw = execute_read_cache(_ids, level="full")
                try:
                    _snips = json.loads(_raw)
                    _idx_by_id = {e.get("id"): e for e in _relevant}
                    _good  = [s for s in _snips
                              if not s.get("error") and (s.get("snippet") or s.get("full_content"))]
                    if _good:
                        _lines = []
                        for s in _good:
                            _idx_e = _idx_by_id.get(s.get("id"), {})
                            _url = s.get("url", "")
                            try:
                                _domain = _url.split("/")[2]
                            except Exception:
                                _domain = _idx_e.get("domain", "")
                            if _domain.lower() in _BLOCKED_SOURCE_DOMAINS:
                                continue
                            _body = s.get("full_content") or s.get("snippet") or ""
                            # 对直接API工具缓存：只保留一条（工具名去重）
                            _entry_tool = _domain or s.get("title", "")
                            _DIRECT_TOOLS = {"check_weather","vps_traffic","github_trending",
                                             "check_api_balance","calendar_query","calendar_add"}
                            if any(t in _entry_tool for t in _DIRECT_TOOLS):
                                if any(t in (e.get("domain","") + e.get("title",""))
                                       for e in pre_source_entries
                                       for t in _DIRECT_TOOLS
                                       if t in _entry_tool):
                                    continue  # 该直接API工具已有缓存，跳过重复
                            pre_source_entries.append({
                                "id": s.get("id"),
                                "tool": "read_today_cache",
                                "query": _idx_e.get("query", "今日缓存"),
                                "title": s.get("title", ""),
                                "url": _url,
                                "domain": _domain,
                                "snippet": (s.get("snippet") or "")[:600],
                                "full_content": s.get("full_content"),
                            })
                            _lines.append(f"  [ID:{s['id']}] {s.get('title','')}")
                            _lines.append(f"  {_body[:1200]}")
                        pre_results  = "\n".join(_lines)
                        pre_searched = "缓存:" + ",".join(
                            e.get("query","") for e in _relevant[:3]
                        )[:40]
                        log.info(f"📂 代码层预查命中 {len(_good)} 条缓存，已注入采集AI")
                        progress.stage(
                            "gather_item",
                            "start",
                            f"📂 发现今日缓存 {len(_good)} 条，已注入采集AI，减少重复搜索",
                        )
                except Exception as _e:
                    log.debug(f"代码层预查解析失败: {_e}")

    # 第二层：采集 AI
    # ── 构建焦点任务 ────────────────────────────────────────────────
    _focus_now = load_focus()
    _focus_task = None
    _user_deferred = pre.get("user_deferred", False) if pre else False
    if _focus_now.get("active") or _user_deferred:
        _focus_task = {
            "goal": (_focus_now.get("goal") or (pre.get("goal") if pre else None) or text),
            "user_deferred": _user_deferred or _focus_now.get("user_deferred", False),
            "topic_anchor": (_focus_now.get("topic_anchor") or (pre.get("topic_anchor") if pre else None) or []),
        }
        log.info(f"🎯 对话焦点注入: goal={_focus_task['goal'][:40]} deferred={_focus_task['user_deferred']}")
    # ────────────────────────────────────────────────────────────────
    source_index_result = []
    try:
        from tg_bot.core.pipeline import run_search_pipeline
        history_context = history[:-1]
        if summary or memory_turns:
            # 不再把 memory_context 伪装成一条 assistant 消息塞进历史——Writer 不会把
            # history_context 当 messages 直接喂给模型，这里用 role="memory" 标记，
            # Writer 只会把它放进"对话背景"说明段，不会被当成真实的对话轮次。
            history_context = [{"role": "memory", "content": memory_context}] + history_context
        _local_sources_for_pipeline = (
            (local_evidence_pack or {}).get("source_index") if lane_decision.name in ("local_tool", "report") else None
        )
        reply, verify_status, meta = _run_search_with_progress(
            progress,
            run_search_pipeline,
            text,
            keywords,
            chat_id=chat_id if not http_mode else None,
            history_context=history_context,
            pre_results=pre_results,
            pre_source_entries=pre_source_entries,
            retry_hint=_retry_hint,
            prev_searches=_prev_searches,
            focus_task=_focus_task,
            suggested_length=(pre or {}).get("suggested_length", ""),
            local_sources=_local_sources_for_pipeline,
        )
        meta = meta or {}
        source_index_result = meta.get("source_index", []) or []
        write_reasoning = meta.get("write_reasoning", "")
        _wl_verifier["final"] = verify_status
        _wl_verifier["rounds"].append({
            "attempt": 0,
            "verdict": verify_status,
            "summary": "core.pipeline",
        })
        log.info(f"🧱 新搜索 pipeline 已接入: verify={verify_status}, sources={len(source_index_result)}")
    except Exception as _pipe_e:
        log.warning(f"⚠️ 新搜索 pipeline 失败，回退旧流程: {_pipe_e}", exc_info=True)
        progress.stage("gather_item", "start", "⚠️ 主搜索流程异常，切换备用方案…")
        source_index_result, meta = gather_ai(
            text,
            keywords,
            None if progress.enabled else (chat_id if not http_mode else None),
            pre_results=pre_results,
            retry_hint=_retry_hint,
            prev_searches=_prev_searches,
            pre_source_entries=pre_source_entries,
            focus_task=_focus_task,
            status_cb=progress.stage if progress.enabled else None,
        )
        progress.stage("writer", "start", "")
        if not meta.get("tool_calls_summary") and not meta.get("source_index"):
            log.warning("⚠️ 搜索路径但采集AI未调用任何工具，回复基于纯模型知识")
            reply, write_reasoning = write_ai(
                text, "", history_context=history_context, facts_json=None,
                source_index=source_index_result,
                suggested_length=meta.get("suggested_length", ""),
                tool_results=meta.get("tool_results", [])
            )
            reply = "ℹ️ 以下回复基于AI知识生成，未经搜索验证。如需确认可发送「再查一下」。\n\n" + reply
            _wl_verifier["final"] = "skip_no_tools_warned"
            verify_status = "skip_no_tools_warned"
        else:
            reply, write_reasoning = write_ai(
                text, "", history_context=history_context, facts_json=None,
                source_index=source_index_result,
                suggested_length=meta.get("suggested_length", ""),
                tool_results=meta.get("tool_results", [])
            )
            _verdict, _flagged, _checks = verify_reply(
                reply, "", user_text=text, attempt=0,
                source_index=source_index_result,
                facts_json=None)
            verify_status = _verdict
            _wl_verifier["final"] = _verdict
            _wl_verifier["rounds"].append({
                "attempt": 0,
                "verdict": _verdict,
                "summary": _flagged[:200],
            })
            if _verdict != "pass":
                log.warning(f"⚠️ 旧流程 fallback 审核未通过: {_flagged[:200]}")
        progress.stage("writer", "done", str(len(reply or "")))
        if verify_status != "skip_no_tools_warned":
            progress.stage("critic", "done", verify_status)

    if not pre_searched:
        pre_searched = " ".join(keywords)[:40]  # 无缓存命中时回退到关键词记录

    meta = meta or {}
    meta.setdefault("rounds", [])
    meta.setdefault("tool_calls_summary", [])
    meta.setdefault("tool_results", [])
    meta.setdefault("fetched_pages", [])
    meta.setdefault("source_index", source_index_result)
    meta.setdefault("failed_urls", [])
    meta.setdefault("facts_json", {})

    evidence_flags = []
    if source_index_result or meta.get("source_index"):
        evidence_flags.append("source_index")
    if meta.get("fetched_pages"):
        evidence_flags.append("fetched_pages")
    _tool_names = [
        str(t).split("(", 1)[0]
        for t in (meta.get("tool_calls_summary") or [])
    ]
    if any(t in ("web_search", "serper_search", "wikipedia_lookup", "fetch_content")
           for t in _tool_names):
        evidence_flags.append("search_tools")
    if "read_today_report" in _tool_names:
        evidence_flags.append("read_today_report")
    if "vps_traffic" in _tool_names:
        evidence_flags.append("vps_traffic")
    if _tool_names and not any(t in (
            "web_search", "serper_search", "wikipedia_lookup", "fetch_content",
            "read_today_report", "vps_traffic",
    ) for t in _tool_names):
        evidence_flags.append("direct_tools")
    if local_evidence_pack:
        evidence_flags = list(dict.fromkeys((local_evidence_pack.get("evidence_flags") or []) + evidence_flags))
    reference_mode = "evidence_backed" if evidence_flags else "pure_model"

    if not (reply or "").strip():
        log.warning("⚠️ 最终回复为空，使用兜底提示")
        reply = "这轮搜索和核查没有生成可发送的有效回复。请换个问法或发送“再查一次”，我会重新采集材料。"

    if reference_mode == "pure_model":
        reply = _mark_no_references(reply)
    raw_reply = reply
    display_mode = (
        "report" if local_evidence_kind == "today_report" or meta.get("reply_mode") == "report"
        else "search" if evidence_flags else "answer"
    )
    display_reply = _render_display_reply(raw_reply, meta=meta, mode=display_mode)
    if meta.get("verification"):
        display_reply = append_verification_note(display_reply, meta.get("verification"))
    route_info["verification"] = meta.get("verification")
    route_info["patch_modes"] = meta.get("patch_modes", [])
    route_info["critic_issues"] = meta.get("critic_issues", [])

    history.append({"role": "assistant", "content": display_reply, "ts": _now_ts()})
    if not http_mode:
        append_daily_log("assistant", display_reply)

    # ⑤ 工作日志：记录主 bot + 审核员本轮工作详情
    _wl_main = {
        "rounds":       len(meta.get("rounds", [])),
        "tools":        meta.get("tool_calls_summary", []),
        "source_count": len(source_index_result),
        "fetch_count":  len(meta.get("fetched_pages", [])),
        "fact_count":   (meta.get("facts_json") or {}).get("fact_count", 0),
        "reference_mode": reference_mode,
        "evidence_flags": evidence_flags,
        "route":        route_info,
        "verify_status": verify_status,
    }
    save_worklog_entry({
        "ts":        datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "user":      text[:60],
        "main":      _wl_main,
        "verifier":  _wl_verifier,
        "route":     route_info,
        "reference_mode": reference_mode,
        "evidence_flags": evidence_flags,
        "reply_len": len(display_reply),
    })

    # 保存思考日志（③）
    if is_fast_path:
        # 快速路径：只存一次，标注为写作AI（快速路径），避免重复
        if write_reasoning:
            save_write_thinking(text, "（快速路径，单次调用）\n" + write_reasoning)
    else:
        # 四层路径：采集AI各轮 + 写作AI分别存档
        save_thinking_entry(text, meta["rounds"])
        if write_reasoning:
            save_write_thinking(text, write_reasoning)
    # 提取本轮思考摘要（取第一轮有 reasoning 的内容）
    _reasoning_preview = ""
    for _r in meta.get("rounds", []):
        if _r.get("reasoning"):
            _reasoning_preview = _r["reasoning"][:300]
            break
    if not _reasoning_preview and write_reasoning:
        _reasoning_preview = write_reasoning[:300]
    # 保存本轮工具使用记录（②），同时附上思考和回复摘要
    save_toollog_entry(
        text,
        pre_searched=pre_searched,
        model_tools=meta["tool_calls_summary"],
        confidence=None,   # 新架构不再用置信度分，由消歧层的 needs_search 替代
        reply_preview=raw_reply[:200],
        reasoning_preview=_reasoning_preview,
        search_snippets=meta.get("tool_results", []),
        route_info=route_info,
        verify_status=verify_status,
        reference_mode=reference_mode,
        evidence_flags=evidence_flags,
        failed_urls=meta.get("failed_urls", []),
    )
    # 来源存档：URL + 域名 + AI 参考文字 → SOURCES_DIR/<timestamp>.json
    _src_path = save_sources_file(
        user_text     = text,
        source_index  = source_index_result,
        tool_results  = meta.get("tool_results", []),
        fetched_pages = meta.get("fetched_pages", []),
        facts_json    = meta.get("facts_json"),
        reference_mode=reference_mode,
        evidence_flags=evidence_flags,
        reply         = raw_reply,
    )

    # ── 保存详细过程日志（Telegram + HTTP 两条路径都记录）────────────
    try:
        save_detailed_log(
            user_text=text,
            meta=meta,
            write_reply=raw_reply,
            write_reasoning=write_reasoning,
            verifier_result=_wl_verifier,
            source_index=source_index_result,
            suggested_length=meta.get("suggested_length", ""),
            route_info=route_info,
            pre=pre,
        )
    except Exception as _dl_e:
        log.warning(f"详细日志写入异常: {_dl_e}")
    # ─────────────────────────────────────────────────────────────────
    # http_mode：跳过 history 写入和 Telegram 发送，直接返回 reply
    if http_mode:
        return display_reply
    # 超出限制时总结旧消息
    if len(history) > MAX_HISTORY:
        cutoff    = len(history) - MAX_HISTORY
        old_msgs  = history[:cutoff]
        new_piece = summarize(old_msgs)
        existing  = load_summary()
        combined  = (existing + "\n" + new_piece).strip() if existing else new_piece
        if len(combined) > 600:
            combined = fast_chat(
                [{"role": "user", "content": f"将以下摘要压缩到150字以内：\n{combined}"}],
                system="你是总结助手。", max_tokens=200, temp=0.3
            )
        save_summary(combined)
        history = history[cutoff:]

    save_history(history)
    # 保存本轮对话摘要到上下文（供下轮消歧使用）
    _ctx_summary = summarize_for_context(display_reply)
    save_context(text, _ctx_summary)
    clear_focus()  # 任务执行完毕，清空焦点
    # 发送 API 配额预警（如有）
    for w in list(_cfg._quota_warnings):
        send(chat_id, w)
    _cfg._quota_warnings.clear()
    _send_reply_with_progress(progress, send, chat_id, display_reply)

    # 同步到 OpenHuman 记忆（来自 Telegram 渠道）
    sync_to_openhuman_memory(
        user_text=text, reply_text=display_reply, source="telegram",
        tools_used=meta.get("tool_calls_summary", []),
        sources=source_index_result,
    )


# ── offset 文件读写 ────────────────────────────────────────────────────
def _load_offset():
    try:
        return int(open(OFFSET_FILE).read().strip())
    except:
        return 0

def _save_offset(off):
    try:
        atomic_write_text(OFFSET_FILE, str(off))
    except Exception:
        pass


def main():
    log.info("Bot v2 启动（含搜索能力），开始监听...")
    diagnostics = validate_config()
    if not diagnostics["ok"]:
        raise RuntimeError("配置检查失败：" + "；".join(diagnostics["errors"]))
    ensure_data_dir()
    for warning in diagnostics["warnings"]:
        log.warning("配置提示：%s", warning)
    auto_cleanup()   # 启动时清理一次过期文件

    # 启动 HTTP /ask 接口（后台线程，供 OpenHuman 等外部系统调用）
    _ask_server_mod.ASK_SERVER_READY.clear()
    _ask_server_mod.ASK_SERVER_ERROR = None
    _ask_server_mod.ASK_API_TOKEN = _load_or_create_ask_token()
    threading.Thread(target=_run_ask_server, daemon=True, name="ask-http").start()
    if not _ask_server_mod.ASK_SERVER_READY.wait(timeout=5):
        raise RuntimeError("HTTP API 启动超时")
    if _ask_server_mod.ASK_SERVER_ERROR:
        raise RuntimeError(f"HTTP API 启动失败：{_ask_server_mod.ASK_SERVER_ERROR}")
    if _cfg.ASK_API_TOKEN:
        log.info("🔑 /ask 使用 ASK_API_TOKEN 环境变量")
    else:
        log.info("🔑 /ask token 已保存到 %s", ASK_TOKEN_FILE)

    _cleanup_counter = 0
    offset = _load_offset()
    while True:
        try:
            from tg_bot.tools.fetch import http_get
            raw = http_get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
                f"?offset={offset}&timeout=30&allowed_updates=message",
                timeout=35   # 比 Telegram hold 时间多5秒，避免 read timeout 警告
            )
            resp = json.loads(raw) if raw else None
            if not resp or not resp.get("ok"):
                time.sleep(5)
                continue

            for update in resp.get("result", []):
                offset  = update["update_id"] + 1
                _save_offset(offset)
                msg     = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text    = (msg.get("text") or "").strip()

                if not text or not chat_id:
                    continue
                if int(chat_id) != ALLOWED_CHAT:
                    log.info(f"拒绝陌生 chat: {chat_id}")
                    continue

                log.info(f"收到: {text[:60]}")
                _cleanup_counter += 1
                if _cleanup_counter % 100 == 0:
                    auto_cleanup()

                if text == "/start":
                    handle_start(chat_id)
                elif text == "/help":
                    handle_help(chat_id)
                elif text == "/clear":
                    handle_clear(chat_id)
                elif text == "/recap":
                    handle_recap(chat_id)
                elif text == "/thinking":
                    handle_thinking(chat_id)
                elif text == "/tools":
                    handle_tools(chat_id)
                elif text == "/sources":
                    handle_sources(chat_id)
                elif text.startswith("/source "):
                    handle_source_detail(chat_id, text)
                elif text == "/worklog" or text.startswith("/worklog "):
                    handle_worklog(chat_id, text)
                elif text == "/diary" or text.startswith("/diary "):
                    handle_diary(chat_id, text)
                elif text == "/balance":
                    handle_balance(chat_id)
                elif text == "/quota":
                    handle_quota(chat_id)
                elif text.startswith("/quota set "):
                    handle_quota_set(chat_id, text)
                elif text.startswith("/ask "):
                    handle_ask(chat_id, text, handle)
                else:
                    handle(chat_id, text)

        except KeyboardInterrupt:
            log.info("停止。")
            break
        except Exception as e:
            log.error(f"主循环出错: {e}")
            time.sleep(10)
