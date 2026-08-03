CREATE TABLE note_categories (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES note_categories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (length(trim(name)) > 0),
    UNIQUE(owner_user_id, parent_id, name)
);

CREATE UNIQUE INDEX note_categories_unique_root
ON note_categories(owner_user_id, name)
WHERE parent_id IS NULL;

CREATE INDEX note_categories_owner_parent
ON note_categories(owner_user_id, parent_id, name);

ALTER TABLE notes ADD COLUMN category_id TEXT
REFERENCES note_categories(id) ON DELETE RESTRICT;

INSERT INTO note_categories(id, owner_user_id, parent_id, name, created_at)
SELECT
    'category_' || lower(hex(randomblob(16))),
    owner_user_id,
    NULL,
    '未分类',
    MIN(created_at)
FROM notes
GROUP BY owner_user_id;

UPDATE notes
SET category_id = (
    SELECT category.id
    FROM note_categories AS category
    WHERE category.owner_user_id = notes.owner_user_id
      AND category.parent_id IS NULL
      AND category.name = '未分类'
);

CREATE INDEX notes_owner_category_updated
ON notes(owner_user_id, category_id, updated_at DESC);

CREATE TABLE note_content_blocks (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL CHECK (position >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(trim(name)) > 0),
    UNIQUE(note_id, name)
);

CREATE INDEX note_content_blocks_note_position
ON note_content_blocks(note_id, position, id);

CREATE TABLE note_content_fields (
    block_id TEXT NOT NULL REFERENCES note_content_blocks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    CHECK (length(trim(name)) > 0),
    CHECK (length(trim(value)) > 0),
    PRIMARY KEY(block_id, name)
);

CREATE INDEX note_content_fields_block_position
ON note_content_fields(block_id, position, name);

INSERT INTO note_content_blocks(
    id, note_id, name, body, position, created_at, updated_at
)
SELECT
    'note_block_' || lower(hex(randomblob(16))),
    id,
    '默认内容',
    body,
    0,
    created_at,
    updated_at
FROM notes
WHERE length(trim(body)) > 0;
