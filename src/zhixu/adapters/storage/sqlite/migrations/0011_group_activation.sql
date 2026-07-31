INSERT OR IGNORE INTO roles(id, description)
VALUES('project_admin', 'May register and manage messaging groups');

-- Existing single-user installations have one human principal. Shared group
-- workspace principals are excluded. Multi-user installations fail closed and
-- require an explicit role assignment.
WITH eligible AS (
    SELECT users.id, users.created_at
    FROM users
    WHERE users.status='active'
      AND NOT EXISTS (
          SELECT 1 FROM channel_routes
          WHERE channel_routes.shared_owner_user_id=users.id
      )
)
INSERT OR IGNORE INTO role_bindings(user_id, role_id, created_at)
SELECT id, 'project_admin', created_at
FROM eligible
WHERE (SELECT COUNT(*) FROM eligible)=1;

CREATE TABLE group_activation_challenges (
    code_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    channel_account TEXT NOT NULL,
    group_mode TEXT NOT NULL CHECK (group_mode IN ('public','internal')),
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX group_activation_user_active
ON group_activation_challenges(user_id, expires_at, consumed_at);
