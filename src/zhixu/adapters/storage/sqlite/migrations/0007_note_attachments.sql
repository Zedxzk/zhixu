CREATE TABLE note_attachments (
    note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (
        size_bytes >= 0 AND size_bytes <= 10737418240
    ),
    content_ref TEXT NOT NULL,
    PRIMARY KEY(note_id, id)
);

CREATE INDEX note_attachments_note ON note_attachments(note_id, id);
