ALTER TABLE identity_link_challenges
ADD COLUMN channel_account TEXT NOT NULL DEFAULT '';

ALTER TABLE identity_link_challenges
ADD COLUMN opaque_ref TEXT NOT NULL DEFAULT '';

ALTER TABLE identity_link_challenges
ADD COLUMN external_subject_enc TEXT NOT NULL DEFAULT '' CHECK (
    external_subject_enc = '' OR external_subject_enc LIKE 'enc:%'
);

ALTER TABLE identity_link_challenges
ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0);

CREATE TABLE admin_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    authentication TEXT NOT NULL CHECK (
        authentication IN ('password','mfa','step_up')
    ),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE channel_sessions (
    id TEXT PRIMARY KEY,
    external_identity_id TEXT NOT NULL REFERENCES external_identities(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    channel_account TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX admin_sessions_user_active
ON admin_sessions(user_id,expires_at,revoked_at);

CREATE INDEX channel_sessions_identity_active
ON channel_sessions(external_identity_id,expires_at,revoked_at);
