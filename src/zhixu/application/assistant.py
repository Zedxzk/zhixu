"""Deterministic assistant workflow with optional model fallback."""

from __future__ import annotations

import json
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from zhixu.domain import CommandContext, TaskStatus
from zhixu.domain.errors import InvalidModelOutput, LLMUnavailable, ValidationError
from zhixu.ports import LLMRequest

from .commands import (
    CreateNote,
    CreateReminder,
    CreateTask,
    PostponeTask,
    TransitionTask,
)
from .intent_router import ModelIntentClassifier, RuleIntentRouter
from .intents import AssistantReply, IntentAction, ParsedIntent
from .llm import LLMGateway
from .queries import AgendaBetween, ListTasks, SearchNotes
from .services import ZhixuServices


class _SummaryEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1, max_length=4000)


class AssistantEngine:
    def __init__(
        self,
        *,
        services: ZhixuServices,
        router: RuleIntentRouter,
        classifier: ModelIntentClassifier | None = None,
        llm_gateway: LLMGateway | None = None,
        llm_model: str = "",
    ) -> None:
        self.services = services
        self.router = router
        self.classifier = classifier
        self.llm_gateway = llm_gateway
        self.llm_model = llm_model

    def handle(
        self,
        text: str,
        context: CommandContext,
        *,
        target_ref: str = "",
    ) -> AssistantReply:
        intent = self.router.route(text)
        if intent is None:
            matches = self.services.query_bus().execute(
                SearchNotes(text, limit=3),
                context,
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
                intent = self.classifier.classify(context.actor_user_id, text)
            except LLMUnavailable:
                return AssistantReply(
                    "模型暂时不可用，但日程、待办、备忘和提醒命令仍可使用。",
                    "llm_unavailable",
                    "deterministic",
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
        if intent.action is IntentAction.LIST_AGENDA:
            local_now = self.services.clock.now().astimezone(self.router.timezone)
            start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            items = self.services.query_bus().execute(
                AgendaBetween(start, start + timedelta(days=1)),
                context,
            )
            if not items:
                return AssistantReply("今天没有日程。", "ok", intent.source)
            lines = [
                f"{item.start_at.astimezone(self.router.timezone):%H:%M} {item.title}"
                for item in items
            ]
            return AssistantReply("\n".join(lines), "ok", intent.source)
        if intent.action is IntentAction.LIST_TASKS:
            tasks = self.services.query_bus().execute(ListTasks(), context)
            if not tasks:
                return AssistantReply("目前没有待办。", "ok", intent.source)
            lines = [f"{task.id} [{task.status.value}] {task.title}" for task in tasks]
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
            task = self.services.command_bus().execute(CreateTask(title=title), context)
            return AssistantReply(f"已创建任务：{task.title}", "created", intent.source)
        if intent.action is IntentAction.CREATE_NOTE:
            body = str(arguments.get("body") or arguments.get("title") or "").strip()
            title = str(arguments.get("title") or body[:80]).strip()
            if not body:
                return AssistantReply("缺少备忘内容。", "invalid_intent", intent.source)
            note = self.services.command_bus().execute(
                CreateNote(title=title, body=body),
                context,
            )
            return AssistantReply(f"已保存备忘：{note.title}", "created", intent.source)
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
                ),
                context,
            )
            return AssistantReply(
                f"提醒已设置：{reminder.fire_at.isoformat()} {reminder.title}",
                "created",
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
                )
                summary = _SummaryEnvelope.model_validate_json(response.content)
            except LLMUnavailable:
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
                notes = self.services.query_bus().execute(SearchNotes(query, limit=3), context)
                if notes:
                    return self._notes_reply(notes, source="fts")
                if self.classifier is not None:
                    try:
                        proposed = self.classifier.classify(
                            context.actor_user_id,
                            query,
                        )
                    except LLMUnavailable:
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

    @staticmethod
    def _notes_reply(notes, *, source: str) -> AssistantReply:
        if not notes:
            return AssistantReply("没有找到相关备忘。", "not_found", source)
        lines = [f"{note.title}：{note.body}" for note in notes]
        return AssistantReply("\n".join(lines), "ok", source)
