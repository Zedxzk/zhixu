-- Birthdays recur like anniversaries but mark a date rather than accumulate a
-- day count, and may be kept on the lunisolar calendar, whose Gregorian month
-- and day move every year.
ALTER TABLE anniversaries ADD COLUMN kind TEXT NOT NULL DEFAULT 'anniversary'
    CHECK (kind IN ('anniversary', 'birthday'));
ALTER TABLE anniversaries ADD COLUMN calendar TEXT NOT NULL DEFAULT 'solar'
    CHECK (calendar IN ('solar', 'lunar'));
ALTER TABLE anniversaries ADD COLUMN lunar_month INTEGER
    CHECK (lunar_month IS NULL OR lunar_month BETWEEN 1 AND 12);
ALTER TABLE anniversaries ADD COLUMN lunar_day INTEGER
    CHECK (lunar_day IS NULL OR lunar_day BETWEEN 1 AND 30);
ALTER TABLE anniversaries ADD COLUMN lunar_leap INTEGER NOT NULL DEFAULT 0
    CHECK (lunar_leap IN (0, 1));
-- Comma-separated days before the occurrence that get an advance notice.
ALTER TABLE anniversaries ADD COLUMN advance_days TEXT NOT NULL DEFAULT '';

-- Lead times are minutes before an occurrence starts. An owner keeps one
-- default set; an agenda item may carry its own, and an empty override string
-- means the item was deliberately silenced.
CREATE TABLE notification_lead_defaults (
    owner_user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    lead_minutes TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE agenda_notification_leads (
    agenda_item_id TEXT PRIMARY KEY REFERENCES agenda_items(id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lead_minutes TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX agenda_notification_leads_owner
ON agenda_notification_leads(owner_user_id, agenda_item_id);
