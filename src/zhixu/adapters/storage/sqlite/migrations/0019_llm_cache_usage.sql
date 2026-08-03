ALTER TABLE llm_call_events
ADD COLUMN input_units INTEGER NOT NULL DEFAULT 0 CHECK (input_units >= 0);

ALTER TABLE llm_call_events
ADD COLUMN cached_input_units INTEGER NOT NULL DEFAULT 0 CHECK (cached_input_units >= 0);
