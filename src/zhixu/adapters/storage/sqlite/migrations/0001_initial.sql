CREATE TABLE users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL
);

CREATE TABLE external_identities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    channel_account TEXT NOT NULL,
    external_subject_enc TEXT NOT NULL CHECK (external_subject_enc LIKE 'enc:%'),
    opaque_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(channel, channel_account, opaque_ref)
);

CREATE TABLE identity_link_challenges (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    challenge_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE roles (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE role_bindings (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, role_id)
);

CREATE TABLE resource_acl (
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    subject_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    granted_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(resource_kind, resource_id, subject_user_id, action)
);

CREATE TABLE agenda_items (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    all_day INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0, 1)),
    classification INTEGER NOT NULL CHECK (classification BETWEEN 0 AND 2),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX agenda_owner_start ON agenda_items(owner_user_id, start_at);

CREATE TABLE recurrence_rules (
    agenda_item_id TEXT PRIMARY KEY REFERENCES agenda_items(id) ON DELETE CASCADE,
    rule_text TEXT NOT NULL,
    timezone TEXT NOT NULL
);

CREATE TABLE recurrence_exceptions (
    agenda_item_id TEXT NOT NULL REFERENCES agenda_items(id) ON DELETE CASCADE,
    occurrence_at TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('cancel', 'replace')),
    replacement_start TEXT,
    replacement_end TEXT,
    PRIMARY KEY(agenda_item_id, occurrence_at),
    CHECK (
        (action = 'cancel' AND replacement_start IS NULL AND replacement_end IS NULL)
        OR
        (action = 'replace' AND replacement_start IS NOT NULL AND replacement_end IS NOT NULL)
    )
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'in_progress', 'completed', 'cancelled', 'archived')
    ),
    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 4),
    due_at TEXT,
    classification INTEGER NOT NULL CHECK (classification BETWEEN 0 AND 2),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX tasks_owner_status_due ON tasks(owner_user_id, status, due_at);

CREATE TABLE notes (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    classification INTEGER NOT NULL CHECK (classification BETWEEN 0 AND 2),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(trim(title)) > 0 OR length(trim(body)) > 0)
);

CREATE VIRTUAL TABLE notes_fts USING fts5(
    note_id UNINDEXED,
    owner_user_id UNINDEXED,
    title,
    body,
    tokenize = 'unicode61'
);

CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    UNIQUE(owner_user_id, name)
);

CREATE TABLE resource_tags (
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY(resource_kind, resource_id, tag_id)
);

CREATE TABLE reminders (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    fire_at TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'fired', 'cancelled')),
    missed_policy TEXT NOT NULL CHECK (missed_policy IN ('fire', 'skip')),
    classification INTEGER NOT NULL CHECK (classification BETWEEN 0 AND 2),
    related_kind TEXT,
    related_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((related_kind IS NULL) = (related_id IS NULL))
);
CREATE INDEX reminders_due ON reminders(status, fire_at);

CREATE TABLE scheduled_jobs (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_kind TEXT NOT NULL,
    schedule_spec TEXT NOT NULL,
    timezone TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE job_runs (
    id TEXT PRIMARY KEY,
    scheduled_job_id TEXT NOT NULL REFERENCES scheduled_jobs(id) ON DELETE CASCADE,
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error_code TEXT NOT NULL DEFAULT '',
    UNIQUE(scheduled_job_id, scheduled_for)
);

CREATE TABLE channel_accounts (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    label TEXT NOT NULL,
    config_ref TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE channel_contacts (
    id TEXT PRIMARY KEY,
    channel_account_id TEXT NOT NULL REFERENCES channel_accounts(id) ON DELETE CASCADE,
    opaque_ref TEXT NOT NULL,
    external_target_enc TEXT NOT NULL CHECK (external_target_enc LIKE 'enc:%'),
    kind TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(channel_account_id, opaque_ref)
);

CREATE TABLE outbox_deliveries (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_ref TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    classification INTEGER NOT NULL CHECK (classification BETWEEN 0 AND 2),
    priority INTEGER NOT NULL DEFAULT 30,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at TEXT NOT NULL,
    last_error_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX outbox_due ON outbox_deliveries(status, next_attempt_at, priority, created_at);

CREATE TABLE dead_letters (
    id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL UNIQUE REFERENCES outbox_deliveries(id),
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retried_at TEXT
);

CREATE TABLE quota_usage (
    scope_kind TEXT NOT NULL,
    scope_ref TEXT NOT NULL,
    window_kind TEXT NOT NULL,
    window_start TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(scope_kind, scope_ref, window_kind, window_start)
);

CREATE TABLE llm_usage (
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model_ref TEXT NOT NULL,
    window_kind TEXT NOT NULL,
    window_start TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    input_units INTEGER NOT NULL DEFAULT 0,
    output_units INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(owner_user_id, model_ref, window_kind, window_start)
);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor_user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT ''
);
CREATE INDEX audit_resource ON audit_events(resource_kind, resource_id, occurred_at);
