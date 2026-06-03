#!/usr/bin/env python3
"""tools/calendar_tool.py — CalDAV 日历读写工具"""

import logging
import os
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_ICAL_USER = os.getenv("ICAL_USER", "")
_ICAL_PASS = os.getenv("ICAL_PASS", "")
_ICAL_URL  = os.getenv("ICAL_URL", "https://caldav.icloud.com")
_TZ        = ZoneInfo("Asia/Shanghai")

_DEFAULT_CALS = {"工作", "行程", "个人", "提醒 ⚠️"}


def _get_client():
    if not _ICAL_USER or not _ICAL_PASS:
        raise RuntimeError("缺少 ICAL_USER / ICAL_PASS 环境变量")
    import caldav
    return caldav.DAVClient(url=_ICAL_URL, username=_ICAL_USER, password=_ICAL_PASS)


def _to_local(dt):
    if dt is None:
        return None
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return datetime(dt.year, dt.month, dt.day, tzinfo=_TZ)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_TZ)
        return dt.astimezone(_TZ)
    return dt


def _parse_event(ev_obj):
    """用 icalendar 库直接解析事件，稳定可靠。"""
    from icalendar import Calendar as iCal
    raw = ev_obj.data if hasattr(ev_obj, "data") else str(ev_obj)
    cal = iCal.from_ical(raw)
    for comp in cal.walk():
        if comp.name != "VEVENT":
            continue
        summary  = str(comp.get("SUMMARY",  "无标题")).strip()
        location = str(comp.get("LOCATION", "")).strip()
        desc     = str(comp.get("DESCRIPTION", "")).strip()

        dtstart = comp.get("DTSTART")
        dtstart_val = dtstart.dt if dtstart else None
        all_day = isinstance(dtstart_val, date) and not isinstance(dtstart_val, datetime)
        start = _to_local(dtstart_val)

        dtend = comp.get("DTEND")
        if dtend:
            end = _to_local(dtend.dt)
        else:
            dur = comp.get("DURATION")
            end = (start + dur.dt) if (dur and start) else start

        return {
            "summary":  summary,
            "start":    start,
            "end":      end,
            "all_day":  all_day,
            "location": location,
            "desc":     desc[:120],
        }
    return None


def execute_calendar_query(days: int = 7, calendar_names: list = None) -> str:
    """查询未来 N 天的日历事件，返回格式化字符串。"""
    try:
        client    = _get_client()
        principal = client.principal()
        all_cals  = principal.calendars()

        target = set(calendar_names) if calendar_names else _DEFAULT_CALS
        chosen = [c for c in all_cals if (c.get_display_name() or "") in target]
        if not chosen:
            chosen = all_cals

        now   = datetime.now(tz=_TZ)
        end_q = now + timedelta(days=days)

        events = []
        for cal in chosen:
            cal_name = cal.get_display_name() or "?"
            try:
                results = cal.date_search(start=now, end=end_q, expand=True)
                for ev in results:
                    try:
                        info = _parse_event(ev)
                        if info:
                            info["calendar"] = cal_name
                            events.append(info)
                    except Exception as e:
                        log.debug(f"解析事件失败: {e}")
            except Exception as e:
                log.warning(f"读取日历 {cal_name} 失败: {e}")

        if not events:
            end_fmt = end_q.strftime("%m-%d")
            return f"未来 {days} 天（至 {end_fmt}）暂无日程。"

        events.sort(key=lambda e: e["start"] or datetime.max.replace(tzinfo=_TZ))

        lines = [f"📅 未来 {days} 天日程（北京时间，共 {len(events)} 条）："]
        cur_date = None
        for ev in events:
            d = ev["start"].strftime("%Y-%m-%d %a") if ev["start"] else "?"
            if d != cur_date:
                lines.append(f"\n【{d}】")
                cur_date = d
            if ev["all_day"]:
                time_str = "全天"
            else:
                t_start = ev["start"].strftime("%H:%M")
                t_end   = ev["end"].strftime("%H:%M") if ev["end"] and ev["end"] != ev["start"] else ""
                time_str = f"{t_start}–{t_end}" if t_end else t_start
            loc  = f"  📍{ev['location']}" if ev["location"] else ""
            desc = f"\n    备注：{ev['desc']}" if ev["desc"] else ""
            lines.append(f"  {time_str}  {ev['summary']}  [{ev['calendar']}]{loc}{desc}")

        return "\n".join(lines)

    except Exception as e:
        log.error(f"日历查询失败: {e}", exc_info=True)
        return f"日历查询失败：{e}"


def execute_calendar_add(
    summary: str,
    start: str,
    end: str = "",
    calendar_name: str = "个人",
    location: str = "",
    description: str = "",
) -> str:
    """
    添加一个日历事件。
    start/end 格式：'YYYY-MM-DD' (全天) 或 'YYYY-MM-DD HH:MM' (具体时间)
    """
    try:
        from icalendar import Calendar as iCal, Event
        import uuid

        def _parse_dt(s):
            s = (s or "").strip()
            if not s:
                return None, True
            if " " in s and ":" in s:
                dt = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=_TZ)
                return dt, False
            d = datetime.strptime(s, "%Y-%m-%d").date()
            return d, True

        dt_start, all_day = _parse_dt(start)
        if dt_start is None:
            return "❌ 开始时间格式错误，请用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM"

        dt_end, _ = _parse_dt(end) if end else (None, all_day)
        if dt_end is None:
            if all_day:
                dt_end = dt_start + timedelta(days=1)
            else:
                dt_end = dt_start + timedelta(hours=1)

        cal = iCal()
        cal.add("prodid", "-//tg-bot//iCloud//")
        cal.add("version", "2.0")
        ev = Event()
        ev.add("uid",      str(uuid.uuid4()))
        ev.add("summary",  summary)
        ev.add("dtstart",  dt_start)
        ev.add("dtend",    dt_end)
        ev.add("dtstamp",  datetime.now(tz=timezone.utc))
        if location:
            ev.add("location", location)
        if description:
            ev.add("description", description)
        cal.add_component(ev)

        client    = _get_client()
        principal = client.principal()
        all_cals  = principal.calendars()
        target_cal = next(
            (c for c in all_cals if (c.get_display_name() or "") == calendar_name), None
        )
        if target_cal is None:
            target_cal = all_cals[0]
            log.warning(f"未找到日历「{calendar_name}」，改用「{target_cal.get_display_name()}」")

        target_cal.save_event(cal.to_ical().decode("utf-8"))

        start_fmt = dt_start.strftime("%Y-%m-%d %H:%M") if isinstance(dt_start, datetime) else str(dt_start)
        cal_real  = target_cal.get_display_name()
        return f"✅ 已添加到「{cal_real}」：{summary}（{start_fmt}）"

    except Exception as e:
        log.error(f"日历添加失败: {e}", exc_info=True)
        return f"日历添加失败：{e}"
