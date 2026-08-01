CREATE TABLE anniversaries (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    creator_user_id TEXT NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    anchor_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    classification INTEGER NOT NULL CHECK (classification BETWEEN 0 AND 2),
    created_at TEXT NOT NULL
);

CREATE INDEX anniversaries_owner_date
ON anniversaries(owner_user_id, anchor_date, id);

CREATE TABLE daily_briefings (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    creator_user_id TEXT NOT NULL REFERENCES users(id),
    target_ref TEXT NOT NULL,
    time_of_day TEXT NOT NULL,
    timezone TEXT NOT NULL,
    classification INTEGER NOT NULL CHECK (classification BETWEEN 0 AND 2),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_sent_on TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX daily_briefings_due
ON daily_briefings(enabled, time_of_day, id);

CREATE TABLE agenda_notification_rules (
    id TEXT PRIMARY KEY,
    agenda_item_id TEXT NOT NULL REFERENCES agenda_items(id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    creator_user_id TEXT NOT NULL REFERENCES users(id),
    target_ref TEXT NOT NULL,
    time_of_day TEXT NOT NULL,
    day_offset INTEGER NOT NULL CHECK (day_offset BETWEEN -366 AND 366),
    notification_text TEXT NOT NULL,
    timezone TEXT NOT NULL,
    classification INTEGER NOT NULL CHECK (classification BETWEEN 0 AND 2),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX agenda_notification_rules_enabled
ON agenda_notification_rules(enabled, owner_user_id, agenda_item_id, id);

CREATE TABLE assistant_pending_plans (
    id TEXT PRIMARY KEY,
    actor_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_ref TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'revising', 'accepted')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX assistant_pending_plans_context
ON assistant_pending_plans(actor_user_id, target_ref, state, created_at);
