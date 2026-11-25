#!/usr/bin/env python3
"""features/fun.py — 好玩的小功能

/quiz        用今天的午报出 3 道选择题，答案藏在剧透遮罩里，点一下才显示
/debate 话题  正方、反方各一段，再来一句裁判点评
/eli5 问题    用讲给五岁小孩听的方式解释
/fun         随机冷知识（本地精选库，不花钱，不重复到全部看完为止）
/poem        把今天的聊天写成一首打油诗

除 /fun 外每个都只调一次关闭思考的模型，单次成本很低。
"""
from __future__ import annotations

import html
import json
import random
import re

from tg_bot.features.llm import cheap_chat
from tg_bot.features.store import load_json, now_bj, save_json

# 只收录教科书级、没有争议的事实
FUN_FACTS = [
    "章鱼有三颗心脏：两颗给鳃供血，一颗给全身供血。",
    "蜂蜜几乎不会变质，因为含水量低、偏酸，细菌很难在里面繁殖。",
    "香蕉是浆果，而草莓在植物学上不算浆果。",
    "金星自转一圈比绕太阳公转一圈还慢，所以金星上的一天比一年还长。",
    "人体最大的器官是皮肤。",
    "光从太阳传到地球大约需要 8 分 20 秒。",
    "袋熊的粪便是方块形的。",
    "火烈鸟的粉红色来自它们吃的藻类和甲壳类里的类胡萝卜素。",
    "埃菲尔铁塔在夏天会因为热胀冷缩长高十几厘米。",
    "一个人的指纹在胎儿时期就已经形成，终生基本不变。",
    "海獭睡觉时会手拉手，防止被水流冲散。",
    "鲨鱼在地球上出现的时间比树木还早。",
    "水在 4℃ 左右密度最大，所以冬天湖底的水不容易结冰，鱼能过冬。",
    "人的骨骼中，最小的骨头是耳朵里的镫骨。",
    "蜗牛的牙齿数量可以超过一万颗，长在一条叫齿舌的带子上。",
    "长颈鹿和人一样，脖子里只有 7 块颈椎。",
    "北极熊的皮肤是黑色的，毛是透明中空的，看起来才是白色。",
    "珠穆朗玛峰的峰顶曾经是海底，那里的岩石里能找到海洋生物化石。",
    "闪电的温度可以达到太阳表面温度的好几倍。",
    "猫咪尝不出甜味，因为它们缺少感受甜味的功能基因。",
    "一茶匙中子星物质的质量可以达到数十亿吨。",
    "木星的大红斑是一个比地球还大的风暴，已经被观测了三百多年。",
    "蝴蝶用脚上的感受器来“尝”味道。",
    "地球上的氧气有很大一部分来自海洋里的浮游植物，而不只是森林。",
    "电鳗放电的电压可以达到 600 伏左右。",
    "人一生中大约有三分之一的时间在睡觉。",
    "月亮每年在以大约 3.8 厘米的速度远离地球。",
    "斑马的条纹每一只都不一样，像人的指纹。",
    "牛有好几个胃室，最大的叫瘤胃。",
    "钻石和石墨都是由碳组成的，只是原子排列方式不同。",
    "考拉每天要睡将近 20 个小时，因为桉树叶营养低、难消化。",
    "声音在水中传播的速度大约是空气中的 4 倍多。",
    "人眼对绿色最敏感，所以夜视仪多用绿色画面。",
    "早期电子计算机 ENIAC 重约 27 吨，占满一整个大房间。",
    "壁虎能在天花板上行走，靠的是脚掌上数百万根细毛产生的范德华力。",
    "海马是由雄性怀孕并产下小海马的。",
    "南极洲是世界上最干燥的大陆，内陆的年降水量比很多沙漠还少。",
]

_QUIZ_SYS = (
    "你是出题人。根据给出的日报内容，出 3 道单选题，每题 4 个选项，只考日报里明确写了的事实。"
    "输出 JSON：{\"questions\":[{\"q\":\"题干\",\"options\":[\"A. …\",\"B. …\",\"C. …\",\"D. …\"],"
    "\"answer\":\"A\",\"why\":\"不超过 30 字的解析\"}]}"
)
_DEBATE_SYS = (
    "你是辩论主持人。针对用户给的话题，用中文输出三段：\n"
    "🟥 正方：最有力的 3 个论点（每条一句）\n"
    "🟦 反方：最有力的 3 个论点（每条一句）\n"
    "⚖️ 裁判：一句话点评哪边论证更扎实、真正的分歧点在哪\n"
    "立场要公允，不编造具体数据，不用 Markdown。涉及政治、宗教、民族话题时礼貌拒绝。"
)
_ELI5_SYS = (
    "请把用户的问题讲给一个五岁小朋友听：用一个生活里的比喻开头，最多 5 句话，"
    "不用专业术语，最后用一句「其实大人这么说：……」给出准确的正式说法。不确定的事情要说不确定。"
)
_POEM_SYS = (
    "你是打油诗人。根据用户今天和助手聊过的话题，写一首 4~8 行的中文打油诗，押韵、轻松幽默，"
    "只调侃话题本身，不嘲讽用户，最后一行单独写「—— 今日话题：」加 2~3 个关键词。不用 Markdown。"
)


def pick_fact(*, rng=None) -> str:
    rng = rng or random.Random()
    state = load_json("fun_facts_seen.json", {})
    seen = set(state.get("seen") or [])
    pool = [i for i in range(len(FUN_FACTS)) if i not in seen]
    if not pool:
        seen, pool = set(), list(range(len(FUN_FACTS)))
    idx = rng.choice(pool)
    seen.add(idx)
    save_json("fun_facts_seen.json", {"seen": sorted(seen)})
    left = len(FUN_FACTS) - len(seen)
    return f"⚡ 冷知识\n\n{FUN_FACTS[idx]}\n\n（库里还有 {left} 条没看过）"


def build_quiz(report_text: str, *, chat=None) -> str:
    report_text = (report_text or "").strip()
    if len(report_text) < 100:
        return "今天的午报还没生成，13:00 之后再来答题吧。"
    raw = (chat or cheap_chat)(report_text[:5000], _QUIZ_SYS, max_tokens=900, temperature=0.5,
                               json_mode=True, stage="feature:quiz")
    try:
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        questions = data["questions"][:3]
    except Exception:
        return "出题失败了，稍后再试试 /quiz。"
    lines = ["🧠 午报小测验（点灰色块看答案）"]
    for i, q in enumerate(questions, 1):
        lines.append(f"\n{i}. {html.escape(str(q.get('q', '')))}")
        for opt in (q.get("options") or [])[:4]:
            lines.append(f"   {html.escape(str(opt))}")
        answer = html.escape(f"{q.get('answer', '?')} · {q.get('why', '')}")
        lines.append(f"   答案：<tg-spoiler>{answer}</tg-spoiler>")
    return "\n".join(lines)


def build_poem(*, chat=None, now=None) -> str:
    from tg_bot.features.weekly import _load_day
    now = now or now_bj()
    users = [r.get("content", "") for r in _load_day(now.strftime("%Y-%m-%d"))
             if r.get("role") == "user" and not str(r.get("content", "")).startswith("/")]
    if not users:
        return "今天还没聊过天呢，先聊几句，晚点再来要诗 ✍️"
    sample = "\n".join(u[:60] for u in users[-30:])
    out = (chat or cheap_chat)(f"今天聊过的消息：\n{sample}", _POEM_SYS, max_tokens=300, temperature=0.9,
                               stage="feature:poem")
    return ("📜 今日打油诗\n\n" + out) if out else "诗人卡壳了，稍后再试 /poem。"


def handle_fun(chat_id, text: str, send) -> None:
    cmd, _, arg = text.partition(" ")
    arg = arg.strip()
    if cmd == "/fun":
        send(chat_id, pick_fact())
    elif cmd == "/quiz":
        from tg_bot.storage import load_report
        send(chat_id, build_quiz(load_report()))
    elif cmd == "/poem":
        send(chat_id, build_poem())
    elif cmd == "/debate":
        if not arg:
            send(chat_id, "用法：/debate 话题，比如 /debate 远程办公比坐班效率高")
            return
        out = cheap_chat(arg[:200], _DEBATE_SYS, max_tokens=700, temperature=0.7, stage="feature:debate")
        send(chat_id, ("🎙 辩论：" + arg + "\n\n" + out) if out else "辩手还在路上，稍后再试。")
    elif cmd == "/eli5":
        if not arg:
            send(chat_id, "用法：/eli5 问题，比如 /eli5 为什么天是蓝的")
            return
        out = cheap_chat(arg[:300], _ELI5_SYS, max_tokens=500, temperature=0.6, stage="feature:eli5")
        send(chat_id, ("🧸 " + out + "\n\n（AI-no references）") if out else "暂时讲不出来，稍后再试。")
