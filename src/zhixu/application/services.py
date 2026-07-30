"""Policy-enforced deterministic use cases."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace

from zhixu.domain import (
    Action,
    AgendaItem,
    CommandContext,
    DataClassification,
    Note,
    PolicyEngine,
    RecurrenceException,
    RecurrenceRule,
    Reminder,
    ResourceRef,
    Task,
)
from zhixu.domain.errors import ConcurrencyConflict, NotFoundError
from zhixu.ports import (
    AgendaRepositoryPort,
    Clock,
    NoteRepositoryPort,
    ReminderRepositoryPort,
    TaskRepositoryPort,
)

from .commands import (
    AcknowledgeReminder,
    CommandBus,
    CreateAgenda,
    CreateNote,
    CreateReminder,
    CreateTask,
    DeleteAgenda,
    DeleteNote,
    DeleteTask,
    PostponeTask,
    SetAgendaException,
    SnoozeReminder,
    TransitionTask,
    UpdateAgenda,
    UpdateNote,
    UpdateTask,
)
from .queries import AgendaBetween, ListTasks, QueryBus, SearchNotes

IdFactory = Callable[[str], str]


def random_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


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
    ) -> None:
        self.agenda = agenda
        self.tasks = tasks
        self.notes = notes
        self.reminders = reminders
        self.policy = policy
        self.clock = clock
        self.id_factory = id_factory

    def _context(self, context: CommandContext) -> CommandContext:
        return replace(context, now=self.clock.now())

    @staticmethod
    def _ref(
        kind: str,
        resource_id: str,
        owner_user_id: str,
        classification: DataClassification,
    ) -> ResourceRef:
        return ResourceRef(kind, resource_id, owner_user_id, classification)

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
            owner_user_id=current.actor_user_id,
            title=command.title,
            description=command.description,
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
            owner_user_id=current.actor_user_id,
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
            owner_user_id=current.actor_user_id,
            title=command.title,
            body=command.body,
            tags=command.tags,
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
            owner_user_id=current.actor_user_id,
            title=command.title,
            fire_at=command.fire_at,
            target_ref=command.target_ref,
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
        self.policy.require(
            current,
            Action.READ,
            self._ref(
                "agenda_collection",
                current.actor_user_id,
                current.actor_user_id,
                DataClassification.PERSONAL,
            ),
        )
        occurrences = self.agenda.occurrences(
            current.actor_user_id,
            query.window_start,
            query.window_end,
        )
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

    def list_tasks(self, query: ListTasks, context: CommandContext) -> list[Task]:
        current = self._context(context)
        self.policy.require(
            current,
            Action.READ,
            self._ref(
                "task_collection",
                current.actor_user_id,
                current.actor_user_id,
                DataClassification.PERSONAL,
            ),
        )
        tasks = self.tasks.list_for_owner(current.actor_user_id)
        for task in tasks:
            self.policy.require(
                current,
                Action.READ,
                self._ref("task", task.id, task.owner_user_id, task.classification),
            )
        return tasks if query.include_archived else [
            task for task in tasks if task.status.value != "archived"
        ]

    def search_notes(self, query: SearchNotes, context: CommandContext) -> list[Note]:
        current = self._context(context)
        self.policy.require(
            current,
            Action.READ,
            self._ref(
                "note_collection",
                current.actor_user_id,
                current.actor_user_id,
                DataClassification.PERSONAL,
            ),
        )
        notes = self.notes.search(current.actor_user_id, query.text, limit=query.limit)
        visible: list[Note] = []
        for note in notes:
            self.policy.require(
                current,
                Action.READ,
                self._ref("note", note.id, note.owner_user_id, note.classification),
            )
            visible.append(note)
        return visible

    def command_bus(self) -> CommandBus:
        bus = CommandBus()
        bus.register(CreateAgenda, self.create_agenda)
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
        bus.register(AcknowledgeReminder, self.acknowledge_reminder)
        bus.register(SnoozeReminder, self.snooze_reminder)
        return bus

    def query_bus(self) -> QueryBus:
        bus = QueryBus()
        bus.register(AgendaBetween, self.agenda_between)
        bus.register(ListTasks, self.list_tasks)
        bus.register(SearchNotes, self.search_notes)
        return bus
