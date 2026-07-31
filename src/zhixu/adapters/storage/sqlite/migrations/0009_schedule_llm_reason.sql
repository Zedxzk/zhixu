ALTER TABLE llm_call_events RENAME TO llm_call_events_v8;

CREATE TABLE llm_call_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model_ref TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (
        reason IN (
            'schedule_parse',
            'deterministic_parser_miss',
            'note_summary_requested',
            'general_question'
        )
    ),
    outcome TEXT NOT NULL CHECK (outcome IN ('reserved', 'completed', 'failed')),
    estimated_input_units INTEGER NOT NULL CHECK (estimated_input_units >= 0),
    output_units INTEGER NOT NULL DEFAULT 0 CHECK (output_units >= 0)
);

INSERT INTO llm_call_events(
    id,occurred_at,owner_user_id,model_ref,reason,outcome,
    estimated_input_units,output_units
)
SELECT
    id,occurred_at,owner_user_id,model_ref,reason,outcome,
    estimated_input_units,output_units
FROM llm_call_events_v8;

DROP TABLE llm_call_events_v8;

CREATE INDEX llm_call_events_owner_time
ON llm_call_events(owner_user_id, occurred_at DESC, id DESC);
