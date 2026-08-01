"""Deterministic assistant workflow with optional model fallback."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from zhixu.channels import ButtonActionKind, CalendarPreview, MessageButton
from zhixu.domain import (
    ActionLink,
    CalendarSystem,
    CommandContext,
    DataClassification,
    ImportantDayKind,
    TaskStatus,
)
from zhixu.domain.errors import (
    InvalidModelOutput,
    LLMUnavailable,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from zhixu.domain.hong_kong_calendar import monthly_business_day
from zhixu.ports import LLMCallReason, LLMRequest, PendingPlanStorePort
from zhixu.security import web_query_is_safe

from .commands import (
    AcknowledgeReminder,
    CancelReminder,
    CreateAgenda,
    CreateAgendaNotification,
    CreateAnniversary,
    CreateDailyBriefing,
    CreateNote,
    CreateReminder,
    CreateTask,
    DeleteAgenda,
    PostponeTask,
    SetNotificationLeads,
    SnoozeReminder,
    TransitionTask,
)
from .intent_router import ModelIntentClassifier, RuleIntentRouter
from .intents import (
    AssistantReply,
    IntentAction,
    ModelNotificationProposal,
    ParsedIntent,
)
from .llm import LLMGateway
from .queries import (
    AgendaBetween,
    ListAgendaItems,
    ListAnniversaries,
    ListDailyBriefings,
    ListReminders,
    ListTasks,
    SearchNotes,
)
from .services import ZhixuServices


class _SummaryEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1, max_length=4000)


class _QuestionPlanEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    capability: Literal[
        "runtime_datetime",
        "zhixu_data",
        "model_knowledge",
        "web_search",
    ]
    answer: str | None = Field(default=None, min_length=1, max_length=4000)
    search_query: str | None = Field(default=None, min_length=1, max_length=500)


class _WebSourceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=2048)


class _WebAnswerEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str = Field(min_length=1, max_length=3200)
    sources: list[_WebSourceEnvelope] = Field(max_length=5)


def _escape_markdown_text(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+\-.!>|])", r"\\\1", value)


def _encode_plan_value(value):
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"$type": "time", "value": value.isoformat()}
    if isinstance(value, ModelNotificationProposal):
        return {
            "$type": "notification",
            "value": value.model_dump(mode="json"),
        }
    if isinstance(value, ActionLink):
        return {
            "$type": "action_link",
            "value": {"label": value.label, "url": value.url},
        }
    if isinstance(value, dict):
        return {str(key): _encode_plan_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_plan_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValidationError("pending plan contains an unsupported value")


def _decode_plan_value(value):
    if isinstance(value, list):
        return [_decode_plan_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    value_type = value.get("$type")
    encoded = value.get("value")
    if value_type == "datetime" and isinstance(encoded, str):
        return datetime.fromisoformat(encoded)
    if value_type == "date" and isinstance(encoded, str):
        return date.fromisoformat(encoded)
    if value_type == "time" and isinstance(encoded, str):
        return time.fromisoformat(encoded)
    if value_type == "notification" and isinstance(encoded, dict):
        return ModelNotificationProposal.model_validate_json(json.dumps(encoded))
    if value_type == "action_link" and isinstance(encoded, dict):
        return ActionLink(str(encoded.get("label") or ""), str(encoded.get("url") or ""))
    return {str(key): _decode_plan_value(item) for key, item in value.items()}


def _model_context_value(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, ModelNotificationProposal):
        return value.model_dump(mode="json")
    if isinstance(value, ActionLink):
        return {"label": value.label, "url": "<PRESERVED_USER_LINK>"}
    if isinstance(value, dict):
        return {str(key): _model_context_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_model_context_value(item) for item in value]
    return value


_HELP_TEXT = """# 知序 · 帮助

> 日程、提醒、待办、备忘与联网问答

- `/帮助`、`/help` 或 `/菜单` — 显示本卡片

## 日程与提醒
- `/今天` 或 `/日程` — 按时间查看今日日程与提醒
- `/全部日程` — 列出全部未来日程和待触发提醒，并提供取消入口
- `/日历` — 本月图片日历预览
- `/日历 2026-08` — 查看指定月份
- `/提醒` — 查看待处理提醒及其 ID
- `明天上午9点提醒我提交报告` — 创建日程提醒
- `30秒后参加会议，链接：https://…` — 创建带“打开链接”按钮的提醒
- `15分钟后提醒我关烤箱`、`稍后提醒我检查下载`
- `/提醒完成 reminder_ID`、`/取消提醒 reminder_ID`
- `/提醒稍后 reminder_ID 15分钟`
- `/循环 每周四创建疯狂星期四日程` — 创建循环事件
- `每个月倒数第二个香港工作日发工资` — 创建香港工作日工资事件
- `每周四创建活动并入每日早报中` — 纳入早报，不额外生成提醒

> 计划预览固定提供“接受 / 修改 / 取消创建”。群聊修改受 QQ 入站限制，
> 后续自然语言仍需再次 @机器人；私聊不需要重复 @。

## 纪念日与每日简报
- `/纪念日 名称 YYYY-MM-DD` — 创建纪念日
- `/纪念日` — 查看纪念日及累计天数
- `/每日简报 08:00` — 每天向当前会话推送纪念日、日程图和日程卡片
- `/每日简报` — 查看已配置的简报

## 待办
- `/待办` — 查看待办
- `/任务 要做的事` — 新建待办
- `/完成 task_ID`
- `/延期 task_ID 30分钟`

## 备忘
- `/记 需要记住的内容` — 保存备忘
- `/搜索 关键词` — 搜索备忘
- `/总结 关键词` — 总结相关备忘

## 联网问答
- `/问 问题` — 自动选择可信运行时、模型常识或联网搜索

## 身份绑定
- `/申请绑定` — 在未绑定的机器人私聊中申请绑定码
- `/绑定私聊 绑定码` — 在已启用的内部群中完成绑定

> `/日程` 已包含提醒；`/提醒` 保留为提醒状态管理入口。
> 提醒卡片可直接延后 5/15/30/60 分钟、完成或取消。"""

_PROJECT_ADMIN_HELP_TEXT = f"""{_HELP_TEXT}

## 项目管理
- `/登记内部群` — 生成一次性群登记码
- `/启用内部群 登记码` — 在目标群中完成启用

> 该命令仅对项目管理员开放。"""

_HELP_BUTTONS = (
    MessageButton("今日日程", "/今天"),
    MessageButton("月历预览", "/日历"),
    MessageButton("待办列表", "/待办"),
    MessageButton("提醒列表", "/提醒"),
)

_PUBLIC_GROUP_HELP_TEXT = """# 知序 · 公开群帮助

- `/帮助` — 查看公开群能力
- `/问 问题` — 自动选择可信运行时、模型常识或联网搜索

> 公开群不能读取或写入任何个人数据库、内部群共享库或高敏感数据。
> 请勿在联网问题中填写口令、密钥、银行卡号等敏感信息。"""

_INTERNAL_GROUP_HELP_TEXT = """# 知序 · 内部群帮助

> 本群只查询当前群共享库；不会读取任何成员的私人数据。

## 群共享
- `/今天` 或 `/日程` — 查看本群日程与提醒
- `/日历`、`/日历 2026-08` — 预览本群图片月历
- `/提醒` — 查看本群待处理提醒及 ID
- `/待办` — 查看本群待办
- `/任务 内容`、`/记 内容` — 写入本群共享库并记录创建人
- `明天上午9点提醒我提交报告` — 创建本群日程提醒
- `/搜索 关键词`、`/总结 关键词` — 查询本群共享备忘
- `/问 问题` — 能力规划问答；本群共享备忘仍按权限优先检索

## 管理
- `/完成 task_ID`、`/延期 task_ID 30分钟`
- `/提醒完成 reminder_ID`、`/取消提醒 reminder_ID`
- `/提醒稍后 reminder_ID 15分钟`
- `/循环 每周四创建疯狂星期四日程` — 创建本群循环事件
- `/纪念日 名称 YYYY-MM-DD` — 创建本群共享纪念日
- `/每日简报 08:00` — 每天向本群推送共享纪念日和共享日程

## 明确写入私人库
- `/私人任务 内容`
- `/私人记 内容`
- `/私人提醒 提醒内容和时间`

## 身份绑定
- `/绑定私聊 绑定码` — 为机器人私聊完成身份绑定

> 私人数据只能在与机器人的私聊中查询。"""


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, str)) and str(value).strip() else None


def _format_lead_minutes(values: tuple[int, ...]) -> str:
    parts = []
    for minutes in values:
        if minutes == 0:
            parts.append("准点")
        elif minutes % 1440 == 0:
            parts.append(f"{minutes // 1440}天")
        elif minutes % 60 == 0:
            parts.append(f"{minutes // 60}小时")
        else:
            parts.append(f"{minutes}分钟")
    return "、".join(parts)


def _important_day_created_text(anniversary, today: date) -> str:
    label = "生日" if anniversary.kind is ImportantDayKind.BIRTHDAY else "纪念日"
    if anniversary.calendar is CalendarSystem.LUNAR:
        when = f"农历{anniversary.lunar_month}月{anniversary.lunar_day}日"
        # A lunisolar date means nothing to most readers until it is grounded
        # in the Gregorian day it next lands on.
        upcoming = anniversary.next_occurrence(today)
    else:
        when = f"{anniversary.anchor_date:%Y-%m-%d}"
        upcoming = None
    text = f"已创建{label}：{anniversary.title}（{when}）"
    if anniversary.advance_days:
        text += "，提前 " + "、".join(
            f"{day}天" for day in anniversary.advance_days
        ) + " 预告"
    if upcoming is not None:
        text += f"，下次 {upcoming:%Y-%m-%d}"
    return text


class AssistantEngine:
    def __init__(
        self,
        *,
        services: ZhixuServices,
        router: RuleIntentRouter,
        classifier: ModelIntentClassifier | None = None,
        llm_gateway: LLMGateway | None = None,
        llm_model: str = "",
        web_search_enabled: bool = False,
        pending_plans: PendingPlanStorePort | None = None,
    ) -> None:
        self.services = services
        self.router = router
        self.classifier = classifier
        self.llm_gateway = llm_gateway
        self.llm_model = llm_model
        self.web_search_enabled = web_search_enabled
        self.pending_plans = pending_plans

    def handle(
        self,
        text: str,
        context: CommandContext,
        *,
        target_ref: str = "",
    ) -> AssistantReply:
        intent = self.router.route(text)
        if intent is None and self.pending_plans is not None and target_ref:
            revising = self.pending_plans.current(
                actor_user_id=context.actor_user_id,
                target_ref=target_ref,
                now=self.services.clock.now(),
            )
            if revising is not None and self.classifier is not None:
                cancellation_text = text.strip()
                if len(cancellation_text) <= 30 and re.search(
                    r"(?:取消创建|取消计划|不创建了?|算了|中止|终止)(?:吧|。|！|!)?$",
                    cancellation_text,
                ):
                    self.pending_plans.consume(
                        revising.id,
                        now=self.services.clock.now(),
                    )
                    return AssistantReply(
                        "已取消本次计划并退出连续修改。",
                        "plan_cancelled",
                        "deterministic",
                    )
                try:
                    original_action = IntentAction(revising.action)
                    original_arguments = _decode_plan_value(
                        json.loads(revising.payload_json)
                    )
                except (ValueError, TypeError, json.JSONDecodeError):
                    return AssistantReply(
                        "上一版计划无法安全恢复，请重新描述完整需求。",
                        "invalid_plan",
                        "deterministic",
                    )
                if not isinstance(original_arguments, dict):
                    return AssistantReply(
                        "上一版计划无法安全恢复，请重新描述完整需求。",
                        "invalid_plan",
                        "deterministic",
                    )
                revision_context = json.dumps(
                    {
                        "action": original_action.value,
                        "arguments": _model_context_value(original_arguments),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                try:
                    revised = self.classifier.classify(
                        context.actor_user_id,
                        text,
                        reason=LLMCallReason.SCHEDULE_PARSE,
                        reference_time=self.services.clock.now().astimezone(
                            self.router.timezone
                        ),
                        revision_context=revision_context,
                        required_action=original_action,
                    )
                except (InvalidModelOutput, LLMUnavailable, PermissionDenied):
                    return AssistantReply(
                        "没有理解修改内容。请说明要改的字段，例如：改成早上9点，文案改为……",
                        "invalid_plan_revision",
                        "deterministic",
                    )
                if revised.action is not original_action:
                    return self._stage_plan(
                        ParsedIntent(
                            original_action,
                            original_arguments,
                            source="deterministic",
                            requires_confirmation=True,
                        ),
                        context,
                        target_ref=target_ref,
                        notice=(
                            "模型试图改变计划类型，已拒绝该变更并保留原循环计划。"
                            "事件通知本身会使用提醒卡片。"
                        ),
                    )
                merged_arguments = dict(original_arguments)
                merged_arguments.update(revised.arguments)
                revised = ParsedIntent(
                    revised.action,
                    merged_arguments,
                    source=revised.source,
                    requires_confirmation=revised.requires_confirmation,
                )
                if revised.action not in {
                    IntentAction.CREATE_AGENDA,
                    IntentAction.CREATE_ANNIVERSARY,
                    IntentAction.CREATE_DAILY_BRIEFING,
                    IntentAction.CREATE_REMINDER,
                    IntentAction.CREATE_TASK,
                    IntentAction.CREATE_NOTE,
                }:
                    return AssistantReply(
                        "修改后的内容不是可确认的创建计划，请重新描述。",
                        "invalid_plan_revision",
                        "deterministic",
                    )
                return self._stage_plan(revised, context, target_ref=target_ref)
        model_parsed_action = (
            intent is not None
            and (
                intent.action is IntentAction.CREATE_REMINDER
                or bool(intent.arguments.get("model_parse"))
            )
        )
        if model_parsed_action:
            assert intent is not None
            if self.classifier is None:
                return AssistantReply(
                    "自然语言日程解析需要模型，但当前模型不可用。",
                    "llm_unavailable",
                    "deterministic",
                )
            try:
                model_text = str(intent.arguments.get("model_text") or text)
                private = bool(intent.arguments.get("private"))
                if private and model_text.strip().startswith("/私人提醒 "):
                    model_text = model_text.strip().removeprefix("/私人提醒 ")
                proposed = self.classifier.classify(
                    context.actor_user_id,
                    model_text,
                    reason=LLMCallReason.SCHEDULE_PARSE,
                    reference_time=self.services.clock.now().astimezone(
                        self.router.timezone
                    ),
                )
            except (InvalidModelOutput, LLMUnavailable, PermissionDenied):
                return AssistantReply(
                    "日程解析失败，请补充明确的日期、时间和事项。",
                    "llm_unavailable",
                    "deterministic",
                )
            if proposed.action is not intent.action:
                return AssistantReply(
                    "模型返回的日程类型与命令不一致。",
                    "invalid_intent",
                    "llm",
                )
            parsed_arguments = dict(proposed.arguments)
            if private:
                parsed_arguments["private"] = True
            intent = ParsedIntent(
                proposed.action,
                parsed_arguments,
                source="llm",
                requires_confirmation=True,
            )
        if intent is None:
            matches = (
                []
                if "public_group_guest" in context.roles
                else self.services.query_bus().execute(
                    SearchNotes(text, limit=3),
                    context,
                )
            )
            if matches:
                return self._notes_reply(matches, source="fts")
            if self.classifier is None:
                return AssistantReply(
                    "我没有识别出固定命令；当前未启用模型问答。",
                    "unrecognized",
                    "deterministic",
                )
            try:
                intent = self.classifier.classify(
                    context.actor_user_id,
                    text,
                    reason=LLMCallReason.DETERMINISTIC_PARSER_MISS,
                    reference_time=self.services.clock.now().astimezone(
                        self.router.timezone
                    ),
                )
            except (LLMUnavailable, PermissionDenied):
                return AssistantReply(
                    "模型暂时不可用，但日程、待办、备忘和提醒命令仍可使用。",
                    "llm_unavailable",
                    "deterministic",
                )
        if intent.action is IntentAction.DELETE_RESOURCE:
            return self._execute(intent, context, target_ref=target_ref)
        if intent.requires_confirmation:
            return self._stage_plan(intent, context, target_ref=target_ref)
        return self._execute(intent, context, target_ref=target_ref)

    def _execute(
        self,
        intent: ParsedIntent,
        context: CommandContext,
        *,
        target_ref: str,
    ) -> AssistantReply:
        if intent.action in {
            IntentAction.CONFIRM_PLAN,
            IntentAction.REJECT_PLAN,
            IntentAction.CANCEL_PLAN,
        }:
            plan_id = str(intent.arguments.get("plan_id") or "")
            if self.pending_plans is None or not target_ref:
                return AssistantReply(
                    "计划确认功能当前不可用。",
                    "plan_unavailable",
                    "deterministic",
                )
            now = self.services.clock.now()
            stored = self.pending_plans.get(
                plan_id,
                actor_user_id=context.actor_user_id,
                target_ref=target_ref,
                now=now,
            )
            if stored is None:
                return AssistantReply(
                    "该计划不存在、已过期，或不属于你和当前会话。",
                    "plan_not_found",
                    "deterministic",
                )
            if intent.action is IntentAction.REJECT_PLAN:
                self.pending_plans.reject(plan_id, now=now)
                continuation = (
                    "群聊中请再次 @机器人并描述修改；私聊可直接描述。"
                    if "internal_group_member" in context.roles
                    else "请直接描述怎么修改。"
                )
                return AssistantReply(
                    "已进入修改模式。"
                    + continuation
                    + "例如：改成早上9点，文案改为……",
                    "plan_revision_requested",
                    "deterministic",
                )
            if intent.action is IntentAction.CANCEL_PLAN:
                if not self.pending_plans.consume(plan_id, now=now):
                    return AssistantReply(
                        "该计划已经处理，请不要重复提交。",
                        "plan_already_handled",
                        "deterministic",
                    )
                return AssistantReply(
                    "已取消本次计划并退出连续修改。",
                    "plan_cancelled",
                    "deterministic",
                )
            if not self.pending_plans.consume(plan_id, now=now):
                return AssistantReply(
                    "该计划已经处理，请不要重复提交。",
                    "plan_already_handled",
                    "deterministic",
                )
            try:
                payload = json.loads(stored.payload_json)
                arguments = _decode_plan_value(payload)
                action = IntentAction(stored.action)
            except (ValueError, TypeError, json.JSONDecodeError):
                return AssistantReply(
                    "计划内容校验失败，未执行写入。",
                    "invalid_plan",
                    "deterministic",
                )
            if not isinstance(arguments, dict):
                return AssistantReply(
                    "计划内容校验失败，未执行写入。",
                    "invalid_plan",
                    "deterministic",
                )
            return self._execute(
                ParsedIntent(action, arguments, source="confirmed"),
                replace(context, confirmed=True),
                target_ref=target_ref,
            )
        if intent.action is IntentAction.DELETE_RESOURCE:
            return AssistantReply(
                "模型不能直接执行删除；请使用明确的资源命令并再次确认。",
                "dangerous_action_blocked",
                intent.source,
            )
        if intent.requires_confirmation and not context.confirmed:
            return AssistantReply(
                "这是模型建议的写入或删除操作，需要明确确认后才能执行。",
                "confirmation_required",
                intent.source,
            )
        arguments = intent.arguments
        if intent.action is IntentAction.HELP:
            if "public_group_guest" in context.roles:
                return AssistantReply(
                    _PUBLIC_GROUP_HELP_TEXT,
                    "ok",
                    intent.source,
                    rich_text=True,
                )
            if "internal_group_member" in context.roles:
                return AssistantReply(
                    _INTERNAL_GROUP_HELP_TEXT,
                    "ok",
                    intent.source,
                    buttons=_HELP_BUTTONS,
                    rich_text=True,
                )
            if "project_admin" in context.roles:
                return AssistantReply(
                    _PROJECT_ADMIN_HELP_TEXT,
                    "ok",
                    intent.source,
                    buttons=_HELP_BUTTONS,
                    rich_text=True,
                )
            return AssistantReply(
                _HELP_TEXT,
                "ok",
                intent.source,
                buttons=_HELP_BUTTONS,
                rich_text=True,
            )
        if intent.action is IntentAction.LIST_AGENDA:
            local_now = self.services.clock.now().astimezone(self.router.timezone)
            start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            items = self.services.query_bus().execute(
                AgendaBetween(start, start + timedelta(days=1)),
                context,
            )
            reminders = [
                reminder
                for reminder in self.services.query_bus().execute(
                    ListReminders(),
                    context,
                )
                if start <= reminder.fire_at.astimezone(self.router.timezone) < start
                + timedelta(days=1)
            ]
            entries: list[tuple[datetime, str]] = [
                (
                    item.start_at,
                    (
                        f"- `{item.start_at.astimezone(self.router.timezone):%H:%M}"
                        f"–{item.end_at.astimezone(self.router.timezone):%H:%M}` "
                        f"📅 {_escape_markdown_text(item.title)}"
                    ),
                )
                for item in items
            ]
            entries.extend(
                (
                    reminder.fire_at,
                    (
                        f"- `{reminder.fire_at.astimezone(self.router.timezone):%H:%M}` "
                        f"⏰ {_escape_markdown_text(reminder.title)}"
                        + (" · 已触发" if reminder.status.value == "fired" else "")
                    ),
                )
                for reminder in reminders
            )
            entries.sort(key=lambda entry: entry[0])
            if not entries:
                return AssistantReply(
                    "# 今日日程\n\n> 今天没有日程或提醒。",
                    "ok",
                    intent.source,
                    rich_text=True,
                )
            return AssistantReply(
                "# 今日日程\n\n" + "\n".join(line for _when, line in entries),
                "ok",
                intent.source,
                rich_text=True,
            )
        if intent.action is IntentAction.LIST_ALL_AGENDA:
            now = self.services.clock.now()
            readable_items = self.services.query_bus().execute(ListAgendaItems(), context)
            next_occurrence: dict[str, datetime] = {}
            for owner_user_id in {item.owner_user_id for item in readable_items}:
                for occurrence in self.services.agenda.occurrences(
                    owner_user_id,
                    now,
                    now + timedelta(days=366 * 2),
                ):
                    next_occurrence.setdefault(
                        occurrence.agenda_item_id,
                        occurrence.start_at,
                    )
            agenda_items = sorted(
                (
                    item
                    for item in readable_items
                    if item.id in next_occurrence
                ),
                key=lambda item: (next_occurrence[item.id], item.id),
            )
            reminders = [
                reminder
                for reminder in self.services.query_bus().execute(
                    ListReminders(include_inactive=False),
                    context,
                )
                if reminder.status.value == "pending" and reminder.fire_at >= now
            ]
            if not agenda_items and not reminders:
                return AssistantReply(
                    "# 所有未来安排\n\n> 没有未来日程或待触发提醒。",
                    "ok",
                    intent.source,
                    rich_text=True,
                )
            lines = ["# 所有未来安排"]
            buttons: list[MessageButton] = []
            if agenda_items:
                lines.extend(["", "## 日程"])
            for item in agenda_items:
                recurrence = item.recurrence.value if item.recurrence is not None else "单次"
                local_next = next_occurrence[item.id].astimezone(self.router.timezone)
                lines.append(
                    f"- `{item.id}` · {_escape_markdown_text(item.title)} · "
                    f"`{local_next:%Y-%m-%d %H:%M}` · "
                    f"`{_escape_markdown_text(recurrence)}`"
                )
                if len(buttons) < 10:
                    buttons.append(
                        MessageButton(
                            f"取消·{item.title[:12]}",
                            f"/取消日程 {item.id}",
                        )
                    )
            if reminders:
                lines.extend(["", "## 提醒"])
            for reminder in reminders:
                lines.append(
                    f"- `{reminder.id}` · {_escape_markdown_text(reminder.title)} · "
                    f"`{reminder.fire_at.astimezone(self.router.timezone):%Y-%m-%d %H:%M}`"
                )
                if len(buttons) < 20:
                    buttons.append(
                        MessageButton(
                            f"取消·{reminder.title[:12]}",
                            f"/请求取消提醒 {reminder.id}",
                        )
                    )
            return AssistantReply(
                "\n".join(lines),
                "ok",
                intent.source,
                buttons=tuple(buttons),
                rich_text=True,
            )
        if intent.action is IntentAction.VIEW_CALENDAR:
            return self._calendar_reply(arguments, context, source=intent.source)
        if intent.action is IntentAction.CREATE_AGENDA:
            title = str(arguments.get("title") or "").strip()
            start_at = arguments.get("start_at")
            end_at = arguments.get("end_at")
            recurrence_rule = str(arguments.get("recurrence_rule") or "").strip()
            notifications = arguments.get("notifications") or []
            links = arguments.get("links") or []
            if (
                not title
                or not isinstance(start_at, datetime)
                or not isinstance(end_at, datetime)
                or not recurrence_rule
            ):
                return AssistantReply(
                    "循环日程缺少事项、起止时间或重复规则。",
                    "invalid_intent",
                    intent.source,
                )
            if not isinstance(notifications, list) or any(
                not isinstance(value, ModelNotificationProposal)
                for value in notifications
            ):
                return AssistantReply(
                    "循环日程的通知规则无效。",
                    "invalid_intent",
                    intent.source,
                )
            if not isinstance(links, (list, tuple)) or any(
                not isinstance(value, ActionLink) for value in links
            ):
                return AssistantReply(
                    "循环日程的操作链接无效。",
                    "invalid_intent",
                    intent.source,
                )
            if notifications and not target_ref:
                return AssistantReply(
                    "循环日程通知缺少当前会话目标。",
                    "invalid_intent",
                    intent.source,
                )
            item = self.services.command_bus().execute(
                CreateAgenda(
                    title=title,
                    start_at=start_at,
                    end_at=end_at,
                    timezone=self.router.timezone.key,
                    recurrence_rule=recurrence_rule,
                    action_links=tuple(links),
                    all_day=(
                        start_at.timetz().replace(tzinfo=None) == time.min
                        and end_at.timetz().replace(tzinfo=None) == time.min
                        and end_at - start_at >= timedelta(days=1)
                    ),
                    private=bool(arguments.get("private")),
                ),
                context,
            )
            for notification in notifications:
                self.services.command_bus().execute(
                    CreateAgendaNotification(
                        agenda_item_id=item.id,
                        time_of_day=notification.time_of_day,
                        day_offset=notification.day_offset,
                        text=notification.text,
                        timezone=self.router.timezone.key,
                        target_ref=target_ref,
                        action_links=tuple(links),
                    ),
                    context,
                )
            return AssistantReply(
                f"已创建循环日程：{item.title}"
                + (f"，并配置 {len(notifications)} 条提醒规则" if notifications else ""),
                "created",
                intent.source,
            )
        if intent.action is IntentAction.CREATE_ANNIVERSARY:
            title = str(arguments.get("title") or "").strip()
            anchor_date = arguments.get("anchor_date")
            if not title or not isinstance(anchor_date, date):
                return AssistantReply(
                    "纪念日缺少名称或有效日期。",
                    "invalid_intent",
                    intent.source,
                )
            try:
                kind = ImportantDayKind(str(arguments.get("kind") or "anniversary"))
                calendar = CalendarSystem(str(arguments.get("calendar") or "solar"))
            except ValueError:
                return AssistantReply(
                    "重要日子的类型或历法无法识别。",
                    "invalid_intent",
                    intent.source,
                )
            advance = arguments.get("advance_days")
            try:
                anniversary = self.services.command_bus().execute(
                    CreateAnniversary(
                        title=title,
                        anchor_date=anchor_date,
                        timezone=self.router.timezone.key,
                        kind=kind,
                        calendar=calendar,
                        lunar_month=_optional_int(arguments.get("lunar_month")),
                        lunar_day=_optional_int(arguments.get("lunar_day")),
                        lunar_leap=bool(arguments.get("lunar_leap")),
                        advance_days=(
                            tuple(int(value) for value in advance)
                            if isinstance(advance, (list, tuple))
                            else None
                        ),
                        private=bool(arguments.get("private")),
                    ),
                    context,
                )
            except ValidationError as error:
                return AssistantReply(str(error), "invalid_intent", intent.source)
            return AssistantReply(
                _important_day_created_text(
                    anniversary,
                    self.services.clock.now().astimezone(self.router.timezone).date(),
                ),
                "created",
                intent.source,
            )
        if intent.action is IntentAction.SET_NOTIFICATION_LEADS:
            raw = arguments.get("lead_minutes")
            if not isinstance(raw, (list, tuple)) or not raw:
                return AssistantReply(
                    "提前提醒缺少有效的时间档位。",
                    "invalid_intent",
                    intent.source,
                )
            try:
                applied = self.services.command_bus().execute(
                    SetNotificationLeads(
                        lead_minutes=tuple(int(value) for value in raw),
                        agenda_item_id=(
                            str(arguments["agenda_item_id"])
                            if arguments.get("agenda_item_id")
                            else None
                        ),
                    ),
                    context,
                )
            except (ValidationError, NotFoundError) as error:
                return AssistantReply(str(error), "invalid_intent", intent.source)
            scope = "该日程" if arguments.get("agenda_item_id") else "默认"
            return AssistantReply(
                f"已设置{scope}提前提醒：{_format_lead_minutes(applied)}。",
                "updated",
                intent.source,
            )
        if intent.action is IntentAction.CREATE_DAILY_BRIEFING:
            briefing_time = arguments.get("briefing_time")
            if not target_ref or not isinstance(briefing_time, time):
                return AssistantReply(
                    "每日简报缺少推送时间或当前会话目标。",
                    "invalid_intent",
                    intent.source,
                )
            briefing = self.services.command_bus().execute(
                CreateDailyBriefing(
                    time_of_day=briefing_time,
                    timezone=self.router.timezone.key,
                    target_ref=target_ref,
                    private=bool(arguments.get("private")),
                ),
                context,
            )
            return AssistantReply(
                f"已开启每日简报：每天 {briefing.time_of_day:%H:%M} 推送到当前会话。",
                "created",
                intent.source,
            )
        if intent.action is IntentAction.LIST_ANNIVERSARIES:
            anniversaries = self.services.query_bus().execute(
                ListAnniversaries(), context
            )
            if not anniversaries:
                return AssistantReply("目前没有纪念日。", "ok", intent.source)
            today = self.services.clock.now().astimezone(self.router.timezone).date()
            lines = [
                f"{item.id} · {item.title}：第 {item.day_number(today)} 天"
                for item in anniversaries
            ]
            return AssistantReply("\n".join(lines), "ok", intent.source)
        if intent.action is IntentAction.LIST_DAILY_BRIEFINGS:
            briefings = self.services.query_bus().execute(
                ListDailyBriefings(), context
            )
            if not briefings:
                return AssistantReply("目前没有每日简报。", "ok", intent.source)
            lines = [
                f"{item.id} · 每天 {item.time_of_day:%H:%M} · "
                f"{'启用' if item.enabled else '停用'}"
                for item in briefings
            ]
            return AssistantReply("\n".join(lines), "ok", intent.source)
        if intent.action is IntentAction.LIST_TASKS:
            tasks = self.services.query_bus().execute(ListTasks(), context)
            if not tasks:
                return AssistantReply("目前没有待办。", "ok", intent.source)
            lines = [f"{task.id} [{task.status.value}] {task.title}" for task in tasks]
            return AssistantReply("\n".join(lines), "ok", intent.source)
        if intent.action is IntentAction.LIST_REMINDERS:
            reminders = self.services.query_bus().execute(
                ListReminders(),
                context,
            )
            if not reminders:
                return AssistantReply("目前没有待处理提醒。", "ok", intent.source)
            lines = [
                (
                    f"{reminder.id} [{reminder.status.value}] "
                    f"{reminder.fire_at.astimezone(self.router.timezone):%m-%d %H:%M} "
                    f"{reminder.title}"
                )
                for reminder in reminders
            ]
            return AssistantReply("\n".join(lines), "ok", intent.source)
        if intent.action is IntentAction.SEARCH_NOTES:
            query = str(arguments.get("query") or "").strip()
            if not query:
                return AssistantReply("缺少搜索关键词。", "invalid_intent", intent.source)
            notes = self.services.query_bus().execute(SearchNotes(query), context)
            return self._notes_reply(notes, source=intent.source)
        if intent.action is IntentAction.CREATE_TASK:
            title = str(arguments.get("title") or "").strip()
            if not title:
                return AssistantReply("缺少任务内容。", "invalid_intent", intent.source)
            task = self.services.command_bus().execute(
                CreateTask(title=title, private=bool(arguments.get("private"))),
                context,
            )
            scope = "私人" if task.owner_user_id == context.actor_user_id else "群共享"
            return AssistantReply(
                f"已创建{scope}任务：{task.title}",
                "created",
                intent.source,
            )
        if intent.action is IntentAction.CREATE_NOTE:
            body = str(arguments.get("body") or arguments.get("title") or "").strip()
            title = str(arguments.get("title") or body[:80]).strip()
            if not body:
                return AssistantReply("缺少备忘内容。", "invalid_intent", intent.source)
            note = self.services.command_bus().execute(
                CreateNote(
                    title=title,
                    body=body,
                    private=bool(arguments.get("private")),
                ),
                context,
            )
            scope = "私人" if note.owner_user_id == context.actor_user_id else "群共享"
            return AssistantReply(
                f"已保存{scope}备忘：{note.title}",
                "created",
                intent.source,
            )
        if intent.action is IntentAction.CREATE_REMINDER:
            title = str(arguments.get("title") or "").strip()
            fire_at = arguments.get("fire_at")
            links = arguments.get("links") or []
            if not title or not hasattr(fire_at, "tzinfo") or not target_ref:
                return AssistantReply(
                    "提醒需要明确的时间、内容和已绑定通知目标。",
                    "invalid_intent",
                    intent.source,
                )
            if not isinstance(links, (list, tuple)) or any(
                not isinstance(value, ActionLink) for value in links
            ):
                return AssistantReply(
                    "提醒的操作链接无效。",
                    "invalid_intent",
                    intent.source,
                )
            reminder = self.services.command_bus().execute(
                CreateReminder(
                    title=title,
                    fire_at=fire_at,
                    target_ref=target_ref,
                    action_links=tuple(links),
                    private=bool(arguments.get("private")),
                ),
                context,
            )
            return AssistantReply(
                (
                    "私人提醒已设置："
                    if reminder.owner_user_id == context.actor_user_id
                    else "群共享提醒已设置："
                )
                + f"{reminder.fire_at.isoformat()} {reminder.title}",
                "created",
                intent.source,
            )
        if intent.action is IntentAction.ACKNOWLEDGE_REMINDER:
            reminder_id = str(arguments.get("reminder_id") or "").strip()
            if not reminder_id:
                return AssistantReply("提醒标识无效。", "invalid_intent", intent.source)
            reminder = self.services.command_bus().execute(
                AcknowledgeReminder(reminder_id),
                context,
            )
            return AssistantReply(
                f"已完成提醒：{reminder.title}",
                "updated",
                intent.source,
            )
        if intent.action is IntentAction.CANCEL_REMINDER:
            reminder_id = str(arguments.get("reminder_id") or "").strip()
            if not reminder_id:
                return AssistantReply("提醒标识无效。", "invalid_intent", intent.source)
            reminder = self.services.command_bus().execute(
                CancelReminder(reminder_id),
                context,
            )
            return AssistantReply(
                f"已取消提醒：{reminder.title}",
                "updated",
                intent.source,
            )
        if intent.action is IntentAction.CANCEL_AGENDA:
            agenda_id = str(arguments.get("agenda_id") or "").strip()
            item = next(
                (
                    value
                    for value in self.services.query_bus().execute(
                        ListAgendaItems(), context
                    )
                    if value.id == agenda_id
                ),
                None,
            )
            if item is None:
                return AssistantReply("日程不存在。", "not_found", intent.source)
            self.services.command_bus().execute(DeleteAgenda(agenda_id), context)
            return AssistantReply(
                f"已取消该日程的未来安排：{item.title}",
                "updated",
                intent.source,
            )
        if intent.action is IntentAction.SNOOZE_REMINDER:
            reminder_id = str(arguments.get("reminder_id") or "").strip()
            fire_at = arguments.get("fire_at")
            if not reminder_id or not hasattr(fire_at, "tzinfo"):
                return AssistantReply("稍后提醒参数无效。", "invalid_intent", intent.source)
            reminder = self.services.command_bus().execute(
                SnoozeReminder(reminder_id, fire_at),
                context,
            )
            return AssistantReply(
                f"已稍后提醒：{reminder.fire_at.isoformat()}",
                "updated",
                intent.source,
            )
        if intent.action is IntentAction.COMPLETE_TASK:
            task_id = str(arguments.get("task_id") or "").strip()
            task = next(
                (
                    item
                    for item in self.services.query_bus().execute(
                        ListTasks(include_archived=True),
                        context,
                    )
                    if item.id == task_id
                ),
                None,
            )
            if task is None:
                return AssistantReply("任务不存在。", "not_found", intent.source)
            completed = self.services.command_bus().execute(
                TransitionTask(
                    task_id=task.id,
                    expected_version=task.version,
                    status=TaskStatus.COMPLETED,
                ),
                context,
            )
            return AssistantReply(f"已完成任务：{completed.title}", "updated", intent.source)
        if intent.action is IntentAction.POSTPONE_TASK:
            task_id = str(arguments.get("task_id") or "").strip()
            due_at = arguments.get("due_at")
            task = next(
                (
                    item
                    for item in self.services.query_bus().execute(
                        ListTasks(include_archived=True),
                        context,
                    )
                    if item.id == task_id
                ),
                None,
            )
            if (
                task is None
                or not hasattr(due_at, "tzinfo")
            ):
                return AssistantReply("任务或延期时间无效。", "not_found", intent.source)
            postponed = self.services.command_bus().execute(
                PostponeTask(
                    task_id=task.id,
                    expected_version=task.version,
                    due_at=due_at,
                ),
                context,
            )
            return AssistantReply(
                f"任务已延期至：{postponed.due_at.isoformat()}",
                "updated",
                intent.source,
            )
        if intent.action is IntentAction.SUMMARIZE_NOTES:
            query = str(arguments.get("query") or "").strip()
            notes = self.services.query_bus().execute(SearchNotes(query, limit=10), context)
            if not notes:
                return AssistantReply("没有找到可总结的备忘。", "not_found", "fts")
            if self.llm_gateway is None or not self.llm_model:
                return self._notes_reply(notes, source="fts")
            classification = max(note.classification for note in notes)
            prompt = json.dumps(
                [{"title": note.title, "body": note.body} for note in notes],
                ensure_ascii=False,
            )
            try:
                response = self.llm_gateway.generate(
                    owner_user_id=context.actor_user_id,
                    request=LLMRequest(
                        model=self.llm_model,
                        system_prompt="Summarize only the supplied notes. Return JSON.",
                        user_prompt=prompt,
                        response_schema=_SummaryEnvelope.model_json_schema(),
                    ),
                    classification=classification,
                    reason=LLMCallReason.NOTE_SUMMARY_REQUESTED,
                )
                summary = _SummaryEnvelope.model_validate_json(response.content)
            except (LLMUnavailable, PermissionDenied):
                return self._notes_reply(notes, source="fts")
            except ValueError as exc:
                raise InvalidModelOutput("summary output did not match schema") from exc
            return AssistantReply(summary.summary, "ok", "llm")
        if intent.action is IntentAction.ANSWER:
            answer = str(arguments.get("answer") or "").strip()
            query = str(arguments.get("query") or "").strip()
            if answer:
                return AssistantReply(answer, "ok", intent.source)
            if query:
                notes = (
                    []
                    if "public_group_guest" in context.roles
                    else self.services.query_bus().execute(
                        SearchNotes(query, limit=3),
                        context,
                    )
                )
                if notes:
                    return self._notes_reply(notes, source="fts")
                if (
                    bool(arguments.get("web_search"))
                    and self.llm_gateway is not None
                    and self.llm_model
                ):
                    if not web_query_is_safe(query):
                        return AssistantReply(
                            "联网问题疑似包含隐私或密钥，已阻止外发。请删除具体值后重新提问。",
                            "sensitive_egress_blocked",
                            "deterministic",
                        )
                    plan = self._plan_question(query, context)
                    if plan is not None:
                        if plan.capability == "runtime_datetime":
                            return self._runtime_datetime_reply()
                        if plan.capability == "zhixu_data":
                            return self._planned_data_reply(
                                query,
                                context,
                                target_ref=target_ref,
                            )
                        if plan.capability == "model_knowledge" and plan.answer:
                            return AssistantReply(plan.answer, "ok", "llm")
                        if plan.capability == "web_search":
                            if not self.web_search_enabled:
                                return AssistantReply(
                                    "这个问题需要查询公开网络，但当前未启用联网搜索。",
                                    "web_search_unavailable",
                                    "deterministic",
                                )
                            web_query = (plan.search_query or query).strip()
                            if not web_query_is_safe(web_query):
                                return AssistantReply(
                                    "模型生成的搜索词疑似包含隐私或密钥，已阻止外发。",
                                    "sensitive_egress_blocked",
                                    "deterministic",
                                )
                            web_answer = self._search_web(web_query, context)
                            if web_answer is not None:
                                return self._web_answer_reply(web_answer)
                            return AssistantReply(
                                "公开网络搜索暂时不可用，请稍后再试。",
                                "web_search_unavailable",
                                "deterministic",
                            )
                if self.classifier is not None:
                    try:
                        proposed = self.classifier.classify(
                            context.actor_user_id,
                            query,
                            reason=LLMCallReason.GENERAL_QUESTION,
                        )
                    except (LLMUnavailable, PermissionDenied):
                        proposed = None
                    if proposed is not None and proposed.action is IntentAction.ANSWER:
                        model_answer = str(proposed.arguments.get("answer") or "").strip()
                        if model_answer:
                            return AssistantReply(model_answer, "ok", "llm")
            return AssistantReply("没有找到确定性答案。", "not_found", intent.source)
        raise ValidationError("intent action is not executable")

    def _stage_plan(
        self,
        intent: ParsedIntent,
        context: CommandContext,
        *,
        target_ref: str,
        notice: str = "",
    ) -> AssistantReply:
        if self.pending_plans is None or not target_ref:
            return AssistantReply(
                "解析已完成，但当前会话不支持计划确认，未写入任何数据。",
                "confirmation_unavailable",
                intent.source,
            )
        encoded = _encode_plan_value(intent.arguments)
        payload_json = json.dumps(
            encoded,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(payload_json.encode("utf-8")) > 16_384:
            return AssistantReply(
                "计划内容过长，未进入确认队列。",
                "plan_too_large",
                "deterministic",
            )
        plan = self.pending_plans.put(
            actor_user_id=context.actor_user_id,
            target_ref=target_ref,
            action=intent.action.value,
            payload_json=payload_json,
            now=self.services.clock.now(),
        )
        scope = "当前内部群共享库" if "internal_group_member" in context.roles else "私人库"
        lines = ["# 请确认计划"]
        if notice:
            lines.extend(["", f"> {_escape_markdown_text(notice)}"])
        lines.extend(["", f"**写入范围：** {scope}"])
        arguments = intent.arguments
        if intent.action is IntentAction.CREATE_AGENDA:
            recurrence = str(arguments.get("recurrence_rule") or "")
            recurrence_text = (
                "每月倒数第二个香港工作日"
                if recurrence
                == "X-BUSINESS-DAY;CALENDAR=HK_GENERAL_HOLIDAYS;BYSETPOS=-2"
                else recurrence
            )
            lines.append(
                f"**事件：** {_escape_markdown_text(str(arguments.get('title') or ''))}"
            )
            if recurrence_text == "每月倒数第二个香港工作日":
                local_today = self.services.clock.now().astimezone(
                    self.router.timezone
                ).date()
                try:
                    first_date = monthly_business_day(
                        local_today.year,
                        local_today.month,
                        -2,
                    )
                    if first_date < local_today:
                        next_year = local_today.year + int(local_today.month == 12)
                        next_month = 1 if local_today.month == 12 else local_today.month + 1
                        first_date = monthly_business_day(next_year, next_month, -2)
                    lines.append(f"**首次执行：** `{first_date:%Y-%m-%d}`")
                except ValidationError:
                    pass
            else:
                lines.append(f"**开始：** `{arguments.get('start_at')}`")
            lines.append(f"**重复：** `{_escape_markdown_text(recurrence_text)}`")
            notifications = arguments.get("notifications") or []
            links = arguments.get("links") or []
            if arguments.get("include_in_daily_briefing"):
                lines.append("**每日早报：** 自动纳入（不会额外创建一条早报提醒）")
            if notifications:
                lines.append(
                    "**通知形式：** 提醒卡片（支持延后 5/15/30/60 分钟、完成、取消）"
                )
            for index, notification in enumerate(notifications, start=1):
                if isinstance(notification, ModelNotificationProposal):
                    relation = (
                        "事件当天"
                        if notification.day_offset == 0
                        else f"事件前 {abs(notification.day_offset)} 天"
                        if notification.day_offset < 0
                        else f"事件后 {notification.day_offset} 天"
                    )
                    lines.append(
                        f"**通知 {index}：** {relation} {notification.time_of_day:%H:%M} · "
                        f"{_escape_markdown_text(notification.text)}"
                    )
            for index, link in enumerate(links, start=1):
                if isinstance(link, ActionLink):
                    lines.append(
                        f"**操作入口 {index}：** {_escape_markdown_text(link.label)}"
                    )
        elif intent.action is IntentAction.CREATE_ANNIVERSARY:
            lines.extend(
                [
                    f"**纪念日：** {_escape_markdown_text(str(arguments.get('title') or ''))}",
                    f"**起始日期：** `{arguments.get('anchor_date')}`",
                ]
            )
        elif intent.action is IntentAction.CREATE_DAILY_BRIEFING:
            lines.append(f"**每日简报时间：** `{arguments.get('briefing_time')}`")
        elif intent.action is IntentAction.CREATE_REMINDER:
            lines.extend(
                [
                    f"**提醒：** {_escape_markdown_text(str(arguments.get('title') or ''))}",
                    f"**时间：** `{arguments.get('fire_at')}`",
                ]
            )
        elif intent.action is IntentAction.CANCEL_AGENDA:
            agenda_id = str(arguments.get("agenda_id") or "")
            item = next(
                (
                    value
                    for value in self.services.query_bus().execute(
                        ListAgendaItems(), context
                    )
                    if value.id == agenda_id
                ),
                None,
            )
            lines.extend(
                [
                    "**操作：** 取消该日程的所有未来安排",
                    f"**日程：** {_escape_markdown_text(item.title) if item else agenda_id}",
                    f"**资源 ID：** `{agenda_id}`",
                ]
            )
        elif intent.action is IntentAction.CANCEL_REMINDER:
            reminder_id = str(arguments.get("reminder_id") or "")
            reminder = next(
                (
                    value
                    for value in self.services.query_bus().execute(
                        ListReminders(include_inactive=True), context
                    )
                    if value.id == reminder_id
                ),
                None,
            )
            reminder_title = (
                _escape_markdown_text(reminder.title) if reminder else reminder_id
            )
            lines.extend(
                [
                    "**操作：** 取消未来提醒",
                    f"**提醒：** {reminder_title}",
                    f"**资源 ID：** `{reminder_id}`",
                ]
            )
        else:
            lines.append(f"**操作：** `{intent.action.value}`")
        if intent.action is not IntentAction.CREATE_AGENDA:
            for index, link in enumerate(arguments.get("links") or [], start=1):
                if isinstance(link, ActionLink):
                    lines.append(
                        f"**操作入口 {index}：** {_escape_markdown_text(link.label)}"
                    )
        lines.extend(
            [
                "",
                "> 接受后才会写入；修改会保留计划；取消创建会立即退出。",
            ]
        )
        preview_links = tuple(
            MessageButton(link.label, link.url, ButtonActionKind.OPEN_URL)
            for link in (arguments.get("links") or [])
            if isinstance(link, ActionLink)
        )
        return AssistantReply(
            "\n".join(lines),
            "plan_preview",
            intent.source,
            buttons=preview_links
            + (
                MessageButton("接受", f"/确认计划 {plan.id}"),
                MessageButton("修改", f"/拒绝计划 {plan.id}"),
                MessageButton("取消创建", f"/取消计划 {plan.id}"),
            ),
            rich_text=True,
        )

    def _plan_question(
        self,
        query: str,
        context: CommandContext,
    ) -> _QuestionPlanEnvelope | None:
        if self.llm_gateway is None:
            return None
        current = self.services.clock.now().astimezone(self.router.timezone)
        try:
            response = self.llm_gateway.generate(
                owner_user_id=context.actor_user_id,
                request=LLMRequest(
                    model=self.llm_model,
                    system_prompt=(
                        "你是信息来源规划器，只能从以下能力中选择一个："
                        "runtime_datetime 表示仅凭可信的当前日期、时间或时区即可回答；"
                        "zhixu_data 表示需要查询用户有权读取的日程、提醒、待办或备忘；"
                        "model_knowledge 表示稳定常识、计算或推理，不需要实时外部资料；"
                        "web_search 表示新闻、天气、价格、人物现职等可能变化的公开事实，"
                        "或用户明确要求搜索和来源。"
                        "凡是只需直接读取可信运行时字段即可回答的问题，都必须选择 "
                        "runtime_datetime，不得选择 web_search，也不得自行计算答案。"
                        "model_knowledge 必须给出 answer；web_search 可给出不含私人信息的 "
                        "search_query；runtime_datetime 不要生成答案。"
                        f" 可信运行时：datetime={current.isoformat()}; "
                        f"timezone={self.router.timezone.key}. 只返回符合 schema 的 JSON。"
                    ),
                    user_prompt=query,
                    response_schema=_QuestionPlanEnvelope.model_json_schema(),
                ),
                classification=DataClassification.PERSONAL,
                reason=LLMCallReason.GENERAL_QUESTION,
            )
            plan = _QuestionPlanEnvelope.model_validate_json(response.content)
        except (LLMUnavailable, PermissionDenied, ValueError):
            return None
        if plan.capability == "model_knowledge" and not plan.answer:
            return None
        return plan

    def _planned_data_reply(
        self,
        query: str,
        context: CommandContext,
        *,
        target_ref: str,
    ) -> AssistantReply:
        if "public_group_guest" in context.roles:
            return AssistantReply(
                "公开群不能查询个人或内部群数据。",
                "permission_denied",
                "deterministic",
            )
        if self.classifier is None:
            return AssistantReply(
                "知序数据查询规划暂时不可用，请使用明确命令。",
                "llm_unavailable",
                "deterministic",
            )
        try:
            intent = self.classifier.classify(
                context.actor_user_id,
                query,
                reason=LLMCallReason.GENERAL_QUESTION,
                reference_time=self.services.clock.now().astimezone(
                    self.router.timezone
                ),
            )
        except (InvalidModelOutput, LLMUnavailable, PermissionDenied):
            return AssistantReply(
                "没有识别出可执行的数据查询，请换一种说法或使用明确命令。",
                "invalid_intent",
                "deterministic",
            )
        allowed = {
            IntentAction.LIST_AGENDA,
            IntentAction.VIEW_CALENDAR,
            IntentAction.LIST_TASKS,
            IntentAction.LIST_REMINDERS,
            IntentAction.LIST_ANNIVERSARIES,
            IntentAction.LIST_DAILY_BRIEFINGS,
            IntentAction.SEARCH_NOTES,
        }
        if intent.action not in allowed:
            return AssistantReply(
                "问答规划只允许读取现有数据；写入请使用明确命令。",
                "dangerous_action_blocked",
                "deterministic",
            )
        return self._execute(intent, context, target_ref=target_ref)

    def _runtime_datetime_reply(self) -> AssistantReply:
        current = self.services.clock.now().astimezone(self.router.timezone)
        weekdays = "一二三四五六日"
        return AssistantReply(
            (
                f"现在是 {current:%Y年%m月%d日 %H:%M}，"
                f"星期{weekdays[current.weekday()]}"
                f"（{self.router.timezone.key}）。"
            ),
            "ok",
            "runtime",
        )

    def _search_web(
        self,
        query: str,
        context: CommandContext,
    ) -> _WebAnswerEnvelope | None:
        if self.llm_gateway is None:
            return None
        current = self.services.clock.now().astimezone(self.router.timezone)
        try:
            response = self.llm_gateway.generate(
                owner_user_id=context.actor_user_id,
                request=LLMRequest(
                    model=self.llm_model,
                    system_prompt=(
                        "使用 web_search 搜索公开网页，再用中文简洁回答。"
                        "区分事实与不确定信息，不得声称访问过未搜索的来源。"
                        "不要在正文末尾自行编造来源列表。"
                        f"可信当前时间为 {current.isoformat()}，"
                        f"时区为 {self.router.timezone.key}。"
                    ),
                    user_prompt=query,
                    response_schema=_WebAnswerEnvelope.model_json_schema(),
                    web_search=True,
                ),
                classification=DataClassification.PUBLIC,
                reason=LLMCallReason.GENERAL_QUESTION,
            )
            return _WebAnswerEnvelope.model_validate_json(response.content)
        except (LLMUnavailable, PermissionDenied, ValueError):
            return None

    @staticmethod
    def _notes_reply(notes, *, source: str) -> AssistantReply:
        if not notes:
            return AssistantReply("没有找到相关备忘。", "not_found", source)
        lines = [f"{note.title}：{note.body}" for note in notes]
        return AssistantReply("\n".join(lines), "ok", source)

    @staticmethod
    def _web_answer_reply(answer: _WebAnswerEnvelope) -> AssistantReply:
        if not answer.sources:
            return AssistantReply(
                answer.answer + "\n\n> 本次搜索未返回可验证来源",
                "ok",
                "web",
                rich_text=True,
            )
        source_lines = [
            f"{index}. {_escape_markdown_text(source.title)}\n{source.url}"
            for index, source in enumerate(answer.sources, start=1)
        ]
        return AssistantReply(
            answer.answer + "\n\n## 参考来源\n" + "\n".join(source_lines),
            "ok",
            "web",
            rich_text=True,
        )

    def _calendar_reply(
        self,
        arguments: dict,
        context: CommandContext,
        *,
        source: str,
    ) -> AssistantReply:
        local_now = self.services.clock.now().astimezone(self.router.timezone)
        try:
            year = int(arguments.get("year") or local_now.year)
            month = int(arguments.get("month") or local_now.month)
        except (TypeError, ValueError):
            year = month = 0
        if not 1970 <= year <= 2100 or not 1 <= month <= 12:
            return AssistantReply(
                "月份格式无效，请使用 `/日历 2026-08`。",
                "invalid_intent",
                source,
                rich_text=True,
            )
        start = datetime(year, month, 1, tzinfo=self.router.timezone)
        next_month = (
            datetime(year + 1, 1, 1, tzinfo=self.router.timezone)
            if month == 12
            else datetime(year, month + 1, 1, tzinfo=self.router.timezone)
        )
        occurrences = self.services.query_bus().execute(
            AgendaBetween(start, next_month),
            context,
        )
        reminders = [
            reminder
            for reminder in self.services.query_bus().execute(
                ListReminders(),
                context,
            )
            if start <= reminder.fire_at.astimezone(self.router.timezone) < next_month
        ]
        entries: list[tuple[datetime, str]] = [
            (
                occurrence.start_at,
                (
                    f"- `{occurrence.start_at.astimezone(self.router.timezone):%m-%d %H:%M}` "
                    f"📅 {_escape_markdown_text(occurrence.title)}"
                ),
            )
            for occurrence in occurrences
        ]
        entries.extend(
            (
                reminder.fire_at,
                (
                    f"- `{reminder.fire_at.astimezone(self.router.timezone):%m-%d %H:%M}` "
                    f"⏰ {_escape_markdown_text(reminder.title)}"
                    + (" · 已触发" if reminder.status.value == "fired" else "")
                ),
            )
            for reminder in reminders
        )
        entries.sort(key=lambda entry: entry[0])
        busy_day_counts = Counter(
            when.astimezone(self.router.timezone).day for when, _line in entries
        )
        today = local_now.date()
        body = [
            f"# {year} 年 {month} 月",
            "",
            "> 月历已生成图片；蓝色为今天，圆点表示当天有安排，灰色日期来自相邻月份。",
        ]
        if entries:
            body.extend(["", "## 本月安排"])
            body.extend(line for _when, line in entries[:30])
            if len(entries) > 30:
                body.append(f"> 另有 {len(entries) - 30} 项未展开")
        else:
            body.extend(["", "> 本月暂无日程或提醒。"])
        previous = start - timedelta(days=1)
        current_month = local_now.strftime("%Y-%m")
        return AssistantReply(
            "\n".join(body),
            "ok",
            source,
            buttons=(
                MessageButton("上个月", f"/日历 {previous:%Y-%m}"),
                MessageButton("本月", f"/日历 {current_month}"),
                MessageButton("下个月", f"/日历 {next_month:%Y-%m}"),
                MessageButton("今天", "/今天"),
            ),
            rich_text=True,
            calendar_preview=CalendarPreview(
                year=year,
                month=month,
                busy_day_counts=tuple(sorted(busy_day_counts.items())),
                today_day=(
                    today.day
                    if today.year == year and today.month == month
                    else None
                ),
            ),
        )
