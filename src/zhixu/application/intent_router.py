"""Chinese rule parsing first; optional strict model proposal second."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import ValidationError as PydanticValidationError

from zhixu.domain import (
    UNKNOWN_YEAR,
    ActionLink,
    DataClassification,
    normalise_lead_minutes,
)
from zhixu.domain.errors import InvalidModelOutput, LLMUnavailable, ValidationError
from zhixu.ports import Clock, LLMCallReason, LLMRequest

from .intents import IntentAction, ModelIntentProposal, ParsedIntent
from .llm import LLMGateway

_MUTATING_MODEL_ACTIONS = {
    IntentAction.CREATE_AGENDA,
    IntentAction.CREATE_ANNIVERSARY,
    IntentAction.CREATE_DAILY_BRIEFING,
    IntentAction.CREATE_TASK,
    IntentAction.CREATE_NOTE,
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


class RuleIntentRouter:
    def __init__(self, clock: Clock, *, timezone: str = "Asia/Shanghai") -> None:
        self.clock = clock
        self.timezone = ZoneInfo(timezone)

    def route(self, text: str) -> ParsedIntent | None:
        value = text.strip()
        compact = re.sub(r"\s+", "", value)
        if value.lower() in {"/帮助", "/help", "帮助", "help", "/菜单", "菜单"}:
            return ParsedIntent(IntentAction.HELP)
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
        calendar_view = re.fullmatch(
            r"/(?:日历|月历)(?:\s+(\d{4})-(\d{1,2}))?",
            value,
        )
        if calendar_view:
            arguments = {}
            if calendar_view.group(1) is not None:
                arguments = {
                    "year": int(calendar_view.group(1)),
                    "month": int(calendar_view.group(2)),
                }
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
        temporal_context = (
            f" Reference time: {reference_time.isoformat()}."
            if reference_time is not None
            else ""
        )
        revision_instruction = (
            " Revise the existing structured plan using the user's latest changes. "
            "Keep every field the user did not ask to change. A request about notification "
            "wording, time, or card style must only update notifications and must preserve "
            "the recurring event and recurrence_rule. "
            + (
                f"You MUST return action={required_action.value}; changing the action is "
                "forbidden. "
                if required_action is not None
                else ""
            )
            + "Existing plan: "
            f"{revision_context}"
            if revision_context
            else ""
        )
        request = LLMRequest(
            model=self.model,
            system_prompt=(
                "Classify the request into the provided schema. Never invent identifiers, "
                "times, or actions. Resolve relative time only from the supplied reference "
                "time. For a reminder request use action=create_reminder and include title, "
                "confidence, and an ISO-8601 fire_at with timezone. For a recurring calendar "
                "event use action=create_agenda with title, aware start_at, aware end_at, and "
                "an RFC 5545 recurrence_rule; an unspecified event time means an all-day "
                "event starting at local midnight and ending at the next local midnight. "
                "This project uses the ordinary calendar for all events except salary. "
                "Only when the event is salary/payday on the second-to-last Hong Kong "
                "business day of every month, set recurrence_rule exactly to "
                "X-BUSINESS-DAY;CALENDAR=HK_GENERAL_HOLIDAYS;BYSETPOS=-2. Never use that "
                "Hong Kong rule for anniversaries, briefings, or other recurring events. "
                "For that custom business-day rule, use the supplied reference date at "
                "local midnight as start_at and the next local midnight as end_at; the "
                "deterministic calendar engine, not the model, chooses each actual payday. "
                "A recurring calendar request may also contain zero or more notification "
                "rules. Put each in notifications with a timezone-free time_of_day, "
                "day_offset relative to the event date (0 means the same day, -1 means "
                "one day before), and the exact requested notification text. Do not put "
                "the notification wording into the calendar title. "
                "URLs in the request are replaced by <LINK_N> placeholders and are never "
                "available to you. For every link that belongs to the created resource, "
                "return its source_index N and a short action label in links; never invent, "
                "repeat, or reconstruct a URL. A phrase such as include/show/add the event "
                "in the daily briefing is not a notification rule: set "
                "include_in_daily_briefing=true and leave notifications empty unless the "
                "user separately asks for a reminder, notification, or push. "
                "For an anniversary use action=create_anniversary with title and "
                "anchor_date, and set important_day_kind=anniversary. For a birthday use "
                "the same action with important_day_kind=birthday; anchor_date carries the "
                "birth date, or 0001-01-01 when the year is unknown. When the user states "
                "the date on the Chinese lunisolar calendar, set calendar_system=lunar with "
                "lunar_month, lunar_day and lunar_leap, and never convert it yourself. "
                "Otherwise leave calendar_system=solar. Put any requested advance notice in "
                "advance_days as whole days before the date. "
                "When the user is changing how far ahead calendar events are announced, use "
                "action=set_notification_leads and put every requested lead in lead_minutes "
                "as minutes before the event starts, where 0 means the moment it starts. "
                "For a daily morning "
                "briefing use action=create_daily_briefing and briefing_time; use 08:00 only "
                "when the user says morning without a precise time. Return only JSON."
                f"{temporal_context}{revision_instruction}"
            ),
            user_prompt=redacted_text,
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
                "advance_days": tuple(proposal.advance_days),
                "lead_minutes": tuple(proposal.lead_minutes),
                "include_in_daily_briefing": (
                    proposal.include_in_daily_briefing or briefing_inclusion
                ),
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
