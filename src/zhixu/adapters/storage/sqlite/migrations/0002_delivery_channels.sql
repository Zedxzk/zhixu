ALTER TABLE outbox_deliveries
ADD COLUMN channel TEXT NOT NULL DEFAULT '';

ALTER TABLE outbox_deliveries
ADD COLUMN channel_account TEXT NOT NULL DEFAULT '';

ALTER TABLE outbox_deliveries
ADD COLUMN lease_owner TEXT NOT NULL DEFAULT '';

ALTER TABLE outbox_deliveries
ADD COLUMN lease_token TEXT NOT NULL DEFAULT '';

ALTER TABLE outbox_deliveries
ADD COLUMN lease_expires_at TEXT;

ALTER TABLE outbox_deliveries
ADD COLUMN provider_message_id TEXT NOT NULL DEFAULT '';

ALTER TABLE channel_contacts
ADD COLUMN commands_enabled INTEGER NOT NULL DEFAULT 0 CHECK (commands_enabled IN (0, 1));

CREATE TABLE inbound_event_receipts (
    channel TEXT NOT NULL,
    channel_account TEXT NOT NULL,
    event_id_hash TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    conversation_ref TEXT NOT NULL,
    intent_kind TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY(channel, channel_account, event_id_hash)
);

CREATE TABLE gateway_sessions (
    channel_account TEXT PRIMARY KEY REFERENCES channel_accounts(id) ON DELETE CASCADE,
    session_id_enc TEXT NOT NULL CHECK (session_id_enc LIKE 'enc:%'),
    resume_url_enc TEXT NOT NULL CHECK (resume_url_enc LIKE 'enc:%'),
    sequence INTEGER,
    updated_at TEXT NOT NULL
);

CREATE INDEX outbox_lease_due
ON outbox_deliveries(status, lease_expires_at, next_attempt_at, priority, created_at);
