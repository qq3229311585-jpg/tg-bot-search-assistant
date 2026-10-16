#!/usr/bin/env python3
"""tools/definitions.py — TOOLS 列表定义"""

TOOLS = [
{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "【第一阶段·搜索】用 Tavily 搜索新闻/信息，返回标题、摘要和URL。"
            "每次对话最多调用 6 次。搜到有价值的URL后，再用 fetch_content 抓正文。"
            "新闻用 'news'，其他查询用 'general'。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，英文为主"},
                "search_type": {
                    "type": "string",
                    "enum": ["news", "general"],
                    "description": "'news' 搜新闻，'general' 搜一般信息"
                }
            },
            "required": ["query", "search_type"]
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "fetch_content",
        "description": (
            "【第二阶段·抓正文】给定URL，抓取页面完整正文，细节比摘要丰富得多。"
            "在 web_search 找到权威URL后使用。每次对话最多调用 2 次，找不到也没关系。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取正文的完整URL"}
            },
            "required": ["url"]
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "serper_search",
        "description": (
            "【交叉验证·Serper】用 Google（Serper）搜索，与 web_search 结果交叉核实。"
            "每次对话只能调用 1 次，用于核查关键事实或补充 web_search 遗漏的内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "search_type": {
                    "type": "string",
                    "enum": ["news", "general"],
                    "description": "'news' 搜新闻，'general' 搜一般信息"
                }
            },
            "required": ["query", "search_type"]
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "wikipedia_lookup",
        "description": (
            "【首选工具】用户问'什么是X/X是什么/解释X/X的原理/X的历史'时，必须优先调用本工具。"
            "同时返回中文Wikipedia和英文Wikipedia双语结果。"
            "中文版含人名/地名/机构名的正确汉字字形，写中文内容时以中文Wikipedia为准。"
            "无次数限制，免费，查概念/定义/术语/人物/历史事件时永远比 web_search 更合适。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查询的主题，英文"}
            },
            "required": ["query"]
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "vps_traffic",
        "description": (
            "查询 VPS 服务器的网络流量、带宽使用情况、连接数、系统负载和内存。"
            "用户问'流量''带宽''服务器状态''网络用了多少''内存''负载'等时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "check_weather",
        "description": (
            "查询指定城市当前天气及未来2天预报（分时段温度、天气、降雨概率）。"
            "不传 city 时默认安阳。用户问任何城市的'天气''冷不冷''要下雨吗''带伞吗'时都调用本工具，"
            "不要用网页搜索查天气。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名（中文或英文，如'武汉''东京'）；用户没提城市就省略，默认安阳"
                }
            },
            "required": []
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "github_trending",
        "description": (
            "获取 GitHub 今日热榜仓库列表，含项目名、描述、语言、星标数、今日新增星标。"
            "做技术日报/早报/热榜汇总时必须调用本工具，不要用 web_search 或 fetch_content 抓 github.com/trending（那会失败）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "description": "筛选语言，留空表示所有语言，例如 'python' 'javascript'"
                }
            },
            "required": []
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "exchange_rate",
        "description": (
            "查询任意两种货币的实时汇率并可换算金额（免费官方数据）。"
            "用户问'汇率''XX多少人民币''美元换日元'等时必须调用本工具，不要网页搜索。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": "源货币 ISO 代码，如 USD/JPY/EUR/GBP，默认 USD"},
                "target": {"type": "string", "description": "目标货币 ISO 代码，默认 CNY"},
                "amount": {"type": "number", "description": "要换算的金额，不需要换算时省略"}
            },
            "required": []
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "stock_quote",
        "description": (
            "查询股票/指数实时报价（价格、涨跌幅、数据时间）。用户问'XX股价''纳指怎么样'等时必须调用本工具，不要网页搜索。"
            "query 请传英文公司名或股票代码（如 Apple、AAPL、TSLA、^IXIC；A股加后缀如 600519.SS）；"
            "留空返回纳斯达克+标普500快照。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "英文公司名或股票代码；留空=美股大盘快照"}
            },
            "required": []
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "world_time",
        "description": (
            "查询世界任意城市的当前时间、星期、时区和与北京的时差。用户问'XX现在几点''时差'时调用。"
            "外国城市请传英文名（如 Tokyo、New York、London），中国城市可直接中文；留空返回北京时间。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "城市名，外国城市用英文；留空=北京时间"}
            },
            "required": []
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "steam_specials",
        "description": (
            "获取 Steam 中国区当前特价游戏（官方API：游戏名、人民币价格、折扣力度、商店链接）。"
            "用户问'steam打折''游戏特价''有什么便宜游戏'时必须调用本工具，不要网页搜索。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
},
{
    "type": "function",
    "function": {
        "name": "hn_front_page",
        "description": (
            "获取 Hacker News 当前首页热帖（官方API，技术圈正在热议的话题）。"
            "用户问'圈子在聊什么''HN热帖''技术圈动态'时必须调用本工具，不要网页搜索。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
},
{
    "type": "function",
    "function": {
        "name": "proxy_releases",
        "description": (
            "查询 sing-box / Xray-core / mihomo 三个代理内核的最新版本和更新内容（GitHub 官方 releases）。"
            "用户问这几个工具的'最新版本''更新了什么'时必须调用本工具，不要网页搜索。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "number", "description": "只看最近 N 天内的发布，默认 30"}
            },
            "required": []
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "chinese_holiday",
        "description": (
            "查询中国法定节假日和调休安排。用户问'放假''调休''补班''XX节放几天'时必须调用，不要网页搜索。"
            "不传参数返回今天安排+下一个法定假期。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "要查的日期 YYYY-MM-DD；留空=今天+下个假期"}
            },
            "required": []
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "github_release",
        "description": (
            "查任意 GitHub 开源项目的最新版本号和更新内容（官方 releases API）。"
            "用户问某个开源软件'最新版本''更新了什么'时必须调用，不要网页搜索。repo 传 owner/name 格式。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "仓库名 owner/name，如 python/cpython、ffmpeg 就传 FFmpeg/FFmpeg"}
            },
            "required": ["repo"]
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "epic_free_games",
        "description": "查 Epic 商店本周免费领取的游戏和即将免费的预告（官方接口）。用户问'epic送什么''免费游戏''喜加一'时调用。",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
},
{
    "type": "function",
    "function": {
        "name": "cn_hot_topics",
        "description": (
            "查中文互联网热榜：微博热搜、知乎热榜、B站热门。用户问'微博热搜''知乎在讨论什么''国内热点''今天有什么瓜'时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "weibo/zhihu/bilibili/all，默认 all"}
            },
            "required": []
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "earthquake_recent",
        "description": "查近期全球显著地震（USGS 官方数据，北京时间）。用户问'最近地震''XX地震了吗'时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "min_magnitude": {"type": "number", "description": "最小震级，默认 5.0"},
                "limit": {"type": "number", "description": "返回条数，默认 5"}
            },
            "required": []
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "x_trends",
        "description": "查 X(推特) 当前全球热门话题榜（第三方数据源，偶尔不可用）。用户问'推特热搜''X上在聊什么'时调用。",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
},
{
    "type": "function",
    "function": {
        "name": "check_api_balance",
        "description": (
            "查询所有外部 API 的剩余额度和余额。"
            "用户问'API 余额''还剩多少额度''key 还能用多久'等时调用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
},
{
    "type": "function",
    "function": {
        "name": "read_daily_log",
        "description": "读取某天的原始对话流水账，可看完整对话内容。通常先用 search_daily_summaries 确定日期，再用本工具读详情。date_str 格式 YYYY-MM-DD。",
        "parameters": {"type": "object",
                        "properties": {"date_str": {"type": "string"}},
                        "required": ["date_str"]}
    }
},
{
    "type": "function",
    "function": {
        "name": "search_daily_summaries",
        "description": "在所有历史日总结中搜索关键词，返回命中的日期和片段。用于回答'以前聊过XX吗''上次问YY是哪天'等历史查询。",
        "parameters": {"type": "object",
                        "properties": {"keyword": {"type": "string"}},
                        "required": ["keyword"]}
    }
},
{
    "type": "function",
    "function": {
        "name": "read_daily_summary",
        "description": "读取某天的完整日总结。date_str 格式 YYYY-MM-DD。",
        "parameters": {"type": "object",
                        "properties": {"date_str": {"type": "string"}},
                        "required": ["date_str"]}
    }
},
{
    "type": "function",
    "function": {
        "name": "read_today_cache",
        "description": (
            "【优先使用·零配额】读取今日已采集过的搜索结果，避免重复搜索浪费配额。\n"
            "启动时系统会注入今日索引（标题列表+ID）。发现相关条目后：\n"
            "  1. 调用本工具 level=snippet 读取简介；满足需求则直接使用。\n"
            "  2. 需要更多细节时用 level=full 读完整正文（如有抓取过）。\n"
            "  3. 仍不足时再调用搜索工具。\n"
            "注意：天气/汇率/股价等实时数据不适用缓存，必须重新获取。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要读取的结果 ID 列表，来自今日索引（如 '191829_R001'）"
                },
                "level": {
                    "type": "string",
                    "enum": ["snippet", "full"],
                    "description": "snippet=返回简介+URL，full=返回完整正文（如有抓取）"
                }
            },
            "required": ["ids"]
        }
    }
}
,
{
    "type": "function",
    "function": {
        "name": "read_today_report",
        "description": (
            "读取今天已发送的午报全文。"
            "用于：用户问午报内容/某板块/某条新闻、想回顾今日午报、"
            "或需要确认某个话题是否出现在今天午报里。"
            "无需参数，直接调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}
,
{
    "type": "function",
    "function": {
        "name": "search_chat_history",
        "description": (
            "在完整对话历史（chat_history.json）里搜索包含关键词的消息，返回角色+精确时间戳+内容摘要。"
            "用于：用户问某句话/某条消息是什么时候发的、"
            "某个话题第一次出现是什么时间、确认某条消息的确切发送时间。"
            "比 read_daily_log 更精确，支持今天实时内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "要搜索的关键词或消息片段"
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回条数，默认20",
                    "default": 20
                }
            },
            "required": ["keyword"]
        }
    }
}

,
{
    "type": "function",
    "function": {
        "name": "calendar_query",
        "description": (
            "查询 iCloud 日历中未来 N 天的事件/日程。"
            "用户问'我有什么安排''日程''日历''这周有啥事''明天有会吗''近期计划'等时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "查询未来几天，默认7，最多30",
                    "default": 7
                },
                "calendar_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "指定日历名称列表，如['工作','个人']，留空查所有默认日历"
                }
            },
            "required": []
        }
    }
},
{
    "type": "function",
    "function": {
        "name": "calendar_add",
        "description": (
            "向 iCloud 日历添加一个新事件。"
            "用户说'帮我加个日程''记到日历''提醒我去XX''定个会议'等时调用。"
            "可选日历：工作、行程、个人。start/end 格式：'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "事件标题"},
                "start":   {"type": "string", "description": "开始时间，如 2026-06-05 或 2026-06-05 14:00"},
                "end":     {"type": "string", "description": "结束时间，留空则自动+1小时（具体时间）或+1天（全天）"},
                "calendar_name": {"type": "string", "description": "目标日历名，默认'个人'", "default": "个人"},
                "location":    {"type": "string", "description": "地点（可选）"},
                "description": {"type": "string", "description": "备注内容（可选）"}
            },
            "required": ["summary", "start"]
        }
    }
}
]