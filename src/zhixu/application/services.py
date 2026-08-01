"""Policy-enforced deterministic use cases."""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import replace

from zhixu.domain import (
    DEFAULT_ANNIVERSARY_ADVANCE_DAYS,
    DEFAULT_BIRTHDAY_ADVANCE_DAYS,
    Action,
    AgendaItem,
    AgendaNotificationRule,
    Anniversary,
    CommandContext,
    DailyBriefing,
    DataClassification,
    ImportantDayKind,
    Note,
    PolicyEngine,
    RecurrenceException,
    RecurrenceRule,
    Reminder,
    RequestChannel,
    ResourceRef,
    Task,
)
from zhixu.domain.errors import (
    ConcurrencyConflict,
    ConfirmationRequired,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from zhixu.ports import (
    AgendaNotificationRepositoryPort,
    AgendaRepositoryPort,
    AnniversaryRepositoryPort,
    Clock,
    DailyBriefingRepositoryPort,
    NoteRepositoryPort,
    NotificationLeadRepositoryPort,
    ReminderRepositoryPort,
    TaskRepositoryPort,
)

from .commands import (
    AcknowledgeReminder,
    CancelReminder,
    CommandBus,
    CreateAgenda,
    CreateAgendaNotification,
    CreateAnniversary,
    CreateDailyBriefing,
    CreateNote,
    CreateReminder,
    CreateTask,
    DeleteAgenda,
    DeleteAgendaNotification,
    DeleteAnniversary,
    DeleteDailyBriefing,
    DeleteNote,
    DeleteTask,
    PostponeTask,
    SetAgendaException,
    SetNotificationLeads,
    SnoozeReminder,
    TransitionTask,
    UpdateAgenda,
    UpdateAgendaNotification,
    UpdateAnniversary,
    UpdateDailyBriefing,
    UpdateNote,
    UpdateTask,
)
from .queries import (
    AgendaBetween,
    ListAgendaItems,
    ListAnniversaries,
    ListDailyBriefings,
    ListReminders,
    ListTasks,
    QueryBus,
    SearchNotes,
)

IdFactory = Callable[[str], str]


def random_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _default_advance_days(kind: ImportantDayKind) -> tuple[int, ...]:
    if kind is ImportantDayKind.BIRTHDAY:
        return DEFAULT_BIRTHDAY_ADVANCE_DAYS
    return DEFAULT_ANNIVERSARY_ADVANCE_DAYS


def _important_day_title_key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _important_day_date_key(value: Anniversary | CreateAnniversary) -> tuple[object, ...]:
    if value.calendar.value == "lunar":
        date_parts: tuple[object, ...] = (
            value.lunar_month,
            value.lunar_day,
            value.lunar_leap,
        )
    else:
        date_parts = (value.anchor_date.month, value.anchor_date.day)
    if value.kind is ImportantDayKind.ANNIVERSARY:
        return (*date_parts, value.anchor_date.year)
    return date_parts


class ZhixuServices:
    def __init__(
        self,
        *,
        agenda: AgendaRepositoryPort,
        tasks: TaskRepositoryPort,
        notes: NoteRepositoryPort,
        reminders: ReminderRepositoryPort,
        policy: PolicyEngine,
        clock: Clock,
        id_factory: IdFactory = random_id,
        anniversaries: AnniversaryRepositoryPort | None = None,
        daily_briefings: DailyBriefingRepositoryPort | None = None,
        agenda_notifications: AgendaNotificationRepositoryPort | None = None,
        notification_leads: NotificationLeadRepositoryPort | None = None,
    ) -> None:
        self.agenda = agenda
        self.tasks = tasks
        self.notes = notes
        self.reminders = reminders
        self.policy = policy
        self.clock = clock
        self.id_factory = id_factory
        self.anniversaries = anniversaries
        self.daily_briefings = daily_briefings
        self.agenda_notifications = agenda_notifications
        self.notification_leads = notification_leads

    def _context(self, context: CommandContext) -> CommandContext:
        return replace(context, now=self.clock.now())

    @staticmethod
    def _create_owner(context: CommandContext, *, private: bool) -> str:
        if not private and context.shared_owner_user_id is not None:
            return context.shared_owner_user_id
        return context.actor_user_id

    @staticmethod
    def _read_owners(context: CommandContext) -> tuple[str, ...]:
        if context.request_channel is RequestChannel.GROUP_CHAT:
            return context.readable_shared_owner_user_ids
        return (context.actor_user_id, *context.readable_shared_owner_user_ids)

    @staticmethod
    def _ref(
        kind: str,
        resource_id: str,
        owner_user_id: str,
        classification: DataClassification,
    ) -> ResourceRef:
        return ResourceRef(kind, resource_id, owner_user_id, classification)

    @staticmethod
    def _require_creator_or_manager(
        context: CommandContext,
        *,
        owner_user_id: str,
        creator_user_id: str | None,
    ) -> None:
        if context.actor_user_id == owner_user_id:
            return
        if context.actor_user_id == creator_user_id:
            return
        if context.roles.intersection({"group_owner", "project_admin"}):
            return
        raise PermissionDenied("only the creator or a group manager may cancel shared data")

    def create_agenda(self, command: CreateAgenda, context: CommandContext) -> AgendaItem:
        current = self._context(context)
        item_id = self.id_factory("agenda")
        recurrence = (
            RecurrenceRule(command.recurrence_rule, command.timezone)
            if command.recurrence_rule
            else None
        )
        item = AgendaItem(
            id=item_id,
            owner_user_id=self._create_owner(current, private=command.private),
            creator_user_id=current.actor_user_id,
            title=command.title,
            description=command.description,
            action_links=command.action_links,
            start_at=command.start_at,
            end_at=command.end_at,
            timezone=command.timezone,
            all_day=command.all_day,
            classification=command.classification,
            recurrence=recurrence,
        )
        authorization = self.policy.require(
            current,
            Action.CREATE,
            self._ref("agenda", item.id, item.owner_user_id, item.classification),
        )
        return self.agenda.create(item, authorization)

    def create_anniversary(
        self,
        command: CreateAnniversary,
        context: CommandContext,
    ) -> Anniversary:
        if self.anniversaries is None:
            raise ValidationError("anniversary repository is unavailable")
        current = self._context(context)
        duplicate = self.find_matching_anniversary(command, current)
        if duplicate is not None and not (
            current.confirmed and command.allow_duplicate
        ):
            raise ConfirmationRequired(
                "a matching important day already exists; confirm duplicate creation"
            )
        anniversary = Anniversary(
            id=self.id_factory("anniversary"),
            owner_user_id=self._create_owner(current, private=command.private),
            creator_user_id=current.actor_user_id,
            title=command.title,
            anchor_date=command.anchor_date,
            timezone=command.timezone,
            kind=command.kind,
            calendar=command.calendar,
            lunar_month=command.lunar_month,
            lunar_day=command.lunar_day,
            lunar_leap=command.lunar_leap,
            advance_days=(
                command.advance_days
                if command.advance_days is not None
                else _default_advance_days(command.kind)
            ),
            classification=command.classification,
        )
        authorization = self.policy.require(
            current,
            Action.CREATE,
            self._ref(
                "anniversary",
                anniversary.id,
                anniversary.owner_user_id,
                anniversary.classification,
            ),
        )
        return self.anniversaries.create(anniversary, authorization)

    def find_matching_anniversary(
        self,
        command: CreateAnniversary,
        context: CommandContext,
    ) -> Anniversary | None:
        """Return an exact semantic duplicate in the destination workspace."""

        if self.anniversaries is None:
            return None
        current = self._context(context)
        owner_user_id = self._create_owner(current, private=command.private)
        title_key = _important_day_title_key(command.title)
        date_key = _important_day_date_key(command)
        for item in self.anniversaries.list_for_owner(owner_user_id):
            if (
                item.kind is command.kind
                and item.calendar is command.calendar
                and _important_day_title_key(item.title) == title_key
                and _important_day_date_key(item) == date_key
            ):
                self.policy.require(
                    current,
                    Action.READ,
                    self._ref(
                        "anniversary",
                        item.id,
                        item.owner_user_id,
                        item.classification,
                    ),
                )
                return item
        return None

    def create_agenda_notification(
        self,
        command: CreateAgendaNotification,
        context: CommandContext,
    ) -> AgendaNotificationRule:
        if self.agenda_notifications is None:
            raise ValidationError("agenda notification repository is unavailable")
        item = self.agenda.get(command.agenda_item_id)
        if item is None:
            raise NotFoundError("agenda item not found")
        current = self._context(context)
        rule = AgendaNotificationRule(
            id=self.id_factory("agenda_notification"),
            agenda_item_id=item.id,
            owner_user_id=item.owner_user_id,
            creator_user_id=current.actor_user_id,
            target_ref=command.target_ref,
            time_of_day=command.time_of_day,
            day_offset=command.day_offset,
            text=command.text,
            timezone=command.timezone,
            action_links=command.action_links,
            classification=max(item.classification, command.classification),
        )
        authorization = self.policy.require(
            current,
            Action.CREATE,
            self._ref(
                "agenda_notification",
                rule.id,
                rule.owner_user_id,
                rule.classification,
            ),
        )
        return self.agenda_notifications.create(rule, authorization)

    def set_notification_leads(
        self,
        command: SetNotificationLeads,
        context: CommandContext,
    ) -> tuple[int, ...]:
        if self.notification_leads is None:
            raise ValidationError("notification lead repository is unavailable")
        current = self._context(context)
        owner = self._create_owner(current, private=False)
        if command.agenda_item_id is None:
            self.policy.require(
                current,
                Action.UPDATE,
                self._ref(
                    "notification_lead",
                    owner,
                    owner,
                    DataClassification.PERSONAL,
                ),
            )
            return self.notification_leads.set_default(
                owner,
                command.lead_minutes,
                now=current.now or self.clock.now(),
            )
        if self.agenda is None:
            raise ValidationError("agenda repository is unavailable")
        item = self.agenda.get(command.agenda_item_id)
        if item is None:
            raise NotFoundError("agenda item was not found")
        self.policy.require(
            current,
            Action.UPDATE,
            self._ref("agenda", item.id, item.owner_user_id, item.classification),
        )
        return self.notification_leads.set_override(
            item.id,
            item.owner_user_id,
            command.lead_minutes,
            now=current.now or self.clock.now(),
        )


    def update_anniversary(
        self,
        command: UpdateAnniversary,
        context: CommandContext,
    ) -> Anniversary:
        if self.anniversaries is None:
            raise ValidationError("anniversary repository is unavailable")
        existing = self.anniversaries.get(command.anniversary_id)
        if existing is None:
            raise NotFoundError("important day was not found")
        current = self._context(context)
        calendar = command.calendar or existing.calendar
        # Switching calendar carries the new date with it; keeping the old
        # calendar keeps whichever date fields that calendar uses.
        if command.calendar is not None:
            lunar_month, lunar_day = command.lunar_month, command.lunar_day
            lunar_leap = bool(command.lunar_leap)
        else:
            lunar_month = (
                command.lunar_month
                if command.lunar_month is not None
                else existing.lunar_month
            )
            lunar_day = (
                command.lunar_day
                if command.lunar_day is not None
                else existing.lunar_day
            )
            lunar_leap = (
                command.lunar_leap
                if command.lunar_leap is not None
                else existing.lunar_leap
            )
        updated = replace(
            existing,
            title=command.title or existing.title,
            anchor_date=command.anchor_date or existing.anchor_date,
            kind=command.kind or existing.kind,
            calendar=calendar,
            lunar_month=lunar_month,
            lunar_day=lunar_day,
            lunar_leap=lunar_leap,
            advance_days=(
                command.advance_days
                if command.advance_days is not None
                else existing.advance_days
            ),
            timezone=command.timezone or existing.timezone,
            classification=(
                command.classification
                if command.classification is not None
                else existing.classification
            ),
        )
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref(
                "anniversary",
                updated.id,
                updated.owner_user_id,
                updated.classification,
            ),
        )
        return self.anniversaries.update(updated, authorization)

    def delete_anniversary(
        self,
        command: DeleteAnniversary,
        context: CommandContext,
    ) -> None:
        if self.anniversaries is None:
            raise ValidationError("anniversary repository is unavailable")
        existing = self.anniversaries.get(command.anniversary_id)
        if existing is None:
            raise NotFoundError("important day was not found")
        authorization = self.policy.require(
            self._context(context),
            Action.DELETE,
            self._ref(
                "anniversary",
                existing.id,
                existing.owner_user_id,
                existing.classification,
            ),
        )
        self.anniversaries.delete(existing.id, authorization)

    def update_daily_briefing(
        self,
        command: UpdateDailyBriefing,
        context: CommandContext,
    ) -> DailyBriefing:
        if self.daily_briefings is None:
            raise ValidationError("daily briefing repository is unavailable")
        existing = self.daily_briefings.get(command.briefing_id)
        if existing is None:
            raise NotFoundError("daily briefing was not found")
        current = self._context(context)
        updated = replace(
            existing,
            time_of_day=command.time_of_day or existing.time_of_day,
            target_ref=command.target_ref or existing.target_ref,
            timezone=command.timezone or existing.timezone,
            enabled=(
                command.enabled if command.enabled is not None else existing.enabled
            ),
            classification=(
                command.classification
                if command.classification is not None
                else existing.classification
            ),
        )
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref(
                "daily_briefing",
                updated.id,
                updated.owner_user_id,
                updated.classification,
            ),
        )
        return self.daily_briefings.update(
            updated,
            authorization,
            now=current.now or self.clock.now(),
        )

    def delete_daily_briefing(
        self,
        command: DeleteDailyBriefing,
        context: CommandContext,
    ) -> None:
        if self.daily_briefings is None:
            raise ValidationError("daily briefing repository is unavailable")
        existing = self.daily_briefings.get(command.briefing_id)
        if existing is None:
            raise NotFoundError("daily briefing was not found")
        authorization = self.policy.require(
            self._context(context),
            Action.DELETE,
            self._ref(
                "daily_briefing",
                existing.id,
                existing.owner_user_id,
                existing.classification,
            ),
        )
        self.daily_briefings.delete(existing.id, authorization)

    def update_agenda_notification(
        self,
        command: UpdateAgendaNotification,
        context: CommandContext,
    ) -> AgendaNotificationRule:
        if self.agenda_notifications is None:
            raise ValidationError("agenda notification repository is unavailable")
        existing = self.agenda_notifications.get(command.rule_id)
        if existing is None:
            raise NotFoundError("agenda notification rule was not found")
        current = self._context(context)
        updated = replace(
            existing,
            text=command.text or existing.text,
            time_of_day=command.time_of_day or existing.time_of_day,
            day_offset=(
                command.day_offset
                if command.day_offset is not None
                else existing.day_offset
            ),
            target_ref=command.target_ref or existing.target_ref,
            timezone=command.timezone or existing.timezone,
            enabled=(
                command.enabled if command.enabled is not None else existing.enabled
            ),
            classification=(
                command.classification
                if command.classification is not None
                else existing.classification
            ),
        )
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref(
                "agenda_notification",
                updated.id,
                updated.owner_user_id,
                updated.classification,
            ),
        )
        return self.agenda_notifications.update(updated, authorization)

    def delete_agenda_notification(
        self,
        command: DeleteAgendaNotification,
        context: CommandContext,
    ) -> None:
        if self.agenda_notifications is None:
            raise ValidationError("agenda notification repository is unavailable")
        existing = self.agenda_notifications.get(command.rule_id)
        if existing is None:
            raise NotFoundError("agenda notification rule was not found")
        authorization = self.policy.require(
            self._context(context),
            Action.DELETE,
            self._ref(
                "agenda_notification",
                existing.id,
                existing.owner_user_id,
                existing.classification,
            ),
        )
        self.agenda_notifications.delete(existing.id, authorization)


    def create_daily_briefing(
        self,
        command: CreateDailyBriefing,
        context: CommandContext,
    ) -> DailyBriefing:
        if self.daily_briefings is None:
            raise ValidationError("daily briefing repository is unavailable")
        current = self._context(context)
        briefing = DailyBriefing(
            id=self.id_factory("briefing"),
            owner_user_id=self._create_owner(current, private=command.private),
            creator_user_id=current.actor_user_id,
            target_ref=command.target_ref,
            time_of_day=command.time_of_day,
            timezone=command.timezone,
            classification=command.classification,
        )
        authorization = self.policy.require(
            current,
            Action.CREATE,
            self._ref(
                "daily_briefing",
                briefing.id,
                briefing.owner_user_id,
                briefing.classification,
            ),
        )
        return self.daily_briefings.create(briefing, authorization)

    def update_agenda(self, command: UpdateAgenda, context: CommandContext) -> AgendaItem:
        existing = self.agenda.get(command.item_id)
        if existing is None:
            raise NotFoundError("agenda item not found")
        if existing.version != command.expected_version:
            raise ConcurrencyConflict("agenda item changed")
        current = self._context(context)
        updated = replace(
            existing,
            title=command.title,
            description=command.description,
            action_links=command.action_links,
            start_at=command.start_at,
            end_at=command.end_at,
            timezone=command.timezone,
            all_day=command.all_day,
            classification=command.classification,
            recurrence=(
                RecurrenceRule(command.recurrence_rule, command.timezone)
                if command.recurrence_rule
                else None
            ),
            version=command.expected_version + 1,
        )
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref(
                "agenda",
                updated.id,
                updated.owner_user_id,
                updated.classification,
            ),
        )
        return self.agenda.update(
            updated,
            expected_version=command.expected_version,
            authorization=authorization,
        )

    def delete_agenda(self, command: DeleteAgenda, context: CommandContext) -> None:
        existing = self.agenda.get(command.item_id)
        if existing is None:
            raise NotFoundError("agenda item not found")
        current = self._context(context)
        self._require_creator_or_manager(
            current,
            owner_user_id=existing.owner_user_id,
            creator_user_id=existing.creator_user_id,
        )
        authorization = self.policy.require(
            current,
            Action.DELETE,
            self._ref(
                "agenda",
                existing.id,
                existing.owner_user_id,
                existing.classification,
            ),
        )
        for reminder in self.reminders.list_for_owner(existing.owner_user_id):
            if (
                reminder.related_kind == "agenda"
                and reminder.related_id == existing.id
                and reminder.status.value == "pending"
            ):
                reminder_authorization = self.policy.require(
                    current,
                    Action.UPDATE,
                    self._ref(
                        "reminder",
                        reminder.id,
                        reminder.owner_user_id,
                        reminder.classification,
                    ),
                )
                self.reminders.cancel(
                    reminder.id,
                    expected_version=reminder.version,
                    authorization=reminder_authorization,
                )
        self.agenda.delete(existing.id, authorization)

    def set_agenda_exception(
        self,
        command: SetAgendaException,
        context: CommandContext,
    ) -> None:
        existing = self.agenda.get(command.item_id)
        if existing is None:
            raise NotFoundError("agenda item not found")
        current = self._context(context)
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref(
                "agenda",
                existing.id,
                existing.owner_user_id,
                existing.classification,
            ),
        )
        self.agenda.add_exception(
            existing.id,
            RecurrenceException(
                occurrence_at=command.occurrence_at,
                action=command.action,
                replacement_start=command.replacement_start,
                replacement_end=command.replacement_end,
            ),
            authorization,
        )

    def create_task(self, command: CreateTask, context: CommandContext) -> Task:
        current = self._context(context)
        task = Task(
            id=self.id_factory("task"),
            owner_user_id=self._create_owner(current, private=command.private),
            creator_user_id=current.actor_user_id,
            title=command.title,
            description=command.description,
            priority=command.priority,
            due_at=command.due_at,
            classification=command.classification,
        )
        authorization = self.policy.require(
            current,
            Action.CREATE,
            self._ref("task", task.id, task.owner_user_id, task.classification),
        )
        return self.tasks.create(task, authorization)

    def update_task(self, command: UpdateTask, context: CommandContext) -> Task:
        task = self.tasks.get(command.task_id)
        if task is None:
            raise NotFoundError("task not found")
        if task.version != command.expected_version:
            raise ConcurrencyConflict("task changed")
        current = self._context(context)
        updated = replace(
            task,
            title=command.title,
            description=command.description,
            priority=command.priority,
            due_at=command.due_at,
            classification=command.classification,
            version=command.expected_version + 1,
        )
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref("task", task.id, task.owner_user_id, updated.classification),
        )
        return self.tasks.update(
            updated,
            expected_version=command.expected_version,
            authorization=authorization,
        )

    def delete_task(self, command: DeleteTask, context: CommandContext) -> None:
        task = self.tasks.get(command.task_id)
        if task is None:
            raise NotFoundError("task not found")
        current = self._context(context)
        authorization = self.policy.require(
            current,
            Action.DELETE,
            self._ref("task", task.id, task.owner_user_id, task.classification),
        )
        self.tasks.delete(task.id, authorization)

    def transition_task(self, command: TransitionTask, context: CommandContext) -> Task:
        task = self.tasks.get(command.task_id)
        if task is None:
            raise NotFoundError("task not found")
        if task.version != command.expected_version:
            raise ConcurrencyConflict("task changed")
        current = self._context(context)
        updated = task.transition(command.status, now=self.clock.now())
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref("task", task.id, task.owner_user_id, task.classification),
        )
        return self.tasks.update(
            updated,
            expected_version=command.expected_version,
            authorization=authorization,
        )

    def postpone_task(self, command: PostponeTask, context: CommandContext) -> Task:
        task = self.tasks.get(command.task_id)
        if task is None:
            raise NotFoundError("task not found")
        if task.version != command.expected_version:
            raise ConcurrencyConflict("task changed")
        current = self._context(context)
        updated = task.postpone(command.due_at, now=self.clock.now())
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref("task", task.id, task.owner_user_id, task.classification),
        )
        return self.tasks.update(
            updated,
            expected_version=command.expected_version,
            authorization=authorization,
        )

    def create_note(self, command: CreateNote, context: CommandContext) -> Note:
        current = self._context(context)
        note = Note(
            id=self.id_factory("note"),
            owner_user_id=self._create_owner(current, private=command.private),
            creator_user_id=current.actor_user_id,
            title=command.title,
            body=command.body,
            tags=command.tags,
            attachments=command.attachments,
            classification=command.classification,
        )
        authorization = self.policy.require(
            current,
            Action.CREATE,
            self._ref("note", note.id, note.owner_user_id, note.classification),
        )
        return self.notes.create(note, authorization)

    def update_note(self, command: UpdateNote, context: CommandContext) -> Note:
        note = self.notes.get(command.note_id)
        if note is None:
            raise NotFoundError("note not found")
        if note.version != command.expected_version:
            raise ConcurrencyConflict("note changed")
        current = self._context(context)
        updated = replace(
            note,
            title=command.title,
            body=command.body,
            tags=command.tags,
            attachments=command.attachments,
            classification=command.classification,
            version=command.expected_version + 1,
        )
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref("note", note.id, note.owner_user_id, updated.classification),
        )
        return self.notes.update(
            updated,
            expected_version=command.expected_version,
            authorization=authorization,
        )

    def delete_note(self, command: DeleteNote, context: CommandContext) -> None:
        note = self.notes.get(command.note_id)
        if note is None:
            raise NotFoundError("note not found")
        current = self._context(context)
        authorization = self.policy.require(
            current,
            Action.DELETE,
            self._ref("note", note.id, note.owner_user_id, note.classification),
        )
        self.notes.delete(note.id, authorization)

    def create_reminder(
        self,
        command: CreateReminder,
        context: CommandContext,
    ) -> Reminder:
        current = self._context(context)
        reminder = Reminder(
            id=self.id_factory("reminder"),
            owner_user_id=self._create_owner(current, private=command.private),
            creator_user_id=current.actor_user_id,
            title=command.title,
            fire_at=command.fire_at,
            target_ref=command.target_ref,
            action_links=command.action_links,
            missed_policy=command.missed_policy,
            classification=command.classification,
            related_kind=command.related_kind,
            related_id=command.related_id,
        )
        authorization = self.policy.require(
            current,
            Action.CREATE,
            self._ref(
                "reminder",
                reminder.id,
                reminder.owner_user_id,
                reminder.classification,
            ),
        )
        return self.reminders.create(reminder, authorization)

    def acknowledge_reminder(
        self,
        command: AcknowledgeReminder,
        context: CommandContext,
    ) -> Reminder:
        reminder = self.reminders.get(command.reminder_id)
        if reminder is None:
            raise NotFoundError("reminder not found")
        current = self._context(context)
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref(
                "reminder",
                reminder.id,
                reminder.owner_user_id,
                reminder.classification,
            ),
        )
        return self.reminders.acknowledge(
            reminder.id,
            expected_version=reminder.version,
            authorization=authorization,
        )

    def cancel_reminder(
        self,
        command: CancelReminder,
        context: CommandContext,
    ) -> Reminder:
        reminder = self.reminders.get(command.reminder_id)
        if reminder is None:
            raise NotFoundError("reminder not found")
        current = self._context(context)
        self._require_creator_or_manager(
            current,
            owner_user_id=reminder.owner_user_id,
            creator_user_id=reminder.creator_user_id,
        )
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref(
                "reminder",
                reminder.id,
                reminder.owner_user_id,
                reminder.classification,
            ),
        )
        return self.reminders.cancel(
            reminder.id,
            expected_version=reminder.version,
            authorization=authorization,
        )

    def snooze_reminder(
        self,
        command: SnoozeReminder,
        context: CommandContext,
    ) -> Reminder:
        reminder = self.reminders.get(command.reminder_id)
        if reminder is None:
            raise NotFoundError("reminder not found")
        current = self._context(context)
        authorization = self.policy.require(
            current,
            Action.UPDATE,
            self._ref(
                "reminder",
                reminder.id,
                reminder.owner_user_id,
                reminder.classification,
            ),
        )
        return self.reminders.snooze(
            reminder.id,
            fire_at=command.fire_at,
            expected_version=reminder.version,
            authorization=authorization,
        )

    def agenda_between(
        self,
        query: AgendaBetween,
        context: CommandContext,
    ) -> list:
        current = self._context(context)
        occurrences = []
        for owner_user_id in self._read_owners(current):
            self.policy.require(
                current,
                Action.READ,
                self._ref(
                    "agenda_collection",
                    owner_user_id,
                    owner_user_id,
                    DataClassification.PERSONAL,
                ),
            )
            occurrences.extend(
                self.agenda.occurrences(
                    owner_user_id,
                    query.window_start,
                    query.window_end,
                )
            )
        occurrences.sort(key=lambda item: (item.start_at, item.agenda_item_id))
        checked: set[str] = set()
        for occurrence in occurrences:
            if occurrence.agenda_item_id in checked:
                continue
            item = self.agenda.get(occurrence.agenda_item_id)
            if item is None:
                raise NotFoundError("agenda item not found")
            self.policy.require(
                current,
                Action.READ,
                self._ref("agenda", item.id, item.owner_user_id, item.classification),
            )
            checked.add(item.id)
        return occurrences

    def list_agenda_items(
        self,
        _query: ListAgendaItems,
        context: CommandContext,
    ) -> list[AgendaItem]:
        current = self._context(context)
        values: list[AgendaItem] = []
        for owner_user_id in self._read_owners(current):
            self.policy.require(
                current,
                Action.READ,
                self._ref(
                    "agenda_collection",
                    owner_user_id,
                    owner_user_id,
                    DataClassification.PERSONAL,
                ),
            )
            for item in self.agenda.list_for_owner(owner_user_id):
                self.policy.require(
                    current,
                    Action.READ,
                    self._ref("agenda", item.id, item.owner_user_id, item.classification),
                )
                values.append(item)
        values.sort(key=lambda item: (item.start_at, item.id))
        return values

    def list_tasks(self, query: ListTasks, context: CommandContext) -> list[Task]:
        current = self._context(context)
        tasks: list[Task] = []
        for owner_user_id in self._read_owners(current):
            self.policy.require(
                current,
                Action.READ,
                self._ref(
                    "task_collection",
                    owner_user_id,
                    owner_user_id,
                    DataClassification.PERSONAL,
                ),
            )
            tasks.extend(self.tasks.list_for_owner(owner_user_id))
        for task in tasks:
            self.policy.require(
                current,
                Action.READ,
                self._ref("task", task.id, task.owner_user_id, task.classification),
            )
        return tasks if query.include_archived else [
            task for task in tasks if task.status.value != "archived"
        ]

    def list_reminders(
        self,
        query: ListReminders,
        context: CommandContext,
    ) -> list[Reminder]:
        current = self._context(context)
        reminders: list[Reminder] = []
        for owner_user_id in self._read_owners(current):
            reminders.extend(self.reminders.list_for_owner(owner_user_id))
        for reminder in reminders:
            self.policy.require(
                current,
                Action.READ,
                self._ref(
                    "reminder",
                    reminder.id,
                    reminder.owner_user_id,
                    reminder.classification,
                ),
            )
        if query.include_inactive:
            return reminders
        return [
            reminder
            for reminder in reminders
            if reminder.status.value in {"pending", "fired"}
        ]

    def search_notes(self, query: SearchNotes, context: CommandContext) -> list[Note]:
        current = self._context(context)
        notes: list[Note] = []
        for owner_user_id in self._read_owners(current):
            self.policy.require(
                current,
                Action.READ,
                self._ref(
                    "note_collection",
                    owner_user_id,
                    owner_user_id,
                    DataClassification.PERSONAL,
                ),
            )
            notes.extend(self.notes.search(owner_user_id, query.text, limit=query.limit))
        notes = notes[: query.limit]
        visible: list[Note] = []
        for note in notes:
            self.policy.require(
                current,
                Action.READ,
                self._ref("note", note.id, note.owner_user_id, note.classification),
            )
            visible.append(note)
        return visible

    def list_anniversaries(
        self,
        _query: ListAnniversaries,
        context: CommandContext,
    ) -> list[Anniversary]:
        if self.anniversaries is None:
            return []
        current = self._context(context)
        values: list[Anniversary] = []
        for owner_user_id in self._read_owners(current):
            values.extend(self.anniversaries.list_for_owner(owner_user_id))
        for anniversary in values:
            self.policy.require(
                current,
                Action.READ,
                self._ref(
                    "anniversary",
                    anniversary.id,
                    anniversary.owner_user_id,
                    anniversary.classification,
                ),
            )
        return values

    def list_daily_briefings(
        self,
        _query: ListDailyBriefings,
        context: CommandContext,
    ) -> list[DailyBriefing]:
        if self.daily_briefings is None:
            return []
        current = self._context(context)
        values: list[DailyBriefing] = []
        for owner_user_id in self._read_owners(current):
            values.extend(self.daily_briefings.list_for_owner(owner_user_id))
        for briefing in values:
            self.policy.require(
                current,
                Action.READ,
                self._ref(
                    "daily_briefing",
                    briefing.id,
                    briefing.owner_user_id,
                    briefing.classification,
                ),
            )
        return values

    def command_bus(self) -> CommandBus:
        bus = CommandBus()
        bus.register(CreateAgenda, self.create_agenda)
        bus.register(CreateAgendaNotification, self.create_agenda_notification)
        bus.register(CreateAnniversary, self.create_anniversary)
        bus.register(SetNotificationLeads, self.set_notification_leads)
        bus.register(UpdateAnniversary, self.update_anniversary)
        bus.register(DeleteAnniversary, self.delete_anniversary)
        bus.register(UpdateDailyBriefing, self.update_daily_briefing)
        bus.register(DeleteDailyBriefing, self.delete_daily_briefing)
        bus.register(UpdateAgendaNotification, self.update_agenda_notification)
        bus.register(DeleteAgendaNotification, self.delete_agenda_notification)
        bus.register(CreateDailyBriefing, self.create_daily_briefing)
        bus.register(UpdateAgenda, self.update_agenda)
        bus.register(DeleteAgenda, self.delete_agenda)
        bus.register(SetAgendaException, self.set_agenda_exception)
        bus.register(CreateTask, self.create_task)
        bus.register(UpdateTask, self.update_task)
        bus.register(DeleteTask, self.delete_task)
        bus.register(TransitionTask, self.transition_task)
        bus.register(PostponeTask, self.postpone_task)
        bus.register(CreateNote, self.create_note)
        bus.register(UpdateNote, self.update_note)
        bus.register(DeleteNote, self.delete_note)
        bus.register(CreateReminder, self.create_reminder)
        bus.register(CancelReminder, self.cancel_reminder)
        bus.register(AcknowledgeReminder, self.acknowledge_reminder)
        bus.register(SnoozeReminder, self.snooze_reminder)
        return bus

    def query_bus(self) -> QueryBus:
        bus = QueryBus()
        bus.register(AgendaBetween, self.agenda_between)
        bus.register(ListAgendaItems, self.list_agenda_items)
        bus.register(ListTasks, self.list_tasks)
        bus.register(SearchNotes, self.search_notes)
        bus.register(ListReminders, self.list_reminders)
        bus.register(ListAnniversaries, self.list_anniversaries)
        bus.register(ListDailyBriefings, self.list_daily_briefings)
        return bus
