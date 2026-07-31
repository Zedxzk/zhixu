"""SQLite repositories with authorization-bound writes and atomic audit events."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from zhixu.domain import (
    Action,
    AgendaItem,
    AgendaOccurrence,
    AuthorizedAction,
    DataClassification,
    EncryptedIdentifier,
    ExceptionAction,
    ExternalIdentity,
    JobRun,
    JobRunStatus,
    MissedReminderPolicy,
    Note,
    NoteAttachment,
    RecurrenceException,
    RecurrenceRule,
    Reminder,
    ReminderStatus,
    ResourceRef,
    ScheduledJob,
    Task,
    TaskStatus,
    User,
    UserStatus,
    occurrences_between,
)
from zhixu.domain.agenda import require_aware
from zhixu.domain.errors import (
    ConcurrencyConflict,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)

from .database import Database


def _dump_datetime(value: datetime) -> str:
    require_aware(value, "datetime")
    return value.astimezone(UTC).isoformat()


def _load_datetime(value: str | None, *, timezone: str | None = None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if timezone is not None:
        return parsed.astimezone(ZoneInfo(timezone))
    return parsed


_REMINDER_NOTIFICATION_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _escape_markdown_text(value: str) -> str:
    """Keep user-owned reminder text from becoming active Markdown content."""
    compact = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"([\\`*_{}\[\]()#+\-.!>|])", r"\\\1", compact)


def _reminder_notification_text(reminder: Reminder) -> str:
    local_fire_at = reminder.fire_at.astimezone(_REMINDER_NOTIFICATION_TIMEZONE)
    title = _escape_markdown_text(reminder.title)
    return (
        "# ⏰ 日程提醒\n\n"
        f"**事项：** {title}\n\n"
        f"**时间：** {local_fire_at:%Y-%m-%d %H:%M}（北京时间）"
    )


def _resource(
    kind: str,
    resource_id: str,
    owner_user_id: str,
    classification: DataClassification,
) -> ResourceRef:
    return ResourceRef(kind, resource_id, owner_user_id, classification)


def _require_authorization(
    authorization: AuthorizedAction,
    *,
    action: Action,
    kind: str,
    resource_id: str,
    owner_user_id: str,
    classification: DataClassification,
) -> None:
    expected = _resource(kind, resource_id, owner_user_id, classification)
    if authorization.action is not action or authorization.resource != expected:
        raise PermissionDenied("repository authorization does not match the write")


def _audit(
    connection: sqlite3.Connection,
    authorization: AuthorizedAction,
    *,
    outcome: str = "completed",
    reason_code: str = "",
) -> None:
    connection.execute(
        """
        INSERT INTO audit_events(
            occurred_at,actor_user_id,action,resource_kind,resource_id,outcome,reason_code
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            _dump_datetime(authorization.authorized_at),
            authorization.actor_user_id,
            authorization.action.value,
            authorization.resource.kind,
            authorization.resource.id,
            outcome,
            reason_code,
        ),
    )


def _raise_conflict(exc: sqlite3.IntegrityError) -> ConflictError:
    return ConflictError("the resource conflicts with existing data")


class GrantRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def has_grant(self, actor_user_id: str, action: Action, resource: ResourceRef) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM resource_acl
                WHERE resource_kind=? AND resource_id=?
                  AND subject_user_id=? AND action=?
                """,
                (resource.kind, resource.id, actor_user_id, action.value),
            ).fetchone()
        return row is not None

    def grant(
        self,
        *,
        subject_user_id: str,
        action: Action,
        authorization: AuthorizedAction,
    ) -> None:
        _require_authorization(
            authorization,
            action=Action.GRANT,
            kind=authorization.resource.kind,
            resource_id=authorization.resource.id,
            owner_user_id=authorization.resource.owner_user_id,
            classification=authorization.resource.classification,
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO resource_acl(
                    resource_kind,resource_id,subject_user_id,action,granted_by,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    authorization.resource.kind,
                    authorization.resource.id,
                    subject_user_id,
                    action.value,
                    authorization.actor_user_id,
                    _dump_datetime(authorization.authorized_at),
                ),
            )
            _audit(connection, authorization)

    def revoke(
        self,
        *,
        subject_user_id: str,
        action: Action,
        authorization: AuthorizedAction,
    ) -> bool:
        _require_authorization(
            authorization,
            action=Action.GRANT,
            kind=authorization.resource.kind,
            resource_id=authorization.resource.id,
            owner_user_id=authorization.resource.owner_user_id,
            classification=authorization.resource.classification,
        )
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                DELETE FROM resource_acl
                WHERE resource_kind=? AND resource_id=?
                  AND subject_user_id=? AND action=?
                """,
                (
                    authorization.resource.kind,
                    authorization.resource.id,
                    subject_user_id,
                    action.value,
                ),
            ).rowcount
            _audit(
                connection,
                authorization,
                outcome="completed" if changed else "not_found",
            )
        return changed == 1


class UserRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, user: User, authorization: AuthorizedAction) -> User:
        _require_authorization(
            authorization,
            action=Action.CREATE,
            kind="user",
            resource_id=user.id,
            owner_user_id=user.id,
            classification=DataClassification.PERSONAL,
        )
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO users(id,display_name,status,created_at)
                    VALUES(?,?,?,?)
                    """,
                    (
                        user.id,
                        user.display_name,
                        user.status.value,
                        _dump_datetime(user.created_at),
                    ),
                )
                _audit(connection, authorization)
        except sqlite3.IntegrityError as exc:
            raise _raise_conflict(exc) from exc
        return user

    def get(self, user_id: str) -> User | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        created_at = _load_datetime(str(row["created_at"]))
        assert created_at is not None
        return User(
            id=str(row["id"]),
            display_name=str(row["display_name"]),
            status=UserStatus(str(row["status"])),
            created_at=created_at,
        )

    def bind_identity(
        self,
        identity: ExternalIdentity,
        authorization: AuthorizedAction,
    ) -> ExternalIdentity:
        _require_authorization(
            authorization,
            action=Action.CREATE,
            kind="external_identity",
            resource_id=identity.id,
            owner_user_id=identity.user_id,
            classification=DataClassification.PERSONAL,
        )
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO external_identities(
                        id,user_id,channel,channel_account,external_subject_enc,
                        opaque_ref,created_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        identity.id,
                        identity.user_id,
                        identity.channel,
                        identity.channel_account,
                        identity.encrypted_subject.value,
                        identity.opaque_ref,
                        _dump_datetime(identity.created_at),
                    ),
                )
                _audit(connection, authorization)
        except sqlite3.IntegrityError as exc:
            raise _raise_conflict(exc) from exc
        return identity

    def identity_by_opaque_ref(
        self,
        channel: str,
        channel_account: str,
        opaque_ref: str,
    ) -> ExternalIdentity | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM external_identities
                WHERE channel=? AND channel_account=? AND opaque_ref=?
                """,
                (channel, channel_account, opaque_ref),
            ).fetchone()
        if row is None:
            return None
        created_at = _load_datetime(str(row["created_at"]))
        assert created_at is not None
        return ExternalIdentity(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            channel=str(row["channel"]),
            channel_account=str(row["channel_account"]),
            encrypted_subject=EncryptedIdentifier(str(row["external_subject_enc"])),
            opaque_ref=str(row["opaque_ref"]),
            created_at=created_at,
        )

    def list_identities(self, user_id: str) -> list[ExternalIdentity]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM external_identities
                WHERE user_id=? ORDER BY channel,channel_account,opaque_ref
                """,
                (user_id,),
            ).fetchall()
        identities: list[ExternalIdentity] = []
        for row in rows:
            created_at = _load_datetime(str(row["created_at"]))
            assert created_at is not None
            identities.append(
                ExternalIdentity(
                    id=str(row["id"]),
                    user_id=str(row["user_id"]),
                    channel=str(row["channel"]),
                    channel_account=str(row["channel_account"]),
                    encrypted_subject=EncryptedIdentifier(
                        str(row["external_subject_enc"])
                    ),
                    opaque_ref=str(row["opaque_ref"]),
                    created_at=created_at,
                )
            )
        return identities

    def unbind_identity(
        self,
        identity_id: str,
        authorization: AuthorizedAction,
    ) -> bool:
        _require_authorization(
            authorization,
            action=Action.DELETE,
            kind="external_identity",
            resource_id=identity_id,
            owner_user_id=authorization.resource.owner_user_id,
            classification=DataClassification.PERSONAL,
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT user_id FROM external_identities WHERE id=?",
                (identity_id,),
            ).fetchone()
            if row is None:
                _audit(connection, authorization, outcome="not_found")
                return False
            if str(row["user_id"]) != authorization.resource.owner_user_id:
                raise PermissionDenied("identity owner does not match authorization")
            now_text = _dump_datetime(authorization.authorized_at)
            connection.execute(
                """
                UPDATE channel_sessions SET revoked_at=?
                WHERE external_identity_id=? AND revoked_at IS NULL
                """,
                (now_text, identity_id),
            )
            connection.execute(
                "DELETE FROM external_identities WHERE id=?",
                (identity_id,),
            )
            _audit(connection, authorization)
        return True


class AgendaRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> AgendaItem:
        timezone = str(row["timezone"])
        recurrence_row = connection.execute(
            "SELECT * FROM recurrence_rules WHERE agenda_item_id=?",
            (str(row["id"]),),
        ).fetchone()
        recurrence = (
            RecurrenceRule(
                value=str(recurrence_row["rule_text"]),
                timezone=str(recurrence_row["timezone"]),
            )
            if recurrence_row is not None
            else None
        )
        start_at = _load_datetime(str(row["start_at"]), timezone=timezone)
        end_at = _load_datetime(str(row["end_at"]), timezone=timezone)
        created_at = _load_datetime(str(row["created_at"]))
        updated_at = _load_datetime(str(row["updated_at"]))
        assert start_at is not None
        assert end_at is not None
        assert created_at is not None
        assert updated_at is not None
        return AgendaItem(
            id=str(row["id"]),
            owner_user_id=str(row["owner_user_id"]),
            creator_user_id=(
                str(row["creator_user_id"])
                if row["creator_user_id"] is not None
                else str(row["owner_user_id"])
            ),
            title=str(row["title"]),
            description=str(row["description"]),
            start_at=start_at,
            end_at=end_at,
            timezone=timezone,
            all_day=bool(row["all_day"]),
            classification=DataClassification(int(row["classification"])),
            recurrence=recurrence,
            version=int(row["version"]),
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def _write_recurrence(
        connection: sqlite3.Connection,
        item: AgendaItem,
    ) -> None:
        if item.recurrence is None:
            connection.execute(
                "DELETE FROM recurrence_rules WHERE agenda_item_id=?",
                (item.id,),
            )
            return
        connection.execute(
            """
            INSERT INTO recurrence_rules(agenda_item_id,rule_text,timezone)
            VALUES(?,?,?)
            ON CONFLICT(agenda_item_id) DO UPDATE SET
              rule_text=excluded.rule_text,
              timezone=excluded.timezone
            """,
            (item.id, item.recurrence.value, item.recurrence.timezone),
        )

    def create(self, item: AgendaItem, authorization: AuthorizedAction) -> AgendaItem:
        _require_authorization(
            authorization,
            action=Action.CREATE,
            kind="agenda",
            resource_id=item.id,
            owner_user_id=item.owner_user_id,
            classification=item.classification,
        )
        stored = replace(
            item,
            version=1,
            created_at=authorization.authorized_at,
            updated_at=authorization.authorized_at,
        )
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO agenda_items(
                        id,owner_user_id,creator_user_id,title,description,start_at,end_at,timezone,
                        all_day,classification,version,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stored.id,
                        stored.owner_user_id,
                        stored.creator_user_id,
                        stored.title,
                        stored.description,
                        _dump_datetime(stored.start_at),
                        _dump_datetime(stored.end_at),
                        stored.timezone,
                        int(stored.all_day),
                        int(stored.classification),
                        stored.version,
                        _dump_datetime(stored.created_at),
                        _dump_datetime(stored.updated_at),
                    ),
                )
                self._write_recurrence(connection, stored)
                _audit(connection, authorization)
        except sqlite3.IntegrityError as exc:
            raise _raise_conflict(exc) from exc
        return stored

    def get(self, item_id: str) -> AgendaItem | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agenda_items WHERE id=?",
                (item_id,),
            ).fetchone()
            return self._from_row(connection, row) if row is not None else None

    def list_for_owner(self, owner_user_id: str) -> list[AgendaItem]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agenda_items
                WHERE owner_user_id=?
                ORDER BY start_at,id
                """,
                (owner_user_id,),
            ).fetchall()
            return [self._from_row(connection, row) for row in rows]

    def update(
        self,
        item: AgendaItem,
        *,
        expected_version: int,
        authorization: AuthorizedAction,
    ) -> AgendaItem:
        _require_authorization(
            authorization,
            action=Action.UPDATE,
            kind="agenda",
            resource_id=item.id,
            owner_user_id=item.owner_user_id,
            classification=item.classification,
        )
        if item.version != expected_version + 1:
            raise ValidationError("updated agenda version must increment exactly once")
        stored = replace(item, updated_at=authorization.authorized_at)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE agenda_items SET
                    title=?,description=?,start_at=?,end_at=?,timezone=?,all_day=?,
                    classification=?,version=?,updated_at=?
                WHERE id=? AND owner_user_id=? AND version=?
                """,
                (
                    stored.title,
                    stored.description,
                    _dump_datetime(stored.start_at),
                    _dump_datetime(stored.end_at),
                    stored.timezone,
                    int(stored.all_day),
                    int(stored.classification),
                    stored.version,
                    _dump_datetime(stored.updated_at),
                    stored.id,
                    stored.owner_user_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("agenda item changed or does not exist")
            self._write_recurrence(connection, stored)
            _audit(connection, authorization)
        return stored

    def delete(self, item_id: str, authorization: AuthorizedAction) -> None:
        if authorization.resource.id != item_id:
            raise PermissionDenied("authorization does not match agenda item")
        _require_authorization(
            authorization,
            action=Action.DELETE,
            kind="agenda",
            resource_id=item_id,
            owner_user_id=authorization.resource.owner_user_id,
            classification=authorization.resource.classification,
        )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM agenda_items WHERE id=? AND owner_user_id=?",
                (item_id, authorization.resource.owner_user_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("agenda item not found")
            _audit(connection, authorization)

    def add_exception(
        self,
        item_id: str,
        exception: RecurrenceException,
        authorization: AuthorizedAction,
    ) -> None:
        _require_authorization(
            authorization,
            action=Action.UPDATE,
            kind="agenda",
            resource_id=item_id,
            owner_user_id=authorization.resource.owner_user_id,
            classification=authorization.resource.classification,
        )
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM agenda_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if exists is None:
                raise NotFoundError("agenda item not found")
            connection.execute(
                """
                INSERT INTO recurrence_exceptions(
                    agenda_item_id,occurrence_at,action,replacement_start,replacement_end
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(agenda_item_id,occurrence_at) DO UPDATE SET
                    action=excluded.action,
                    replacement_start=excluded.replacement_start,
                    replacement_end=excluded.replacement_end
                """,
                (
                    item_id,
                    _dump_datetime(exception.occurrence_at),
                    exception.action.value,
                    (
                        _dump_datetime(exception.replacement_start)
                        if exception.replacement_start is not None
                        else None
                    ),
                    (
                        _dump_datetime(exception.replacement_end)
                        if exception.replacement_end is not None
                        else None
                    ),
                ),
            )
            _audit(connection, authorization)

    @staticmethod
    def _exceptions(
        connection: sqlite3.Connection,
        item: AgendaItem,
    ) -> tuple[RecurrenceException, ...]:
        rows = connection.execute(
            """
            SELECT * FROM recurrence_exceptions
            WHERE agenda_item_id=? ORDER BY occurrence_at
            """,
            (item.id,),
        ).fetchall()
        timezone = item.timezone
        result: list[RecurrenceException] = []
        for row in rows:
            occurrence_at = _load_datetime(str(row["occurrence_at"]), timezone=timezone)
            replacement_start = _load_datetime(row["replacement_start"], timezone=timezone)
            replacement_end = _load_datetime(row["replacement_end"], timezone=timezone)
            assert occurrence_at is not None
            result.append(
                RecurrenceException(
                    occurrence_at=occurrence_at,
                    action=ExceptionAction(str(row["action"])),
                    replacement_start=replacement_start,
                    replacement_end=replacement_end,
                )
            )
        return tuple(result)

    def occurrences(
        self,
        owner_user_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[AgendaOccurrence]:
        require_aware(window_start, "window_start")
        require_aware(window_end, "window_end")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.* FROM agenda_items a
                LEFT JOIN recurrence_rules r ON r.agenda_item_id=a.id
                WHERE a.owner_user_id=?
                  AND (r.agenda_item_id IS NOT NULL OR a.end_at>?)
                  AND (r.agenda_item_id IS NOT NULL OR a.start_at<?)
                ORDER BY a.start_at,a.id
                """,
                (
                    owner_user_id,
                    _dump_datetime(window_start),
                    _dump_datetime(window_end),
                ),
            ).fetchall()
            result: list[AgendaOccurrence] = []
            for row in rows:
                item = self._from_row(connection, row)
                result.extend(
                    occurrences_between(
                        item,
                        window_start,
                        window_end,
                        self._exceptions(connection, item),
                    )
                )
        return sorted(
            result,
            key=lambda occurrence: (occurrence.start_at, occurrence.agenda_item_id),
        )


class TaskRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Task:
        return Task(
            id=str(row["id"]),
            owner_user_id=str(row["owner_user_id"]),
            creator_user_id=(
                str(row["creator_user_id"])
                if row["creator_user_id"] is not None
                else str(row["owner_user_id"])
            ),
            title=str(row["title"]),
            description=str(row["description"]),
            status=TaskStatus(str(row["status"])),
            priority=int(row["priority"]),
            due_at=_load_datetime(row["due_at"]),
            classification=DataClassification(int(row["classification"])),
            version=int(row["version"]),
            created_at=_load_datetime(str(row["created_at"])),
            updated_at=_load_datetime(str(row["updated_at"])),
        )

    def create(self, task: Task, authorization: AuthorizedAction) -> Task:
        _require_authorization(
            authorization,
            action=Action.CREATE,
            kind="task",
            resource_id=task.id,
            owner_user_id=task.owner_user_id,
            classification=task.classification,
        )
        stored = replace(
            task,
            version=1,
            created_at=authorization.authorized_at,
            updated_at=authorization.authorized_at,
        )
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO tasks(
                        id,owner_user_id,creator_user_id,title,description,status,priority,due_at,
                        classification,version,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stored.id,
                        stored.owner_user_id,
                        stored.creator_user_id,
                        stored.title,
                        stored.description,
                        stored.status.value,
                        stored.priority,
                        _dump_datetime(stored.due_at) if stored.due_at else None,
                        int(stored.classification),
                        stored.version,
                        _dump_datetime(stored.created_at),
                        _dump_datetime(stored.updated_at),
                    ),
                )
                _audit(connection, authorization)
        except sqlite3.IntegrityError as exc:
            raise _raise_conflict(exc) from exc
        return stored

    def get(self, task_id: str) -> Task | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def update(
        self,
        task: Task,
        *,
        expected_version: int,
        authorization: AuthorizedAction,
    ) -> Task:
        _require_authorization(
            authorization,
            action=Action.UPDATE,
            kind="task",
            resource_id=task.id,
            owner_user_id=task.owner_user_id,
            classification=task.classification,
        )
        if task.version != expected_version + 1:
            raise ValidationError("updated task version must increment exactly once")
        stored = replace(task, updated_at=authorization.authorized_at)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET
                    title=?,description=?,status=?,priority=?,due_at=?,classification=?,
                    version=?,updated_at=?
                WHERE id=? AND owner_user_id=? AND version=?
                """,
                (
                    stored.title,
                    stored.description,
                    stored.status.value,
                    stored.priority,
                    _dump_datetime(stored.due_at) if stored.due_at else None,
                    int(stored.classification),
                    stored.version,
                    _dump_datetime(stored.updated_at),
                    stored.id,
                    stored.owner_user_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("task changed or does not exist")
            _audit(connection, authorization)
        return stored

    def list_for_owner(self, owner_user_id: str) -> list[Task]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks WHERE owner_user_id=?
                ORDER BY
                    CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
                    due_at, priority DESC, created_at
                """,
                (owner_user_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def delete(self, task_id: str, authorization: AuthorizedAction) -> None:
        _require_authorization(
            authorization,
            action=Action.DELETE,
            kind="task",
            resource_id=task_id,
            owner_user_id=authorization.resource.owner_user_id,
            classification=authorization.resource.classification,
        )
        with self.database.transaction() as connection:
            changed = connection.execute(
                "DELETE FROM tasks WHERE id=? AND owner_user_id=?",
                (task_id, authorization.resource.owner_user_id),
            ).rowcount
            if changed != 1:
                raise NotFoundError("task not found")
            _audit(connection, authorization)


class NoteRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _tags(connection: sqlite3.Connection, note_id: str) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT t.name FROM tags t
            JOIN resource_tags rt ON rt.tag_id=t.id
            WHERE rt.resource_kind='note' AND rt.resource_id=?
            ORDER BY t.name
            """,
            (note_id,),
        ).fetchall()
        return tuple(str(row["name"]) for row in rows)

    @staticmethod
    def _attachments(
        connection: sqlite3.Connection,
        note_id: str,
    ) -> tuple[NoteAttachment, ...]:
        rows = connection.execute(
            """
            SELECT id,filename,media_type,size_bytes,content_ref
            FROM note_attachments
            WHERE note_id=?
            ORDER BY id
            """,
            (note_id,),
        ).fetchall()
        return tuple(
            NoteAttachment(
                id=str(row["id"]),
                filename=str(row["filename"]),
                media_type=str(row["media_type"]),
                size_bytes=int(row["size_bytes"]),
                content_ref=str(row["content_ref"]),
            )
            for row in rows
        )

    @classmethod
    def _from_row(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> Note:
        return Note(
            id=str(row["id"]),
            owner_user_id=str(row["owner_user_id"]),
            creator_user_id=(
                str(row["creator_user_id"])
                if row["creator_user_id"] is not None
                else str(row["owner_user_id"])
            ),
            title=str(row["title"]),
            body=str(row["body"]),
            tags=cls._tags(connection, str(row["id"])),
            attachments=cls._attachments(connection, str(row["id"])),
            classification=DataClassification(int(row["classification"])),
            version=int(row["version"]),
            created_at=_load_datetime(str(row["created_at"])),
            updated_at=_load_datetime(str(row["updated_at"])),
        )

    @staticmethod
    def _write_tags(connection: sqlite3.Connection, note: Note) -> None:
        connection.execute(
            "DELETE FROM resource_tags WHERE resource_kind='note' AND resource_id=?",
            (note.id,),
        )
        for tag in note.tags:
            connection.execute(
                """
                INSERT INTO tags(owner_user_id,name) VALUES(?,?)
                ON CONFLICT(owner_user_id,name) DO NOTHING
                """,
                (note.owner_user_id, tag),
            )
            row = connection.execute(
                "SELECT id FROM tags WHERE owner_user_id=? AND name=?",
                (note.owner_user_id, tag),
            ).fetchone()
            assert row is not None
            connection.execute(
                """
                INSERT INTO resource_tags(resource_kind,resource_id,tag_id)
                VALUES('note',?,?)
                """,
                (note.id, int(row["id"])),
            )

    @staticmethod
    def _write_attachments(connection: sqlite3.Connection, note: Note) -> None:
        connection.execute(
            "DELETE FROM note_attachments WHERE note_id=?",
            (note.id,),
        )
        connection.executemany(
            """
            INSERT INTO note_attachments(
                note_id,id,filename,media_type,size_bytes,content_ref
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                (
                    note.id,
                    item.id,
                    item.filename,
                    item.media_type,
                    item.size_bytes,
                    item.content_ref,
                )
                for item in note.attachments
            ),
        )

    @staticmethod
    def _write_fts(connection: sqlite3.Connection, note: Note) -> None:
        connection.execute("DELETE FROM notes_fts WHERE note_id=?", (note.id,))
        connection.execute(
            """
            INSERT INTO notes_fts(note_id,owner_user_id,title,body)
            VALUES(?,?,?,?)
            """,
            (note.id, note.owner_user_id, note.title, note.body),
        )

    def create(self, note: Note, authorization: AuthorizedAction) -> Note:
        _require_authorization(
            authorization,
            action=Action.CREATE,
            kind="note",
            resource_id=note.id,
            owner_user_id=note.owner_user_id,
            classification=note.classification,
        )
        stored = replace(
            note,
            tags=tuple(sorted(set(note.tags))),
            version=1,
            created_at=authorization.authorized_at,
            updated_at=authorization.authorized_at,
        )
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO notes(
                        id,owner_user_id,creator_user_id,title,body,classification,
                        version,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stored.id,
                        stored.owner_user_id,
                        stored.creator_user_id,
                        stored.title,
                        stored.body,
                        int(stored.classification),
                        stored.version,
                        _dump_datetime(stored.created_at),
                        _dump_datetime(stored.updated_at),
                    ),
                )
                self._write_tags(connection, stored)
                self._write_attachments(connection, stored)
                self._write_fts(connection, stored)
                _audit(connection, authorization)
        except sqlite3.IntegrityError as exc:
            raise _raise_conflict(exc) from exc
        return stored

    def get(self, note_id: str) -> Note | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notes WHERE id=?",
                (note_id,),
            ).fetchone()
            return self._from_row(connection, row) if row is not None else None

    def list_for_owner(self, owner_user_id: str) -> list[Note]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notes
                WHERE owner_user_id=?
                ORDER BY updated_at DESC,id
                """,
                (owner_user_id,),
            ).fetchall()
            return [self._from_row(connection, row) for row in rows]

    def update(
        self,
        note: Note,
        *,
        expected_version: int,
        authorization: AuthorizedAction,
    ) -> Note:
        _require_authorization(
            authorization,
            action=Action.UPDATE,
            kind="note",
            resource_id=note.id,
            owner_user_id=note.owner_user_id,
            classification=note.classification,
        )
        if note.version != expected_version + 1:
            raise ValidationError("updated note version must increment exactly once")
        stored = replace(
            note,
            tags=tuple(sorted(set(note.tags))),
            updated_at=authorization.authorized_at,
        )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE notes SET
                    title=?,body=?,classification=?,version=?,updated_at=?
                WHERE id=? AND owner_user_id=? AND version=?
                """,
                (
                    stored.title,
                    stored.body,
                    int(stored.classification),
                    stored.version,
                    _dump_datetime(stored.updated_at),
                    stored.id,
                    stored.owner_user_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("note changed or does not exist")
            self._write_tags(connection, stored)
            self._write_attachments(connection, stored)
            self._write_fts(connection, stored)
            _audit(connection, authorization)
        return stored

    def delete(self, note_id: str, authorization: AuthorizedAction) -> None:
        _require_authorization(
            authorization,
            action=Action.DELETE,
            kind="note",
            resource_id=note_id,
            owner_user_id=authorization.resource.owner_user_id,
            classification=authorization.resource.classification,
        )
        with self.database.transaction() as connection:
            changed = connection.execute(
                "DELETE FROM notes WHERE id=? AND owner_user_id=?",
                (note_id, authorization.resource.owner_user_id),
            ).rowcount
            if changed != 1:
                raise NotFoundError("note not found")
            connection.execute("DELETE FROM notes_fts WHERE note_id=?", (note_id,))
            _audit(connection, authorization)

    def search(self, owner_user_id: str, query: str, *, limit: int = 20) -> list[Note]:
        if not 1 <= limit <= 100:
            raise ValidationError("search limit must be between 1 and 100")
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if not tokens:
            return []
        match = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        with self.database.connect() as connection:
            matches = connection.execute(
                """
                SELECT note_id FROM notes_fts
                WHERE owner_user_id=? AND notes_fts MATCH ?
                ORDER BY bm25(notes_fts)
                LIMIT ?
                """,
                (owner_user_id, match, limit),
            ).fetchall()
            result: list[Note] = []
            for match_row in matches:
                row = connection.execute(
                    "SELECT * FROM notes WHERE id=? AND owner_user_id=?",
                    (str(match_row["note_id"]), owner_user_id),
                ).fetchone()
                if row is not None:
                    result.append(self._from_row(connection, row))
        return result


class ReminderRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Reminder:
        fire_at = _load_datetime(str(row["fire_at"]))
        assert fire_at is not None
        return Reminder(
            id=str(row["id"]),
            owner_user_id=str(row["owner_user_id"]),
            creator_user_id=(
                str(row["creator_user_id"])
                if row["creator_user_id"] is not None
                else str(row["owner_user_id"])
            ),
            title=str(row["title"]),
            fire_at=fire_at,
            target_ref=str(row["target_ref"]),
            status=ReminderStatus(str(row["status"])),
            missed_policy=MissedReminderPolicy(str(row["missed_policy"])),
            classification=DataClassification(int(row["classification"])),
            related_kind=row["related_kind"],
            related_id=row["related_id"],
            version=int(row["version"]),
            created_at=_load_datetime(str(row["created_at"])),
            updated_at=_load_datetime(str(row["updated_at"])),
        )

    def create(
        self,
        reminder: Reminder,
        authorization: AuthorizedAction,
    ) -> Reminder:
        _require_authorization(
            authorization,
            action=Action.CREATE,
            kind="reminder",
            resource_id=reminder.id,
            owner_user_id=reminder.owner_user_id,
            classification=reminder.classification,
        )
        stored = replace(
            reminder,
            version=1,
            created_at=authorization.authorized_at,
            updated_at=authorization.authorized_at,
        )
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO reminders(
                        id,owner_user_id,creator_user_id,title,fire_at,target_ref,status,missed_policy,
                        classification,related_kind,related_id,version,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stored.id,
                        stored.owner_user_id,
                        stored.creator_user_id,
                        stored.title,
                        _dump_datetime(stored.fire_at),
                        stored.target_ref,
                        stored.status.value,
                        stored.missed_policy.value,
                        int(stored.classification),
                        stored.related_kind,
                        stored.related_id,
                        stored.version,
                        _dump_datetime(stored.created_at),
                        _dump_datetime(stored.updated_at),
                    ),
                )
                _audit(connection, authorization)
        except sqlite3.IntegrityError as exc:
            raise _raise_conflict(exc) from exc
        return stored

    def get(self, reminder_id: str) -> Reminder | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reminders WHERE id=?",
                (reminder_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_for_owner(self, owner_user_id: str) -> list[Reminder]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM reminders
                WHERE owner_user_id=?
                ORDER BY
                    CASE status
                        WHEN 'pending' THEN 0
                        WHEN 'fired' THEN 1
                        ELSE 2
                    END,
                    fire_at,id
                """,
                (owner_user_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def cancel(
        self,
        reminder_id: str,
        *,
        expected_version: int,
        authorization: AuthorizedAction,
    ) -> Reminder:
        existing = self.get(reminder_id)
        if existing is None:
            raise NotFoundError("reminder not found")
        _require_authorization(
            authorization,
            action=Action.UPDATE,
            kind="reminder",
            resource_id=existing.id,
            owner_user_id=existing.owner_user_id,
            classification=existing.classification,
        )
        now_text = _dump_datetime(authorization.authorized_at)
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE reminders
                SET status='cancelled',version=version+1,updated_at=?
                WHERE id=? AND version=? AND status IN ('pending','fired')
                """,
                (now_text, reminder_id, expected_version),
            ).rowcount
            if changed != 1:
                raise ConflictError("reminder is already cancelled")
            _audit(connection, authorization, reason_code="cancelled")
        updated = self.get(reminder_id)
        assert updated is not None
        return updated

    def acknowledge(
        self,
        reminder_id: str,
        *,
        expected_version: int,
        authorization: AuthorizedAction,
    ) -> Reminder:
        existing = self.get(reminder_id)
        if existing is None:
            raise NotFoundError("reminder not found")
        _require_authorization(
            authorization,
            action=Action.UPDATE,
            kind="reminder",
            resource_id=existing.id,
            owner_user_id=existing.owner_user_id,
            classification=existing.classification,
        )
        now_text = _dump_datetime(authorization.authorized_at)
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE reminders
                SET status='cancelled',version=version+1,updated_at=?
                WHERE id=? AND version=? AND status='fired'
                """,
                (now_text, reminder_id, expected_version),
            ).rowcount
            if changed != 1:
                raise ConflictError("reminder is not awaiting acknowledgement")
            _audit(connection, authorization, reason_code="acknowledged")
        updated = self.get(reminder_id)
        assert updated is not None
        return updated

    def snooze(
        self,
        reminder_id: str,
        *,
        fire_at: datetime,
        expected_version: int,
        authorization: AuthorizedAction,
    ) -> Reminder:
        require_aware(fire_at, "fire_at")
        if fire_at <= authorization.authorized_at:
            raise ValidationError("snooze time must be in the future")
        existing = self.get(reminder_id)
        if existing is None:
            raise NotFoundError("reminder not found")
        _require_authorization(
            authorization,
            action=Action.UPDATE,
            kind="reminder",
            resource_id=existing.id,
            owner_user_id=existing.owner_user_id,
            classification=existing.classification,
        )
        now_text = _dump_datetime(authorization.authorized_at)
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE reminders
                SET status='pending',fire_at=?,version=version+1,updated_at=?
                WHERE id=? AND version=? AND status='fired'
                """,
                (
                    _dump_datetime(fire_at),
                    now_text,
                    reminder_id,
                    expected_version,
                ),
            ).rowcount
            if changed != 1:
                raise ConflictError("reminder is not available to snooze")
            _audit(connection, authorization, reason_code="snoozed")
        updated = self.get(reminder_id)
        assert updated is not None
        return updated

    def enqueue_due(self, now: datetime, *, late_grace_seconds: int = 300) -> int:
        require_aware(now, "now")
        if late_grace_seconds < 0:
            raise ValidationError("late grace must not be negative")
        now_text = _dump_datetime(now)
        inserted = 0
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM reminders
                WHERE status='pending' AND fire_at<=?
                ORDER BY fire_at,id
                """,
                (now_text,),
            ).fetchall()
            for row in rows:
                reminder = self._from_row(row)
                lateness = (now - reminder.fire_at).total_seconds()
                if (
                    reminder.missed_policy is MissedReminderPolicy.SKIP
                    and lateness > late_grace_seconds
                ):
                    connection.execute(
                        """
                        UPDATE reminders SET status='cancelled',version=version+1,updated_at=?
                        WHERE id=? AND status='pending'
                        """,
                        (now_text, reminder.id),
                    )
                    connection.execute(
                        """
                        INSERT INTO audit_events(
                            occurred_at,actor_user_id,action,resource_kind,resource_id,
                            outcome,reason_code
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            now_text,
                            "service:scheduler",
                            "skip",
                            "reminder",
                            reminder.id,
                            "completed",
                            "missed_policy",
                        ),
                    )
                    continue

                idempotency_key = (
                    f"reminder:{reminder.id}:{_dump_datetime(reminder.fire_at)}"
                )
                delivery_id = "out_" + hashlib.sha256(
                    idempotency_key.encode("utf-8")
                ).hexdigest()[:24]
                payload = json.dumps(
                    {
                        "text": _reminder_notification_text(reminder),
                        "reminder_id": reminder.id,
                        "buttons": [
                            {
                                "label": f"{minutes}分钟",
                                "action": (
                                    f"/提醒稍后 {reminder.id} {minutes}分钟"
                                ),
                            }
                            for minutes in (5, 15, 30, 60)
                        ]
                        + [
                            {
                                "label": "完成",
                                "action": f"/提醒完成 {reminder.id}",
                            },
                            {
                                "label": "取消",
                                "action": f"/取消提醒 {reminder.id}",
                            },
                        ],
                        "attachment_url": None,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                target = connection.execute(
                    """
                    SELECT channel,channel_account
                    FROM channel_routes
                    WHERE opaque_ref=?
                    ORDER BY last_seen_at DESC
                    LIMIT 1
                    """,
                    (reminder.target_ref,),
                ).fetchone()
                channel = str(target["channel"]) if target is not None else ""
                channel_account = (
                    str(target["channel_account"]) if target is not None else ""
                )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO outbox_deliveries(
                        id,idempotency_key,owner_user_id,target_ref,message_kind,
                        payload_json,classification,priority,status,attempts,max_attempts,
                        next_attempt_at,last_error_code,created_at,updated_at,
                        channel,channel_account
                    ) VALUES(?,?,?,?,?,?,?,10,'pending',0,5,?,'',?,?,?,?)
                    """,
                    (
                        delivery_id,
                        idempotency_key,
                        reminder.owner_user_id,
                        reminder.target_ref,
                        "text",
                        payload,
                        int(reminder.classification),
                        now_text,
                        now_text,
                        now_text,
                        channel,
                        channel_account,
                    ),
                )
                inserted += max(0, cursor.rowcount)
                connection.execute(
                    """
                    UPDATE reminders SET status='fired',version=version+1,updated_at=?
                    WHERE id=? AND status='pending'
                    """,
                    (now_text, reminder.id),
                )
                connection.execute(
                    """
                    INSERT INTO audit_events(
                        occurred_at,actor_user_id,action,resource_kind,resource_id,
                        outcome,reason_code
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        now_text,
                        "service:scheduler",
                        "enqueue",
                        "reminder",
                        reminder.id,
                        "completed",
                        "",
                    ),
                )
        return inserted


class ScheduledJobRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ScheduledJob:
        return ScheduledJob(
            id=str(row["id"]),
            owner_user_id=str(row["owner_user_id"]),
            job_kind=str(row["job_kind"]),
            schedule_spec=str(row["schedule_spec"]),
            timezone=str(row["timezone"]),
            enabled=bool(row["enabled"]),
            created_at=_load_datetime(str(row["created_at"])),
            updated_at=_load_datetime(str(row["updated_at"])),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> JobRun:
        scheduled_for = _load_datetime(str(row["scheduled_for"]))
        assert scheduled_for is not None
        return JobRun(
            id=str(row["id"]),
            scheduled_job_id=str(row["scheduled_job_id"]),
            scheduled_for=scheduled_for,
            status=JobRunStatus(str(row["status"])),
            started_at=_load_datetime(row["started_at"]),
            completed_at=_load_datetime(row["completed_at"]),
            error_code=str(row["error_code"]),
        )

    def create(
        self,
        job: ScheduledJob,
        authorization: AuthorizedAction,
    ) -> ScheduledJob:
        _require_authorization(
            authorization,
            action=Action.CREATE,
            kind="scheduled_job",
            resource_id=job.id,
            owner_user_id=job.owner_user_id,
            classification=DataClassification.PERSONAL,
        )
        stored = replace(
            job,
            created_at=authorization.authorized_at,
            updated_at=authorization.authorized_at,
        )
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO scheduled_jobs(
                        id,owner_user_id,job_kind,schedule_spec,timezone,
                        enabled,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        stored.id,
                        stored.owner_user_id,
                        stored.job_kind,
                        stored.schedule_spec,
                        stored.timezone,
                        int(stored.enabled),
                        _dump_datetime(stored.created_at),
                        _dump_datetime(stored.updated_at),
                    ),
                )
                _audit(connection, authorization)
        except sqlite3.IntegrityError as exc:
            raise _raise_conflict(exc) from exc
        return stored

    def get(self, job_id: str) -> ScheduledJob | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def create_run(
        self,
        job: ScheduledJob,
        scheduled_for: datetime,
        authorization: AuthorizedAction,
    ) -> tuple[JobRun, bool]:
        require_aware(scheduled_for, "scheduled_for")
        _require_authorization(
            authorization,
            action=Action.UPDATE,
            kind="scheduled_job",
            resource_id=job.id,
            owner_user_id=job.owner_user_id,
            classification=DataClassification.PERSONAL,
        )
        scheduled_text = _dump_datetime(scheduled_for)
        idempotency = f"{job.id}:{scheduled_text}"
        run_id = "run_" + hashlib.sha256(idempotency.encode("utf-8")).hexdigest()[:24]
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO job_runs(
                    id,scheduled_job_id,scheduled_for,status,started_at,completed_at,error_code
                ) VALUES(?,?,?,'pending',NULL,NULL,'')
                """,
                (run_id, job.id, scheduled_text),
            )
            row = connection.execute(
                """
                SELECT * FROM job_runs
                WHERE scheduled_job_id=? AND scheduled_for=?
                """,
                (job.id, scheduled_text),
            ).fetchone()
            assert row is not None
            if cursor.rowcount == 1:
                _audit(connection, authorization)
            return self._run_from_row(row), cursor.rowcount == 1


class OutboxRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def count(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM outbox_deliveries"
            ).fetchone()
        assert row is not None
        return int(row["count"])

    def list_pending(self) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id,idempotency_key,owner_user_id,target_ref,message_kind,
                       payload_json,classification,priority,status,attempts,
                       max_attempts,next_attempt_at
                FROM outbox_deliveries
                WHERE status='pending'
                ORDER BY priority,created_at,id
                """
            ).fetchall()
        return [dict(row) for row in rows]
