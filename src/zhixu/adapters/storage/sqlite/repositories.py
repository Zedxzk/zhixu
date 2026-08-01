"""SQLite repositories with authorization-bound writes and atomic audit events."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from zhixu.domain import (
    DEFAULT_NOTIFICATION_LEAD_MINUTES,
    Action,
    ActionLink,
    AgendaItem,
    AgendaNotificationRule,
    AgendaOccurrence,
    Anniversary,
    AuthorizedAction,
    CalendarSystem,
    DailyBriefing,
    DataClassification,
    EncryptedIdentifier,
    ExceptionAction,
    ExternalIdentity,
    ImportantDayKind,
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
    normalise_lead_minutes,
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


_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def _humanise_gap(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes <= 0:
        return "现在开始"
    if minutes < 60:
        return f"还有 {minutes} 分钟"
    if minutes < 24 * 60:
        hours, rest = divmod(minutes, 60)
        return f"还有 {hours} 小时" + (f" {rest} 分钟" if rest else "")
    days, rest = divmod(minutes, 24 * 60)
    hours = rest // 60
    return f"还有 {days} 天" + (f" {hours} 小时" if hours else "")


def _reminder_notification_text(reminder: Reminder) -> str:
    local_fire_at = reminder.fire_at.astimezone(_REMINDER_NOTIFICATION_TIMEZONE)
    title = _escape_markdown_text(reminder.title)
    lines = ["# ⏰ 日程提醒", "", f"**事项：** {title}"]
    if reminder.related_start_at is not None:
        # This reminder speaks before the thing it announces, so the moment the
        # reader needs is when that thing starts, not when the reminder fired.
        starts_at = reminder.related_start_at.astimezone(
            _REMINDER_NOTIFICATION_TIMEZONE
        )
        gap = (reminder.related_start_at - reminder.fire_at).total_seconds()
        weekday = _WEEKDAYS[starts_at.weekday()]
        same_day = starts_at.date() == local_fire_at.date()
        when = f"{starts_at:%H:%M}" if same_day else f"{starts_at:%Y-%m-%d %H:%M}"
        lines.extend(
            [
                "",
                f"**开始：** {when} 周{weekday}"
                + ("（今天）" if same_day else "")
                + "（北京时间）",
                "",
                f"**距开始：** {_humanise_gap(gap)}",
            ]
        )
    else:
        lines.extend(
            ["", f"**时间：** {local_fire_at:%Y-%m-%d %H:%M}（北京时间）"]
        )
    return "\n".join(lines)


def _dump_action_links(links: tuple[ActionLink, ...]) -> str:
    return json.dumps(
        [{"label": link.label, "url": link.url} for link in links],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _load_action_links(value: object) -> tuple[ActionLink, ...]:
    decoded = json.loads(str(value or "[]"))
    if not isinstance(decoded, list) or len(decoded) > 8:
        raise ValidationError("stored action links are invalid")
    return tuple(
        ActionLink(str(item.get("label") or ""), str(item.get("url") or ""))
        for item in decoded
        if isinstance(item, dict)
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

    def has_role(self, user_id: str, role_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM role_bindings
                WHERE user_id=? AND role_id=?
                """,
                (user_id, role_id),
            ).fetchone()
        return row is not None

    def assign_project_admin_if_vacant(
        self,
        user_id: str,
        *,
        now: datetime,
    ) -> bool:
        """Assign the singleton bootstrap role without replacing an existing admin."""
        require_aware(now, "now")
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT user_id FROM role_bindings
                WHERE role_id='project_admin'
                """
            ).fetchone()
            if existing is not None:
                return str(existing["user_id"]) == user_id
            active_user = connection.execute(
                "SELECT 1 FROM users WHERE id=? AND status='active'",
                (user_id,),
            ).fetchone()
            if active_user is None:
                raise ValidationError("project administrator must be an active user")
            connection.execute(
                """
                INSERT INTO role_bindings(user_id,role_id,created_at)
                VALUES(?,'project_admin',?)
                """,
                (user_id, _dump_datetime(now)),
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    occurred_at,actor_user_id,action,resource_kind,
                    resource_id,outcome,reason_code
                ) VALUES(?,?,'grant','role','project_admin','completed','bootstrap')
                """,
                (_dump_datetime(now), user_id),
            )
        return True

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
            action_links=_load_action_links(row["action_links_json"]),
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
                        id,owner_user_id,creator_user_id,title,description,action_links_json,
                        start_at,end_at,timezone,
                        all_day,classification,version,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stored.id,
                        stored.owner_user_id,
                        stored.creator_user_id,
                        stored.title,
                        stored.description,
                        _dump_action_links(stored.action_links),
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

    def list_scheduled(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> list[AgendaItem]:
        """Items that could occur in the window, across every owner.

        A recurring item starts once and repeats indefinitely, so anything
        beginning before the window ends is a candidate and the caller expands
        the recurrence to find the occurrences that actually land inside it.
        """

        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agenda_items
                WHERE start_at<=?
                  AND (id IN (SELECT agenda_item_id FROM recurrence_rules) OR end_at>=?)
                ORDER BY start_at,id
                """,
                (
                    _dump_datetime(window_end),
                    _dump_datetime(window_start),
                ),
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
                    title=?,description=?,action_links_json=?,start_at=?,end_at=?,timezone=?,all_day=?,
                    classification=?,version=?,updated_at=?
                WHERE id=? AND owner_user_id=? AND version=?
                """,
                (
                    stored.title,
                    stored.description,
                    _dump_action_links(stored.action_links),
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


class AgendaNotificationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AgendaNotificationRule:
        created_at = _load_datetime(str(row["created_at"]))
        assert created_at is not None
        return AgendaNotificationRule(
            id=str(row["id"]),
            agenda_item_id=str(row["agenda_item_id"]),
            owner_user_id=str(row["owner_user_id"]),
            creator_user_id=str(row["creator_user_id"]),
            target_ref=str(row["target_ref"]),
            time_of_day=time.fromisoformat(str(row["time_of_day"])),
            day_offset=int(row["day_offset"]),
            text=str(row["notification_text"]),
            timezone=str(row["timezone"]),
            action_links=_load_action_links(row["action_links_json"]),
            classification=DataClassification(int(row["classification"])),
            enabled=bool(row["enabled"]),
            created_at=created_at,
        )

    def create(
        self,
        rule: AgendaNotificationRule,
        authorization: AuthorizedAction,
    ) -> AgendaNotificationRule:
        _require_authorization(
            authorization,
            action=Action.CREATE,
            kind="agenda_notification",
            resource_id=rule.id,
            owner_user_id=rule.owner_user_id,
            classification=rule.classification,
        )
        stored = replace(rule, created_at=authorization.authorized_at)
        try:
            with self.database.transaction() as connection:
                agenda = connection.execute(
                    "SELECT owner_user_id FROM agenda_items WHERE id=?",
                    (stored.agenda_item_id,),
                ).fetchone()
                if agenda is None:
                    raise NotFoundError("agenda item not found")
                if str(agenda["owner_user_id"]) != stored.owner_user_id:
                    raise PermissionDenied("agenda notification owner mismatch")
                connection.execute(
                    """
                    INSERT INTO agenda_notification_rules(
                        id,agenda_item_id,owner_user_id,creator_user_id,target_ref,
                        time_of_day,day_offset,notification_text,timezone,action_links_json,
                        classification,enabled,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stored.id,
                        stored.agenda_item_id,
                        stored.owner_user_id,
                        stored.creator_user_id,
                        stored.target_ref,
                        stored.time_of_day.isoformat(timespec="minutes"),
                        stored.day_offset,
                        stored.text,
                        stored.timezone,
                        _dump_action_links(stored.action_links),
                        int(stored.classification),
                        int(stored.enabled),
                        _dump_datetime(stored.created_at),
                    ),
                )
                _audit(connection, authorization)
        except sqlite3.IntegrityError as exc:
            raise _raise_conflict(exc) from exc
        return stored

    def get(self, rule_id: str) -> AgendaNotificationRule | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agenda_notification_rules WHERE id=?", (rule_id,)
            ).fetchone()
        return None if row is None else self._from_row(row)

    def update(
        self,
        rule: AgendaNotificationRule,
        authorization: AuthorizedAction,
    ) -> AgendaNotificationRule:
        _require_authorization(
            authorization,
            action=Action.UPDATE,
            kind="agenda_notification",
            resource_id=rule.id,
            owner_user_id=rule.owner_user_id,
            classification=rule.classification,
        )
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE agenda_notification_rules SET
                    target_ref=?,time_of_day=?,day_offset=?,notification_text=?,
                    timezone=?,classification=?,enabled=?
                WHERE id=?
                """,
                (
                    rule.target_ref,
                    rule.time_of_day.isoformat(timespec="minutes"),
                    rule.day_offset,
                    rule.text,
                    rule.timezone,
                    int(rule.classification),
                    int(rule.enabled),
                    rule.id,
                ),
            ).rowcount
            if changed != 1:
                raise NotFoundError("agenda notification rule was not found")
            _audit(connection, authorization)
        return rule

    def delete(self, rule_id: str, authorization: AuthorizedAction) -> None:
        _require_authorization(
            authorization,
            action=Action.DELETE,
            kind="agenda_notification",
            resource_id=rule_id,
            owner_user_id=authorization.resource.owner_user_id,
            classification=authorization.resource.classification,
        )
        with self.database.transaction() as connection:
            changed = connection.execute(
                "DELETE FROM agenda_notification_rules WHERE id=?", (rule_id,)
            ).rowcount
            if changed != 1:
                raise NotFoundError("agenda notification rule was not found")
            _audit(connection, authorization)

    def list_enabled(self) -> list[AgendaNotificationRule]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agenda_notification_rules
                WHERE enabled=1 ORDER BY owner_user_id,agenda_item_id,id
                """
            ).fetchall()
        return [self._from_row(row) for row in rows]


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
            action_links=_load_action_links(row["action_links_json"]),
            status=ReminderStatus(str(row["status"])),
            missed_policy=MissedReminderPolicy(str(row["missed_policy"])),
            classification=DataClassification(int(row["classification"])),
            related_kind=row["related_kind"],
            related_id=row["related_id"],
            related_start_at=_load_datetime(row["related_start_at"]),
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
                        classification,related_kind,related_id,related_start_at,
                        action_links_json,
                        version,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                        (
                            _dump_datetime(stored.related_start_at)
                            if stored.related_start_at is not None
                            else None
                        ),
                        _dump_action_links(stored.action_links),
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
                                "label": link.label,
                                "action": link.url,
                                "kind": "open_url",
                            }
                            for link in reminder.action_links
                        ]
                        + [
                            {
                                "label": f"{minutes}分钟",
                                "action": (
                                    f"/提醒稍后 {reminder.id} {minutes}分钟"
                                ),
                                "kind": "command",
                            }
                            for minutes in (5, 15, 30, 60)
                        ]
                        + [
                            {
                                "label": "完成",
                                "action": f"/提醒完成 {reminder.id}",
                                "kind": "command",
                            },
                            {
                                "label": "取消",
                                "action": f"/取消提醒 {reminder.id}",
                                "kind": "command",
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


class AnniversaryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Anniversary:
        created_at = _load_datetime(str(row["created_at"]))
        assert created_at is not None
        return Anniversary(
            id=str(row["id"]),
            owner_user_id=str(row["owner_user_id"]),
            creator_user_id=str(row["creator_user_id"]),
            title=str(row["title"]),
            anchor_date=date.fromisoformat(str(row["anchor_date"])),
            timezone=str(row["timezone"]),
            kind=ImportantDayKind(str(row["kind"])),
            calendar=CalendarSystem(str(row["calendar"])),
            lunar_month=(
                int(row["lunar_month"]) if row["lunar_month"] is not None else None
            ),
            lunar_day=(
                int(row["lunar_day"]) if row["lunar_day"] is not None else None
            ),
            lunar_leap=bool(row["lunar_leap"]),
            advance_days=_load_advance_days(str(row["advance_days"])),
            classification=DataClassification(int(row["classification"])),
            created_at=created_at,
        )

    def create(
        self,
        anniversary: Anniversary,
        authorization: AuthorizedAction,
    ) -> Anniversary:
        _require_authorization(
            authorization,
            action=Action.CREATE,
            kind="anniversary",
            resource_id=anniversary.id,
            owner_user_id=anniversary.owner_user_id,
            classification=anniversary.classification,
        )
        stored = replace(anniversary, created_at=authorization.authorized_at)
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO anniversaries(
                        id,owner_user_id,creator_user_id,title,anchor_date,
                        timezone,classification,created_at,kind,calendar,
                        lunar_month,lunar_day,lunar_leap,advance_days
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stored.id,
                        stored.owner_user_id,
                        stored.creator_user_id,
                        stored.title,
                        stored.anchor_date.isoformat(),
                        stored.timezone,
                        int(stored.classification),
                        _dump_datetime(stored.created_at),
                        str(stored.kind),
                        str(stored.calendar),
                        stored.lunar_month,
                        stored.lunar_day,
                        int(stored.lunar_leap),
                        ",".join(str(value) for value in stored.advance_days),
                    ),
                )
                _audit(connection, authorization)
        except sqlite3.IntegrityError as exc:
            raise _raise_conflict(exc) from exc
        return stored

    def get(self, anniversary_id: str) -> Anniversary | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM anniversaries WHERE id=?", (anniversary_id,)
            ).fetchone()
        return None if row is None else self._from_row(row)

    def update(
        self,
        anniversary: Anniversary,
        authorization: AuthorizedAction,
    ) -> Anniversary:
        _require_authorization(
            authorization,
            action=Action.UPDATE,
            kind="anniversary",
            resource_id=anniversary.id,
            owner_user_id=anniversary.owner_user_id,
            classification=anniversary.classification,
        )
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE anniversaries SET
                    title=?,anchor_date=?,timezone=?,classification=?,kind=?,
                    calendar=?,lunar_month=?,lunar_day=?,lunar_leap=?,advance_days=?
                WHERE id=?
                """,
                (
                    anniversary.title,
                    anniversary.anchor_date.isoformat(),
                    anniversary.timezone,
                    int(anniversary.classification),
                    str(anniversary.kind),
                    str(anniversary.calendar),
                    anniversary.lunar_month,
                    anniversary.lunar_day,
                    int(anniversary.lunar_leap),
                    ",".join(str(value) for value in anniversary.advance_days),
                    anniversary.id,
                ),
            ).rowcount
            if changed != 1:
                raise NotFoundError("anniversary was not found")
            _audit(connection, authorization)
        return anniversary

    def delete(self, anniversary_id: str, authorization: AuthorizedAction) -> None:
        _require_authorization(
            authorization,
            action=Action.DELETE,
            kind="anniversary",
            resource_id=anniversary_id,
            owner_user_id=authorization.resource.owner_user_id,
            classification=authorization.resource.classification,
        )
        with self.database.transaction() as connection:
            changed = connection.execute(
                "DELETE FROM anniversaries WHERE id=?", (anniversary_id,)
            ).rowcount
            if changed != 1:
                raise NotFoundError("anniversary was not found")
            _audit(connection, authorization)

    def list_for_owner(self, owner_user_id: str) -> list[Anniversary]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM anniversaries WHERE owner_user_id=? ORDER BY anchor_date,id",
                (owner_user_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]


class DailyBriefingRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DailyBriefing:
        created_at = _load_datetime(str(row["created_at"]))
        updated_at = _load_datetime(str(row["updated_at"]))
        assert created_at is not None and updated_at is not None
        return DailyBriefing(
            id=str(row["id"]),
            owner_user_id=str(row["owner_user_id"]),
            creator_user_id=str(row["creator_user_id"]),
            target_ref=str(row["target_ref"]),
            time_of_day=time.fromisoformat(str(row["time_of_day"])),
            timezone=str(row["timezone"]),
            classification=DataClassification(int(row["classification"])),
            enabled=bool(row["enabled"]),
            last_sent_on=(
                date.fromisoformat(str(row["last_sent_on"]))
                if row["last_sent_on"] is not None
                else None
            ),
            created_at=created_at,
            updated_at=updated_at,
        )

    def create(
        self,
        briefing: DailyBriefing,
        authorization: AuthorizedAction,
    ) -> DailyBriefing:
        _require_authorization(
            authorization,
            action=Action.CREATE,
            kind="daily_briefing",
            resource_id=briefing.id,
            owner_user_id=briefing.owner_user_id,
            classification=briefing.classification,
        )
        stored = replace(
            briefing,
            created_at=authorization.authorized_at,
            updated_at=authorization.authorized_at,
        )
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO daily_briefings(
                        id,owner_user_id,creator_user_id,target_ref,time_of_day,
                        timezone,classification,enabled,last_sent_on,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stored.id,
                        stored.owner_user_id,
                        stored.creator_user_id,
                        stored.target_ref,
                        stored.time_of_day.isoformat(timespec="minutes"),
                        stored.timezone,
                        int(stored.classification),
                        int(stored.enabled),
                        None,
                        _dump_datetime(stored.created_at),
                        _dump_datetime(stored.updated_at),
                    ),
                )
                _audit(connection, authorization)
        except sqlite3.IntegrityError as exc:
            raise _raise_conflict(exc) from exc
        return stored

    def get(self, briefing_id: str) -> DailyBriefing | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM daily_briefings WHERE id=?", (briefing_id,)
            ).fetchone()
        return None if row is None else self._from_row(row)

    def update(
        self,
        briefing: DailyBriefing,
        authorization: AuthorizedAction,
        *,
        now: datetime,
    ) -> DailyBriefing:
        _require_authorization(
            authorization,
            action=Action.UPDATE,
            kind="daily_briefing",
            resource_id=briefing.id,
            owner_user_id=briefing.owner_user_id,
            classification=briefing.classification,
        )
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE daily_briefings SET
                    target_ref=?,time_of_day=?,timezone=?,classification=?,
                    enabled=?,updated_at=?
                WHERE id=?
                """,
                (
                    briefing.target_ref,
                    briefing.time_of_day.isoformat(timespec="minutes"),
                    briefing.timezone,
                    int(briefing.classification),
                    int(briefing.enabled),
                    _dump_datetime(now),
                    briefing.id,
                ),
            ).rowcount
            if changed != 1:
                raise NotFoundError("daily briefing was not found")
            _audit(connection, authorization)
        return replace(briefing, updated_at=now)

    def delete(self, briefing_id: str, authorization: AuthorizedAction) -> None:
        _require_authorization(
            authorization,
            action=Action.DELETE,
            kind="daily_briefing",
            resource_id=briefing_id,
            owner_user_id=authorization.resource.owner_user_id,
            classification=authorization.resource.classification,
        )
        with self.database.transaction() as connection:
            changed = connection.execute(
                "DELETE FROM daily_briefings WHERE id=?", (briefing_id,)
            ).rowcount
            if changed != 1:
                raise NotFoundError("daily briefing was not found")
            _audit(connection, authorization)

    def list_for_owner(self, owner_user_id: str) -> list[DailyBriefing]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM daily_briefings WHERE owner_user_id=? ORDER BY time_of_day,id",
                (owner_user_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def due(self, now: datetime) -> list[tuple[DailyBriefing, date]]:
        require_aware(now, "now")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM daily_briefings WHERE enabled=1 ORDER BY id"
            ).fetchall()
        result: list[tuple[DailyBriefing, date]] = []
        for row in rows:
            briefing = self._from_row(row)
            local = now.astimezone(ZoneInfo(briefing.timezone))
            if local.time().replace(tzinfo=None) < briefing.time_of_day:
                continue
            if briefing.last_sent_on == local.date():
                continue
            result.append((briefing, local.date()))
        return result

    def mark_sent(self, briefing_id: str, sent_on: date, now: datetime) -> None:
        require_aware(now, "now")
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE daily_briefings SET last_sent_on=?,updated_at=?
                WHERE id=? AND enabled=1
                """,
                (sent_on.isoformat(), _dump_datetime(now), briefing_id),
            )

    def default_target_for(self, owner_user_id: str) -> str | None:
        """Where this owner already receives scheduled pushes."""

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT target_ref FROM daily_briefings
                WHERE owner_user_id=? AND enabled=1
                ORDER BY time_of_day,id LIMIT 1
                """,
                (owner_user_id,),
            ).fetchone()
        return None if row is None else str(row["target_ref"])

    def target_channel(self, target_ref: str) -> tuple[str, str] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT channel,channel_account FROM channel_routes
                WHERE opaque_ref=? ORDER BY last_seen_at DESC LIMIT 1
                """,
                (target_ref,),
            ).fetchone()
        if row is None:
            return None
        return str(row["channel"]), str(row["channel_account"])


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


def _load_advance_days(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(",") if part.strip())


def _dump_lead_minutes(value: Sequence[int]) -> str:
    return ",".join(str(entry) for entry in value)


class NotificationLeadRepository:
    """Lead times in minutes before an occurrence starts.

    An owner keeps one default set. An agenda item may carry its own, and an
    item row storing an empty set means notifications were deliberately turned
    off for it rather than never configured.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def default_for(self, owner_user_id: str) -> tuple[int, ...] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT lead_minutes FROM notification_lead_defaults WHERE owner_user_id=?",
                (owner_user_id,),
            ).fetchone()
        return None if row is None else _load_advance_days(str(row["lead_minutes"]))

    def set_default(
        self,
        owner_user_id: str,
        lead_minutes: Sequence[int],
        *,
        now: datetime,
    ) -> tuple[int, ...]:
        normalised = normalise_lead_minutes(lead_minutes)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO notification_lead_defaults(
                    owner_user_id,lead_minutes,updated_at
                ) VALUES(?,?,?)
                ON CONFLICT(owner_user_id) DO UPDATE SET
                    lead_minutes=excluded.lead_minutes,updated_at=excluded.updated_at
                """,
                (owner_user_id, _dump_lead_minutes(normalised), _dump_datetime(now)),
            )
        return normalised

    def override_for(self, agenda_item_id: str) -> tuple[int, ...] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT lead_minutes FROM agenda_notification_leads WHERE agenda_item_id=?",
                (agenda_item_id,),
            ).fetchone()
        return None if row is None else _load_advance_days(str(row["lead_minutes"]))

    def set_override(
        self,
        agenda_item_id: str,
        owner_user_id: str,
        lead_minutes: Sequence[int],
        *,
        now: datetime,
    ) -> tuple[int, ...]:
        normalised = normalise_lead_minutes(lead_minutes)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO agenda_notification_leads(
                    agenda_item_id,owner_user_id,lead_minutes,updated_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(agenda_item_id) DO UPDATE SET
                    lead_minutes=excluded.lead_minutes,updated_at=excluded.updated_at
                """,
                (
                    agenda_item_id,
                    owner_user_id,
                    _dump_lead_minutes(normalised),
                    _dump_datetime(now),
                ),
            )
        return normalised

    def clear_override(self, agenda_item_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM agenda_notification_leads WHERE agenda_item_id=?",
                (agenda_item_id,),
            )

    def resolve(self, agenda_item_id: str, owner_user_id: str) -> tuple[int, ...]:
        override = self.override_for(agenda_item_id)
        if override is not None:
            return override
        default = self.default_for(owner_user_id)
        return DEFAULT_NOTIFICATION_LEAD_MINUTES if default is None else default
