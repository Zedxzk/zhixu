CREATE TABLE outbound_targets (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    channel_account TEXT NOT NULL,
    opaque_ref TEXT NOT NULL,
    target_enc TEXT NOT NULL CHECK (target_enc LIKE 'enc:%'),
    target_kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel,channel_account,opaque_ref)
);

CREATE INDEX outbound_targets_account
ON outbound_targets(channel,channel_account,opaque_ref);
