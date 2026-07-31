ALTER TABLE channel_routes
ADD COLUMN owner_user_id TEXT REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE channel_routes
ADD COLUMN group_mode TEXT NOT NULL DEFAULT 'disabled' CHECK (
    group_mode IN ('disabled', 'public', 'internal')
);

ALTER TABLE channel_routes
ADD COLUMN shared_owner_user_id TEXT REFERENCES users(id) ON DELETE SET NULL;

-- Existing group routes predate member ACLs. Fail closed until an administrator
-- explicitly chooses a mode and, for an internal group, its member list.
UPDATE channel_routes
SET commands_enabled = 0, group_mode = 'disabled'
WHERE route_kind = 'group';

CREATE TABLE channel_route_members (
    channel TEXT NOT NULL,
    channel_account TEXT NOT NULL,
    opaque_ref TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_by_user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(channel, channel_account, opaque_ref, user_id),
    FOREIGN KEY(channel, channel_account, opaque_ref)
        REFERENCES channel_routes(channel, channel_account, opaque_ref)
        ON DELETE CASCADE
);

CREATE INDEX channel_routes_group_mode
ON channel_routes(channel, channel_account, route_kind, group_mode);

CREATE INDEX channel_route_members_user
ON channel_route_members(user_id, channel, channel_account, opaque_ref);

-- Shared records belong to the group workspace principal while retaining the
-- human creator for attribution and audit. Existing private records fall back
-- to owner_user_id when this column is NULL.
ALTER TABLE agenda_items
ADD COLUMN creator_user_id TEXT REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE tasks
ADD COLUMN creator_user_id TEXT REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE notes
ADD COLUMN creator_user_id TEXT REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE reminders
ADD COLUMN creator_user_id TEXT REFERENCES users(id) ON DELETE SET NULL;
