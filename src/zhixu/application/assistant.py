"""Deterministic assistant workflow with optional model fallback."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from zhixu.channels import CalendarPreview, MessageButton
from zhixu.domain import CommandContext, DataClassification, TaskStatus
from zhixu.domain.errors import (
    InvalidModelOutput,
    LLMUnavailable,
    PermissionDenied,
    ValidationError,
)
from zhixu.ports import LLMCallReason, LLMRequest
from zhixu.security import web_query_is_safe

from .commands import (
    AcknowledgeReminder,
    CancelReminder,
    CreateNote,
    CreateReminder,
    CreateTask,
    PostponeTask,
    SnoozeReminder,
    TransitionTask,
)
from .intent_router import ModelIntentClassifier, RuleIntentRouter
from .intents import AssistantReply, IntentAction, ParsedIntent
from .llm import LLMGateway
from .queries import AgendaBetween, ListReminders, ListTasks, SearchNotes
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


_HELP_TEXT = """# 知序 · 帮助

> 日程、提醒、待办、备忘与联网问答

- `/帮助`、`/help` 或 `/菜单` — 显示本卡片

## 日程与提醒
- `/今天` 或 `/日程` — 按时间查看今日日程与提醒
- `/日历` — 本月图片日历预览
- `/日历 2026-08` — 查看指定月份
- `/提醒` — 查看待处理提醒及其 ID
- `明天上午9点提醒我提交报告` — 创建日程提醒
- `15分钟后提醒我关烤箱`、`稍后提醒我检查下载`
- `/提醒完成 reminder_ID`、`/取消提醒 reminder_ID`
- `/提醒稍后 reminder_ID 15分钟`

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

## 明确写入私人库
- `/私人任务 内容`
- `/私人记 内容`
- `/私人提醒 提醒内容和时间`

## 身份绑定
- `/绑定私聊 绑定码` — 为机器人私聊完成身份绑定

> 私人数据只能在与机器人的私聊中查询。"""


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
    ) -> None:
        self.services = services
        self.router = router
        self.classifier = classifier
        self.llm_gateway = llm_gateway
        self.llm_model = llm_model
        self.web_search_enabled = web_search_enabled

    def handle(
        self,
        text: str,
        context: CommandContext,
        *,
        target_ref: str = "",
    ) -> AssistantReply:
        intent = self.router.route(text)
        if intent is not None and intent.action is IntentAction.CREATE_REMINDER:
            if self.classifier is None:
                return AssistantReply(
                    "提醒的自然语言解析需要模型，但当前模型不可用。",
                    "llm_unavailable",
                    "deterministic",
                )
            try:
                reminder_text = text
                private = bool(intent.arguments.get("private"))
                if private and reminder_text.strip().startswith("/私人提醒 "):
                    reminder_text = reminder_text.strip().removeprefix("/私人提醒 ")
                proposed = self.classifier.classify(
                    context.actor_user_id,
                    reminder_text,
                    reason=LLMCallReason.SCHEDULE_PARSE,
                    reference_time=self.services.clock.now().astimezone(
                        self.router.timezone
                    ),
                )
            except (InvalidModelOutput, LLMUnavailable, PermissionDenied):
                return AssistantReply(
                    "提醒解析失败，请补充明确的日期、时间和事项。",
                    "llm_unavailable",
                    "deterministic",
                )
            if proposed.action is not IntentAction.CREATE_REMINDER:
                return AssistantReply(
                    "没有识别到明确的提醒事项。",
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
                requires_confirmation=False,
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
            if intent.action is IntentAction.CREATE_REMINDER:
                intent = ParsedIntent(
                    intent.action,
                    dict(intent.arguments),
                    source=intent.source,
                    requires_confirmation=False,
                )
        return self._execute(intent, context, target_ref=target_ref)

    def _execute(
        self,
        intent: ParsedIntent,
        context: CommandContext,
        *,
        target_ref: str,
    ) -> AssistantReply:
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
        if intent.action is IntentAction.VIEW_CALENDAR:
            return self._calendar_reply(arguments, context, source=intent.source)
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
            if not title or not hasattr(fire_at, "tzinfo") or not target_ref:
                return AssistantReply(
                    "提醒需要明确的时间、内容和已绑定通知目标。",
                    "invalid_intent",
                    intent.source,
                )
            reminder = self.services.command_bus().execute(
                CreateReminder(
                    title=title,
                    fire_at=fire_at,
                    target_ref=target_ref,
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
        if intent.action is IntentAction.DELETE_RESOURCE:
            return AssistantReply(
                "模型不能直接执行删除；请使用明确的资源命令并再次确认。",
                "dangerous_action_blocked",
                intent.source,
            )
        raise ValidationError("intent action is not executable")

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
            "> 月历已生成图片；蓝色为今天，圆点表示当天有安排。",
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
