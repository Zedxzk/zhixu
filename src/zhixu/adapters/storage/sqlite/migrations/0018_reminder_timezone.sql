-- A reminder is read in the timezone of the thing it is about, which is not
-- always the one the deployment happens to sit in. An absent value keeps the
-- previous behaviour for every reminder created before this column existed.
ALTER TABLE reminders ADD COLUMN timezone TEXT;
