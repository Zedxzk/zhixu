-- A lead reminder fires before the thing it announces, so the moment it fires
-- is not the moment the reader cares about. Carry the occurrence start so the
-- notification can state when the event actually begins.
ALTER TABLE reminders ADD COLUMN related_start_at TEXT;
