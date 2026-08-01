-- Generic user-supplied HTTPS actions for schedules and their notifications.
ALTER TABLE agenda_items
ADD COLUMN action_links_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE agenda_notification_rules
ADD COLUMN action_links_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE reminders
ADD COLUMN action_links_json TEXT NOT NULL DEFAULT '[]';
