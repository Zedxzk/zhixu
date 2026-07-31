CREATE TABLE secret_policy_overrides (
    secret_id TEXT PRIMARY KEY
        REFERENCES secret_records(id) ON DELETE CASCADE,
    policy TEXT NOT NULL CHECK (
        policy = 'owner_explicit_human_storage'
    ),
    created_at TEXT NOT NULL
);
