"""Repository protocols; every write requires a policy authorization."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from zhixu.domain import (
    AgendaItem,
    AgendaOccurrence,
    AuthorizedAction,
    ExternalIdentity,
    JobRun,
    Note,
    RecurrenceException,
    Reminder,
    ScheduledJob,
    Task,
    User,
)


class UserRepositoryPort(Protocol):
    def create(self, user: User, authorization: AuthorizedAction) -> User: ...

    def get(self, user_id: str) -> User | None: ...

    def bind_identity(
        self,
        identity: ExternalIdentity,
        authorization: AuthorizedAction,
    ) -> ExternalIdentity: ...


class AgendaRepositoryPort(Protocol):
    def create(self, item: AgendaItem, authorization: AuthorizedAction) -> AgendaItem: ...

    def get(self, item_id: str) -> AgendaItem | None: ...

    def update(
        self,
        item: AgendaItem,
        *,
        expected_version: int,
        authorization: AuthorizedAction,
    ) -> AgendaItem: ...

    def delete(self, item_id: str, authorization: AuthorizedAction) -> None: ...

    def add_exception(
        self,
        item_id: str,
        exception: RecurrenceException,
        authorization: AuthorizedAction,
    ) -> None: ...

    def occurrences(
        self,
        owner_user_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[AgendaOccurrence]: ...


class TaskRepositoryPort(Protocol):
    def create(self, task: Task, authorization: AuthorizedAction) -> Task: ...

    def get(self, task_id: str) -> Task | None: ...

    def update(
        self,
        task: Task,
        *,
        expected_version: int,
        authorization: AuthorizedAction,
    ) -> Task: ...

    def list_for_owner(self, owner_user_id: str) -> list[Task]: ...


class NoteRepositoryPort(Protocol):
    def create(self, note: Note, authorization: AuthorizedAction) -> Note: ...

    def get(self, note_id: str) -> Note | None: ...

    def update(
        self,
        note: Note,
        *,
        expected_version: int,
        authorization: AuthorizedAction,
    ) -> Note: ...

    def search(self, owner_user_id: str, query: str, *, limit: int = 20) -> list[Note]: ...


class ReminderRepositoryPort(Protocol):
    def create(
        self,
        reminder: Reminder,
        authorization: AuthorizedAction,
    ) -> Reminder: ...

    def get(self, reminder_id: str) -> Reminder | None: ...

    def enqueue_due(self, now: datetime, *, late_grace_seconds: int = 300) -> int: ...


class ScheduledJobRepositoryPort(Protocol):
    def create(
        self,
        job: ScheduledJob,
        authorization: AuthorizedAction,
    ) -> ScheduledJob: ...

    def get(self, job_id: str) -> ScheduledJob | None: ...

    def create_run(
        self,
        job: ScheduledJob,
        scheduled_for: datetime,
        authorization: AuthorizedAction,
    ) -> tuple[JobRun, bool]: ...
