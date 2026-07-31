"""Chinese rule parsing first; optional strict model proposal second."""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError as PydanticValidationError

from zhixu.domain import DataClassification
from zhixu.domain.errors import InvalidModelOutput, LLMUnavailable
from zhixu.ports import Clock, LLMCallReason, LLMRequest

from .intents import IntentAction, ModelIntentProposal, ParsedIntent
from .llm import LLMGateway

_MUTATING_MODEL_ACTIONS = {
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


class RuleIntentRouter:
    def __init__(self, clock: Clock, *, timezone: str = "Asia/Shanghai") -> None:
        self.clock = clock
        self.timezone = ZoneInfo(timezone)

    def route(self, text: str) -> ParsedIntent | None:
        value = text.strip()
        compact = re.sub(r"\s+", "", value)
        if value in {"/今天", "/日程"} or compact in {
            "今天有什么安排",
            "今天的日程",
            "今日安排",
        }:
            return ParsedIntent(IntentAction.LIST_AGENDA)
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
        for prefix in ("/记 ", "/备忘 ", "记住 "):
            if value.startswith(prefix) and value.removeprefix(prefix).strip():
                body = value.removeprefix(prefix).strip()
                return ParsedIntent(
                    IntentAction.CREATE_NOTE,
                    {"title": body[:80], "body": body},
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
                {"query": value.removeprefix("/问 ").strip()},
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
    ) -> ParsedIntent:
        temporal_context = (
            f" Reference time: {reference_time.isoformat()}."
            if reference_time is not None
            else ""
        )
        request = LLMRequest(
            model=self.model,
            system_prompt=(
                "Classify the request into the provided schema. Never invent identifiers, "
                "times, or actions. Resolve relative time only from the supplied reference "
                f"time. Return only JSON.{temporal_context}"
            ),
            user_prompt=text,
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
        arguments = {
            key: value
            for key, value in {
                "query": proposal.query,
                "title": proposal.title,
                "answer": proposal.answer,
                "fire_at": proposal.fire_at,
                "due_at": proposal.due_at,
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
