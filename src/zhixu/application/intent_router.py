"""Chinese rule parsing first; optional strict model proposal second."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import ValidationError as PydanticValidationError

from zhixu.domain import (
    UNKNOWN_YEAR,
    ActionLink,
    DataClassification,
    normalise_lead_minutes,
)
from zhixu.domain.agenda import BUSINESS_CALENDARS
from zhixu.domain.errors import InvalidModelOutput, LLMUnavailable, ValidationError
from zhixu.ports import Clock, LLMCallReason, LLMRequest

from .intents import IntentAction, ModelIntentProposal, ParsedIntent
from .llm import LLMGateway
from .prompts import render_prompt
from .temporal_context import temporal_context_prompt

# Generated from the installed calendars so a new region never needs a prompt edit.
_BUSINESS_CALENDAR_CHOICES = ", ".join(
    f"{calendar.token} for a {calendar.label} payday"
    for calendar in sorted(
        BUSINESS_CALENDARS.values(),
        key=lambda calendar: calendar.token,
    )
)


@lru_cache(maxsize=1)
def _system_prompt() -> str:
    """The packaged classification prompt, rendered once and reused verbatim.

    Holding it stable across requests is what lets the provider cache it.
    """

    return render_prompt(
        "intent_router_system.md",
        business_calendars=_BUSINESS_CALENDAR_CHOICES,
    )

_MUTATING_MODEL_ACTIONS = {
    IntentAction.CREATE_AGENDA,
    IntentAction.CREATE_ANNIVERSARY,
    IntentAction.CREATE_DAILY_BRIEFING,
    IntentAction.CREATE_TASK,
    IntentAction.CREATE_NOTE,
    IntentAction.ADD_NOTE_CONTENT_BLOCK,
    IntentAction.MOVE_NOTE_CATEGORY,
    IntentAction.CREATE_REMINDER,
    IntentAction.CANCEL_REMINDER,
    IntentAction.ACKNOWLEDGE_REMINDER,
    IntentAction.SNOOZE_REMINDER,
    IntentAction.COMPLETE_TASK,
    IntentAction.POSTPONE_TASK,
    IntentAction.DELETE_RESOURCE,
}

_ACTION_URL_PATTERN = re.compile(r"https://[^\s]+", re.IGNORECASE)
_ACTION_URL_TRAILING = ".,;!?，。；！？、）)]}>'\""
_BRIEFING_INCLUSION_PATTERN = re.compile(
    r"(?:并入|纳入|加入|放入|显示在|出现在).{0,8}(?:每日)?(?:早报|简报)(?:中|里)?"
)
_EXPLICIT_NOTIFICATION_PATTERN = re.compile(r"提醒|通知|推送|叫我|告诉我")
_NATURAL_NOTE_PATTERN = re.compile(
    r"^(?:请|麻烦)?(?:帮我)?(?:登记|记录|记下|保存)(?:一下|下)?[：:，,\s]*(?P<body>.+)$",
    re.DOTALL,
)
_SCHEDULING_CUE_PATTERN = re.compile(
    r"(?:提醒|通知|待办|任务|日程|会议|活动|截止|到期|"
    r"今天|明天|后天|下周|下月|每(?:天|周|月|年)|"
    r"\d{1,2}(?:[:：点时]|月|日|号)|\d{4}-\d{1,2}-\d{1,2})"
)
_STRUCTURED_NOTE_SAVE_PATTERN = re.compile(
    r"^(?:请|麻烦)?(?:帮我)?(?:保存|登记|记录)(?:到|至)\s*"
    r"(?:[“\"](?P<quoted_path>[^”\"]+)[”\"]|(?P<plain_path>[^，,:：]+))"
    r"\s*[，,:：]\s*(?P<rest>.+)$",
    re.DOTALL,
)
_NOTE_BLOCK_PATTERN = re.compile(
    r"^(?:新增|添加)?(?:一条|一组)?\s*[“\"](?P<name>[^”\"]+)[”\"]"
    r"\s*[，,:：]?\s*(?P<content>.*)$",
    re.DOTALL,
)
_APPEND_NOTE_BLOCK_PATTERN = re.compile(
    r"^在\s*(?P<title>.+?)(?:条目)?(?:下|里)\s*(?:再)?"
    r"(?:记|记录|保存|新增|添加)(?:一条|一组)?\s*"
    r"[“\"](?P<name>[^”\"]+)[”\"]\s*[，,:：]?\s*(?P<content>.+)$",
    re.DOTALL,
)
_MOVE_NOTE_CATEGORY_PATTERN = re.compile(
    r"^把\s*(?P<title>.+?)\s*(?:条目|备忘)?\s*移(?:动)?到\s*"
    r"(?:[“\"](?P<quoted_path>[^”\"]+)[”\"]|(?P<plain_path>.+))$"
)


def _natural_note_title(body: str) -> str:
    first_part = re.split(r"[,，;；\n]", body, maxsplit=1)[0].strip()
    return (first_part or body)[:80]


def _note_fields(content: str) -> tuple[list[dict[str, str]], str]:
    fields: list[dict[str, str]] = []
    free_text: list[str] = []
    for part in (value.strip() for value in re.split(r"[，,；;\n]+", content)):
        if not part:
            continue
        match = re.fullmatch(r"([^:：]{1,80})\s*[:：]\s*(.+)", part)
        if match is None:
            match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_.-]{0,79})\s+(.+)", part)
        if match is None:
            free_text.append(part)
            continue
        fields.append({"name": match.group(1).strip(), "value": match.group(2).strip()})
    return fields, "\n".join(free_text)


def _structured_note_route(value: str) -> ParsedIntent | None:
    move = _MOVE_NOTE_CATEGORY_PATTERN.fullmatch(value)
    if move is not None:
        raw_path = move.group("quoted_path") or move.group("plain_path") or ""
        path = tuple(
            part.strip()
            for part in re.split(r"\s*(?:/|>|＞)\s*", raw_path)
            if part.strip()
        )
        if path:
            return ParsedIntent(
                IntentAction.MOVE_NOTE_CATEGORY,
                {
                    "entry_query": move.group("title").strip(),
                    "category_path": path,
                },
                requires_confirmation=True,
            )
    append = _APPEND_NOTE_BLOCK_PATTERN.fullmatch(value)
    if append is not None:
        fields, body = _note_fields(append.group("content"))
        return ParsedIntent(
            IntentAction.ADD_NOTE_CONTENT_BLOCK,
            {
                "entry_query": append.group("title").strip(),
                "block": {
                    "name": append.group("name").strip(),
                    "body": body,
                    "fields": fields,
                },
            },
            requires_confirmation=True,
        )
    structured = _STRUCTURED_NOTE_SAVE_PATTERN.fullmatch(value)
    if structured is None:
        return None
    raw_path = structured.group("quoted_path") or structured.group("plain_path") or ""
    path = tuple(
        part.strip()
        for part in re.split(r"\s*(?:/|>|＞)\s*", raw_path)
        if part.strip()
    )
    if not path:
        return None
    rest = structured.group("rest").strip()
    block_match = _NOTE_BLOCK_PATTERN.fullmatch(rest)
    block_name = "默认内容"
    content = rest
    if block_match is not None:
        block_name = block_match.group("name").strip()
        content = block_match.group("content").strip()
    fields, block_body = _note_fields(content)
    return ParsedIntent(
        IntentAction.CREATE_NOTE,
        {
            "title": path[-1],
            "body": content or rest,
            "category_path": path[:-1] or ("未分类",),
            "content_blocks": [
                {
                    "name": block_name,
                    "body": block_body,
                    "fields": fields,
                }
            ],
        },
        requires_confirmation=True,
    )


def _redact_action_links(text: str) -> tuple[str, tuple[str, ...]]:
    """Replace exact user URLs before sending scheduling text to an external model."""

    urls: list[str] = []

    def replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        url = candidate.rstrip(_ACTION_URL_TRAILING)
        trailing = candidate[len(url) :]
        if not url or len(urls) >= 8:
            return candidate
        try:
            ActionLink("打开链接", url)
        except ValidationError:
            return f"<INVALID_LINK>{trailing}"
        urls.append(url)
        return f"<LINK_{len(urls)}>{trailing}"

    return _ACTION_URL_PATTERN.sub(replace, text), tuple(urls)


def _fallback_link_label(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    if hostname == "meeting.tencent.com" or hostname.endswith(".meeting.tencent.com"):
        return "加入会议"
    if hostname == "docs.qq.com" or hostname.endswith(".docs.qq.com"):
        return "打开文档"
    return "打开链接"


_LEAD_UNITS = {
    "分钟": 1, "分": 1, "min": 1, "m": 1,
    "小时": 60, "时": 60, "h": 60, "hour": 60,
    "天": 1440, "日": 1440, "d": 1440, "day": 1440,
}


def _parse_lead_minutes(value: str) -> tuple[int, ...] | None:
    """Read a lead-time list such as "24小时 6小时 30分钟 准点"."""

    parts = [part for part in re.split(r"[\s,，、]+", value.strip()) if part]
    if not parts:
        return None
    minutes: list[int] = []
    for part in parts:
        if part in {"准点", "开始时", "0"}:
            minutes.append(0)
            continue
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([^\d\s]+)", part)
        if match is None:
            return None
        unit = _LEAD_UNITS.get(match.group(2).lower())
        if unit is None:
            return None
        scaled = float(match.group(1)) * unit
        if scaled != int(scaled):
            return None
        minutes.append(int(scaled))
    try:
        return normalise_lead_minutes(minutes)
    except ValidationError:
        return None


def _parse_switch(value: str) -> bool | None:
    if value in {"开", "启用", "on", "开启"}:
        return True
    if value in {"关", "停用", "off", "关闭"}:
        return False
    return None


def _parse_wall_time(value: str) -> time | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _parse_important_day_edit(rest: str) -> dict[str, object] | None:
    field, _, body = rest.strip().partition(" ")
    body = body.strip()
    if field == "名称" and body:
        return {"title": body}
    if field == "类型":
        if body in {"生日", "birthday"}:
            return {"kind": "birthday"}
        if body in {"纪念日", "anniversary"}:
            return {"kind": "anniversary"}
        return None
    if field == "预告":
        if body in {"关闭", "关", "无"}:
            return {"advance_days": ()}
        days = [part for part in re.split(r"[\s,，、]+", body) if part]
        if not days or any(not part.isdigit() for part in days):
            return None
        values = tuple(sorted({int(part) for part in days}, reverse=True))
        if any(not 1 <= day <= 366 for day in values) or len(values) > 8:
            return None
        return {"advance_days": values}
    if field == "日期":
        lunar = re.fullmatch(r"农历\s+(闰)?(\d{1,2})-(\d{1,2})", body)
        if lunar:
            return {
                "calendar": "lunar",
                "lunar_leap": lunar.group(1) is not None,
                "lunar_month": int(lunar.group(2)),
                "lunar_day": int(lunar.group(3)),
            }
        solar = re.fullmatch(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})", body)
        if solar is None:
            return None
        year = int(solar.group(1)) if solar.group(1) else UNKNOWN_YEAR
        try:
            anchor = date(year, int(solar.group(2)), int(solar.group(3)))
        except ValueError:
            return None
        return {"calendar": "solar", "anchor_date": anchor}
    return None


def _parse_briefing_edit(rest: str) -> dict[str, object] | None:
    field, _, body = rest.strip().partition(" ")
    body = body.strip()
    if field == "时间":
        when = _parse_wall_time(body)
        return None if when is None else {"briefing_time": when}
    if field == "开关":
        switch = _parse_switch(body)
        return None if switch is None else {"enabled": switch}
    return None


def _parse_notification_edit(rest: str) -> dict[str, object] | None:
    field, _, body = rest.strip().partition(" ")
    body = body.strip()
    if field == "文本" and body:
        return {"text": body}
    if field == "时间":
        when = _parse_wall_time(body)
        return None if when is None else {"time_of_day": when}
    if field == "提前":
        if not re.fullmatch(r"-?\d{1,3}", body):
            return None
        offset = int(body)
        return None if not -366 <= offset <= 366 else {"day_offset": offset}
    if field == "开关":
        switch = _parse_switch(body)
        return None if switch is None else {"enabled": switch}
    return None


class RuleIntentRouter:
    def __init__(self, clock: Clock, *, timezone: str = "Asia/Shanghai") -> None:
        self.clock = clock
        self.timezone = ZoneInfo(timezone)

    def route(self, text: str) -> ParsedIntent | None:
        value = text.strip()
        compact = re.sub(r"\s+", "", value)
        if value.lower() in {"/帮助", "/help", "帮助", "help", "/菜单", "菜单"}:
            return ParsedIntent(IntentAction.HELP)
        structured_note = _structured_note_route(value)
        if structured_note is not None:
            return structured_note
        natural_note = _NATURAL_NOTE_PATTERN.fullmatch(value)
        if natural_note:
            body = natural_note.group("body").strip()
            if body and not _SCHEDULING_CUE_PATTERN.search(body):
                return ParsedIntent(
                    IntentAction.CREATE_NOTE,
                    {"title": _natural_note_title(body), "body": body},
                    requires_confirmation=True,
                )
        # QQ never tells us a group nickname, so a member states their own.
        rename = re.fullmatch(r"/(?:我叫|改名)\s+(.{1,40})", value)
        if rename:
            return ParsedIntent(
                IntentAction.SET_DISPLAY_NAME,
                {"display_name": rename.group(1).strip()},
            )
        plan_confirmation = re.fullmatch(
            r"/(确认|拒绝|取消)计划\s+(plan_[A-Za-z0-9_-]{8,80})",
            value,
        )
        if plan_confirmation:
            return ParsedIntent(
                {
                    "确认": IntentAction.CONFIRM_PLAN,
                    "拒绝": IntentAction.REJECT_PLAN,
                    "取消": IntentAction.CANCEL_PLAN,
                }[plan_confirmation.group(1)],
                {"plan_id": plan_confirmation.group(2)},
            )
        # Button payloads only; the time is fixed by the preset that was pressed,
        # so the whole notification adjustment stays deterministic.
        plan_notification = re.fullmatch(
            r"/计划(免)?通知\s+(plan_[A-Za-z0-9_-]{8,80})"
            r"(?:\s+([01]?\d|2[0-3]):([0-5]\d))?",
            value,
        )
        if plan_notification:
            arguments: dict[str, object] = {"plan_id": plan_notification.group(2)}
            if plan_notification.group(1):
                arguments["disable"] = True
            elif plan_notification.group(3) is not None:
                arguments["time_of_day"] = (
                    f"{int(plan_notification.group(3)):02d}:{plan_notification.group(4)}"
                )
            return ParsedIntent(IntentAction.ADJUST_PLAN_NOTIFICATION, arguments)
        if value in {"/今天", "/日程"} or compact in {
            "今天有什么安排",
            "今天的日程",
            "今日安排",
        }:
            return ParsedIntent(IntentAction.LIST_AGENDA)
        if value in {"/全部日程", "/所有日程", "/未来日程"} or compact in {
            "列出所有日程",
            "所有未来日程",
            "查看全部日程",
        }:
            return ParsedIntent(IntentAction.LIST_ALL_AGENDA)
        if value == "/纪念日":
            return ParsedIntent(IntentAction.LIST_ANNIVERSARIES)
        anniversary = re.fullmatch(r"/纪念日\s+(.+?)\s+(\d{4}-\d{1,2}-\d{1,2})", value)
        if anniversary:
            try:
                anchor_date = datetime.strptime(
                    anniversary.group(2), "%Y-%m-%d"
                ).date()
            except ValueError:
                return None
            return ParsedIntent(
                IntentAction.CREATE_ANNIVERSARY,
                {"title": anniversary.group(1).strip(), "anchor_date": anchor_date},
            )
        important_day = re.fullmatch(
            r"(?:/重要日子|/纪念日|/生日)\s+(删除|改)\s+(\S+)(?:\s+(.*))?",
            value,
            re.DOTALL,
        )
        if important_day:
            operation, identifier, rest = important_day.groups()
            if operation == "删除":
                # Destroying a record is confirmed the same way as any
                # other delete, rather than on the strength of one message.
                return ParsedIntent(
                    IntentAction.DELETE_ANNIVERSARY,
                    {"anniversary_id": identifier},
                    requires_confirmation=True,
                )
            edit = _parse_important_day_edit(rest or "")
            if edit is None:
                return None
            return ParsedIntent(
                IntentAction.UPDATE_ANNIVERSARY,
                {"anniversary_id": identifier, **edit},
            )
        briefing_edit = re.fullmatch(
            r"/每日简报\s+(删除|改)\s+(\S+)(?:\s+(.*))?", value, re.DOTALL
        )
        if briefing_edit:
            operation, identifier, rest = briefing_edit.groups()
            if operation == "删除":
                # Destroying a record is confirmed the same way as any
                # other delete, rather than on the strength of one message.
                return ParsedIntent(
                    IntentAction.DELETE_DAILY_BRIEFING,
                    {"briefing_id": identifier},
                    requires_confirmation=True,
                )
            edit = _parse_briefing_edit(rest or "")
            if edit is None:
                return None
            return ParsedIntent(
                IntentAction.UPDATE_DAILY_BRIEFING,
                {"briefing_id": identifier, **edit},
            )
        notification_edit = re.fullmatch(
            r"/日程通知\s+(删除|改)\s+(\S+)(?:\s+(.*))?", value, re.DOTALL
        )
        if notification_edit:
            operation, identifier, rest = notification_edit.groups()
            if operation == "删除":
                # Destroying a record is confirmed the same way as any
                # other delete, rather than on the strength of one message.
                return ParsedIntent(
                    IntentAction.DELETE_AGENDA_NOTIFICATION,
                    {"rule_id": identifier},
                    requires_confirmation=True,
                )
            edit = _parse_notification_edit(rest or "")
            if edit is None:
                return None
            return ParsedIntent(
                IntentAction.UPDATE_AGENDA_NOTIFICATION,
                {"rule_id": identifier, **edit},
            )
        birthday = re.fullmatch(
            r"/生日\s+(.+?)\s+(?:(农历)\s+)?(?:(\d{4})-)?(\d{1,2})-(\d{1,2})",
            value,
        )
        if birthday:
            lunar = birthday.group(2) is not None
            year = int(birthday.group(3)) if birthday.group(3) else UNKNOWN_YEAR
            month, day = int(birthday.group(4)), int(birthday.group(5))
            arguments: dict[str, object] = {
                "title": birthday.group(1).strip(),
                "kind": "birthday",
            }
            if lunar:
                if not 1 <= month <= 12 or not 1 <= day <= 30:
                    return None
                arguments["calendar"] = "lunar"
                arguments["lunar_month"] = month
                arguments["lunar_day"] = day
                arguments["anchor_date"] = date(year, 1, 1)
            else:
                try:
                    arguments["anchor_date"] = date(year, month, day)
                except ValueError:
                    return None
            return ParsedIntent(IntentAction.CREATE_ANNIVERSARY, arguments)
        leads = re.fullmatch(r"/提前提醒\s+(.+)", value)
        if leads:
            minutes = _parse_lead_minutes(leads.group(1))
            if minutes is None:
                return None
            return ParsedIntent(
                IntentAction.SET_NOTIFICATION_LEADS,
                {"lead_minutes": minutes},
            )
        if value == "/每日简报":
            return ParsedIntent(IntentAction.LIST_DAILY_BRIEFINGS)
        daily_briefing = re.fullmatch(r"/每日简报\s+(\d{1,2}):(\d{2})", value)
        if daily_briefing:
            hour, minute = int(daily_briefing.group(1)), int(daily_briefing.group(2))
            if hour > 23 or minute > 59:
                return None
            return ParsedIntent(
                IntentAction.CREATE_DAILY_BRIEFING,
                {"briefing_time": time(hour, minute)},
            )
        if value.startswith("/循环 ") and value.removeprefix("/循环 ").strip():
            return ParsedIntent(
                IntentAction.CREATE_AGENDA,
                {
                    "model_parse": True,
                    "model_text": value.removeprefix("/循环 ").strip(),
                },
            )
        calendar_view = re.fullmatch(r"/(?:日历|月历)(?:\s+(\S+))?", value)
        if calendar_view:
            selector = calendar_view.group(1)
            if selector is None:
                return ParsedIntent(IntentAction.VIEW_CALENDAR, {})
            arguments = self._calendar_month_arguments(selector)
            if arguments is None:
                return ParsedIntent(
                    IntentAction.VIEW_CALENDAR,
                    {"invalid_month": selector},
                )
            return ParsedIntent(IntentAction.VIEW_CALENDAR, arguments)
        if value in {"/待办", "/任务"} or compact in {
            "有哪些待办",
            "我的待办",
            "查看任务",
        }:
            return ParsedIntent(IntentAction.LIST_TASKS)
        if value in {"/提醒", "/提醒列表"} or compact in {
            "有哪些提醒",
            "我的提醒",
            "查看提醒",
        }:
            return ParsedIntent(IntentAction.LIST_REMINDERS)
        if value in {"/备忘", "/备忘录", "/所有备忘"} or compact in {
            "查看所有备忘录条目",
            "查看所有备忘",
            "列出所有备忘录条目",
            "列出全部备忘",
            "所有备忘录",
            "备忘录列表",
        }:
            return ParsedIntent(IntentAction.LIST_NOTES)
        if value.startswith("/私人提醒 "):
            return ParsedIntent(
                IntentAction.CREATE_REMINDER,
                {"private": True},
            )
        for prefix in ("/搜索 ", "/查备忘 ", "搜索备忘 ", "查备忘 "):
            if value.startswith(prefix) and value.removeprefix(prefix).strip():
                return ParsedIntent(
                    IntentAction.SEARCH_NOTES,
                    {"query": value.removeprefix(prefix).strip()},
                )
        for prefix in ("/任务 ", "添加任务 ", "新建任务 ", "记个待办 "):
            if value.startswith(prefix) and value.removeprefix(prefix).strip():
                return ParsedIntent(
                    IntentAction.CREATE_TASK,
                    {"title": value.removeprefix(prefix).strip()},
                )
        if value.startswith("/私人任务 ") and value.removeprefix("/私人任务 ").strip():
            return ParsedIntent(
                IntentAction.CREATE_TASK,
                {
                    "title": value.removeprefix("/私人任务 ").strip(),
                    "private": True,
                },
            )
        for prefix in ("/记 ", "/备忘 ", "记住 "):
            if value.startswith(prefix) and value.removeprefix(prefix).strip():
                body = value.removeprefix(prefix).strip()
                return ParsedIntent(
                    IntentAction.CREATE_NOTE,
                    {"title": body[:80], "body": body},
                )
        if value.startswith("/私人记 ") and value.removeprefix("/私人记 ").strip():
            body = value.removeprefix("/私人记 ").strip()
            return ParsedIntent(
                IntentAction.CREATE_NOTE,
                {"title": body[:80], "body": body, "private": True},
            )
        completed = re.fullmatch(r"/完成\s+([A-Za-z0-9_-]{1,160})", value)
        if completed:
            return ParsedIntent(
                IntentAction.COMPLETE_TASK,
                {"task_id": completed.group(1)},
            )
        postponed = re.fullmatch(
            r"/延期\s+([A-Za-z0-9_-]{1,160})\s+(\d{1,4})(分钟|小时|天)",
            value,
        )
        if postponed:
            count = int(postponed.group(2))
            delta = {
                "分钟": timedelta(minutes=count),
                "小时": timedelta(hours=count),
                "天": timedelta(days=count),
            }[postponed.group(3)]
            return ParsedIntent(
                IntentAction.POSTPONE_TASK,
                {
                    "task_id": postponed.group(1),
                    "due_at": self.clock.now() + delta,
                },
            )
        acknowledged_reminder = re.fullmatch(
            r"/提醒完成\s+([A-Za-z0-9_-]{1,160})",
            value,
        )
        if acknowledged_reminder:
            return ParsedIntent(
                IntentAction.ACKNOWLEDGE_REMINDER,
                {"reminder_id": acknowledged_reminder.group(1)},
            )
        cancelled_reminder = re.fullmatch(
            r"/取消提醒\s+([A-Za-z0-9_-]{1,160})",
            value,
        )
        if cancelled_reminder:
            return ParsedIntent(
                IntentAction.CANCEL_REMINDER,
                {"reminder_id": cancelled_reminder.group(1)},
            )
        requested_cancel_reminder = re.fullmatch(
            r"/请求取消提醒\s+([A-Za-z0-9_-]{1,160})",
            value,
        )
        if requested_cancel_reminder:
            return ParsedIntent(
                IntentAction.CANCEL_REMINDER,
                {"reminder_id": requested_cancel_reminder.group(1)},
                requires_confirmation=True,
            )
        cancelled_agenda = re.fullmatch(
            r"/取消日程\s+([A-Za-z0-9_-]{1,160})",
            value,
        )
        if cancelled_agenda:
            return ParsedIntent(
                IntentAction.CANCEL_AGENDA,
                {"agenda_id": cancelled_agenda.group(1)},
                requires_confirmation=True,
            )
        snoozed_reminder = re.fullmatch(
            r"/提醒稍后\s+([A-Za-z0-9_-]{1,160})(?:\s+(\d{1,4})分钟)?",
            value,
        )
        if snoozed_reminder:
            minutes = int(snoozed_reminder.group(2) or 15)
            return ParsedIntent(
                IntentAction.SNOOZE_REMINDER,
                {
                    "reminder_id": snoozed_reminder.group(1),
                    "fire_at": self.clock.now() + timedelta(minutes=minutes),
                },
            )
        for prefix in ("/总结 ", "总结备忘 "):
            if value.startswith(prefix) and value.removeprefix(prefix).strip():
                return ParsedIntent(
                    IntentAction.SUMMARIZE_NOTES,
                    {"query": value.removeprefix(prefix).strip()},
                )
        reminder = self._parse_reminder(value)
        if reminder is not None:
            return reminder
        if value.startswith("/问 ") and value.removeprefix("/问 ").strip():
            return ParsedIntent(
                IntentAction.ANSWER,
                {
                    "query": value.removeprefix("/问 ").strip(),
                    "web_search": True,
                },
            )
        return None

    def _calendar_month_arguments(self, selector: str) -> dict[str, int] | None:
        """Resolve a /日历 month selector, or None when it is not understood.

        Relative selectors are resolved here rather than by the model so that
        month navigation keeps working with no language model configured.
        """

        text = selector.strip()
        absolute = re.fullmatch(r"(\d{4})[-/年](\d{1,2})月?", text)
        if absolute:
            year, month = int(absolute.group(1)), int(absolute.group(2))
            if not 1 <= month <= 12:
                return None
            return {"year": year, "month": month}

        current = self.clock.now().astimezone(self.timezone)
        offsets = {
            "本月": 0, "这个月": 0, "当月": 0, "本月份": 0,
            "下月": 1, "下个月": 1, "次月": 1,
            "上月": -1, "上个月": -1, "前一个月": -1,
        }
        offset: int | None = offsets.get(text)
        if offset is None:
            relative = re.fullmatch(r"([+-]\d{1,3})", text)
            if relative:
                offset = int(relative.group(1))
        if offset is not None:
            total = (current.year * 12 + current.month - 1) + offset
            year, month = divmod(total, 12)
            return {"year": year, "month": month + 1}

        bare = re.fullmatch(r"(\d{1,2})月?", text)
        if bare:
            month = int(bare.group(1))
            if not 1 <= month <= 12:
                return None
            # A bare month means the nearest one that has not already passed.
            year = current.year + int(month < current.month)
            return {"year": year, "month": month}
        return None

    def _parse_reminder(self, value: str) -> ParsedIntent | None:
        current = self.clock.now().astimezone(self.timezone)
        later = re.fullmatch(r"稍后提醒我(?P<title>.+)", value)
        if later:
            return ParsedIntent(
                IntentAction.CREATE_REMINDER,
                {
                    "title": later.group("title").strip(),
                    "fire_at": current + timedelta(minutes=15),
                },
            )
        relative = re.fullmatch(
            r"(?P<count>\d{1,4})(?P<unit>分钟|小时|天)后提醒我(?P<title>.+)",
            value,
        )
        if relative:
            count = int(relative.group("count"))
            unit = relative.group("unit")
            delta = {
                "分钟": timedelta(minutes=count),
                "小时": timedelta(hours=count),
                "天": timedelta(days=count),
            }[unit]
            return ParsedIntent(
                IntentAction.CREATE_REMINDER,
                {
                    "title": relative.group("title").strip(),
                    "fire_at": current + delta,
                },
            )
        absolute = re.fullmatch(
            r"(?P<day>今天|明天)(?P<period>上午|下午|晚上)?"
            r"(?P<hour>\d{1,2})(?:(?:[:：](?P<minute_colon>\d{1,2}))|"
            r"(?:点(?P<minute_point>\d{1,2})?分?))?"
            r"提醒我(?P<title>.+)",
            value,
        )
        if absolute:
            hour = int(absolute.group("hour"))
            minute = int(
                absolute.group("minute_colon")
                or absolute.group("minute_point")
                or 0
            )
            if absolute.group("period") in {"下午", "晚上"} and hour < 12:
                hour += 12
            if hour > 23 or minute > 59:
                return None
            day = current.date() + timedelta(days=absolute.group("day") == "明天")
            fire_at = datetime.combine(day, time(hour, minute), tzinfo=self.timezone)
            if fire_at <= current:
                return None
            return ParsedIntent(
                IntentAction.CREATE_REMINDER,
                {
                    "title": absolute.group("title").strip(),
                    "fire_at": fire_at,
                },
            )
        return None


class ModelIntentClassifier:
    def __init__(
        self,
        gateway: LLMGateway,
        *,
        model: str,
        confidence_threshold: float = 0.8,
    ) -> None:
        self.gateway = gateway
        self.model = model
        self.confidence_threshold = confidence_threshold

    def classify(
        self,
        owner_user_id: str,
        text: str,
        *,
        reason: LLMCallReason,
        reference_time: datetime | None = None,
        revision_context: str = "",
        required_action: IntentAction | None = None,
    ) -> ParsedIntent:
        redacted_text, source_urls = _redact_action_links(text)
        briefing_inclusion = bool(_BRIEFING_INCLUSION_PATTERN.search(redacted_text))
        notification_requested = bool(
            _EXPLICIT_NOTIFICATION_PATTERN.search(
                _BRIEFING_INCLUSION_PATTERN.sub("", redacted_text)
            )
        )
        user_sections: list[str] = []
        if reference_time is not None:
            user_sections.append(temporal_context_prompt(reference_time))
        if revision_context:
            user_sections.append(f"Existing plan: {revision_context}")
        if required_action is not None:
            user_sections.append(
                f"Required action: {required_action.value} (changing it is forbidden)"
            )
        user_sections.append(f"User request:\n{redacted_text}")
        request = LLMRequest(
            model=self.model,
            system_prompt=_system_prompt(),
            user_prompt="\n\n".join(user_sections),
            response_schema=ModelIntentProposal.model_json_schema(),
        )
        response = self.gateway.generate(
            owner_user_id=owner_user_id,
            request=request,
            classification=DataClassification.PERSONAL,
            reason=reason,
        )
        try:
            proposal = ModelIntentProposal.model_validate_json(response.content)
        except (PydanticValidationError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidModelOutput("model intent did not match the schema") from exc
        if proposal.confidence < self.confidence_threshold:
            raise LLMUnavailable("model intent confidence is too low")
        link_labels = {
            link.source_index: link.label.strip()
            for link in proposal.links
            if link.source_index <= len(source_urls) and link.label.strip()
        }
        action_links = [
            ActionLink(link_labels.get(index, _fallback_link_label(url)), url)
            for index, url in enumerate(source_urls, start=1)
        ]
        notifications = proposal.notifications
        if briefing_inclusion and not notification_requested:
            notifications = []
        arguments = {
            key: value
            for key, value in {
                "query": proposal.query,
                "title": proposal.title,
                "body": proposal.body,
                "answer": proposal.answer,
                "fire_at": proposal.fire_at,
                "due_at": proposal.due_at,
                "start_at": proposal.start_at,
                "end_at": proposal.end_at,
                "recurrence_rule": proposal.recurrence_rule,
                "anchor_date": proposal.anchor_date,
                "briefing_time": proposal.briefing_time,
                "notifications": (
                    notifications
                    if briefing_inclusion and not notification_requested
                    else notifications or None
                ),
                "links": action_links or None,
                "kind": proposal.important_day_kind or "anniversary",
                "calendar": proposal.calendar_system or "solar",
                "lunar_month": proposal.lunar_month,
                "lunar_day": proposal.lunar_day,
                "lunar_leap": proposal.lunar_leap,
                "year": proposal.view_year,
                "month": proposal.view_month,
                # Only meaningful alongside notifications, and False is the
                # same as absent, so let the None filter drop it otherwise.
                "notification_defaulted": proposal.notification_defaulted or None,
                # An empty list means the model said nothing about these, not
                # that the user asked for none; passing it on would override the
                # defaults with silence.
                "advance_days": tuple(proposal.advance_days) or None,
                "lead_minutes": tuple(proposal.lead_minutes) or None,
                "include_in_daily_briefing": (
                    proposal.include_in_daily_briefing or briefing_inclusion
                ),
                "private": proposal.private or None,
                "task_id": proposal.task_id,
                "reminder_id": proposal.reminder_id,
                "resource_id": proposal.resource_id,
            }.items()
            if value is not None
        }
        return ParsedIntent(
            proposal.action,
            arguments,
            source="llm",
            requires_confirmation=proposal.action in _MUTATING_MODEL_ACTIONS,
        )
