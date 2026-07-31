INSERT OR IGNORE INTO users(id, display_name, status, created_at)
VALUES(
    'service:registration',
    'Private identity registration service',
    'active',
    strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
);

CREATE TABLE private_link_challenges (
    code_hash TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    channel_account TEXT NOT NULL,
    private_actor_ref TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX private_link_actor_active
ON private_link_challenges(
    channel,
    channel_account,
    private_actor_ref,
    expires_at,
    consumed_at
);
