CREATE TABLE qq_reply_contexts (
    channel_account TEXT NOT NULL REFERENCES channel_accounts(id) ON DELETE CASCADE,
    opaque_ref TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    context_kind TEXT NOT NULL CHECK (context_kind IN ('msg_id','event_id')),
    external_context_enc TEXT NOT NULL CHECK (external_context_enc LIKE 'enc:%'),
    received_at TEXT NOT NULL,
    PRIMARY KEY(channel_account, opaque_ref)
);

CREATE INDEX qq_reply_context_expiry
ON qq_reply_contexts(channel_account, received_at);
