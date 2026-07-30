CREATE TABLE vault_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE key_versions (
    version INTEGER PRIMARY KEY,
    wrapped_master_key TEXT NOT NULL CHECK (wrapped_master_key LIKE 'enc:v1:%'),
    status TEXT NOT NULL CHECK (status IN ('active','retired')),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX one_active_key
ON key_versions(status) WHERE status='active';

CREATE TABLE secret_records (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    label TEXT NOT NULL,
    secret_kind TEXT NOT NULL CHECK (secret_kind IN ('machine','human')),
    classification TEXT NOT NULL CHECK (
        classification IN ('l3_machine','l3_human')
    ),
    ciphertext TEXT NOT NULL CHECK (ciphertext LIKE 'enc:v1:%'),
    wrapped_data_key TEXT NOT NULL CHECK (wrapped_data_key LIKE 'enc:v1:%'),
    key_version INTEGER NOT NULL REFERENCES key_versions(version),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE secret_acl (
    secret_id TEXT NOT NULL REFERENCES secret_records(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(secret_id,subject,action)
);

CREATE TABLE service_principals (
    id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE grant_nonces (
    nonce_hash TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    secret_id TEXT NOT NULL,
    action TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT NOT NULL
);

CREATE TABLE webauthn_credentials (
    credential_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    public_key TEXT NOT NULL,
    sign_count INTEGER NOT NULL,
    transports_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE webauthn_challenges (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('registration','authentication')),
    challenge TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE vault_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    secret_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    previous_mac TEXT NOT NULL,
    event_mac TEXT NOT NULL UNIQUE
);
