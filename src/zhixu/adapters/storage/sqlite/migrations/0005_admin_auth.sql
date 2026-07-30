CREATE TABLE admin_credentials (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    password_hash TEXT NOT NULL CHECK (password_hash LIKE '$argon2id$%'),
    updated_at TEXT NOT NULL
);

CREATE TABLE admin_login_state (
    user_id TEXT PRIMARY KEY,
    window_started_at TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    locked_until TEXT
);
