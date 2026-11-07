CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    text TEXT,
    received_at TEXT NOT NULL,
    telegram_file_id TEXT,
    media_group_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processed', 'failed')),
    error_reason TEXT,
    poster_path TEXT,
    UNIQUE (chat_id, message_id)
);

CREATE TABLE records (
    id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL,
    data_type TEXT NOT NULL CHECK (data_type IN ('event', 'class')),
    data_json TEXT NOT NULL,
    poster_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'cancelled'))
);

CREATE TABLE drafts (
    id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    base_revision INTEGER,
    data_type TEXT NOT NULL CHECK (data_type IN ('event', 'class')),
    data_json TEXT NOT NULL,
    source_id INTEGER NOT NULL UNIQUE REFERENCES sources(id) ON DELETE RESTRICT,
    poster_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'confirmed', 'rejected', 'superseded')),
    UNIQUE (record_id, version)
);

CREATE UNIQUE INDEX one_pending_draft_per_record
    ON drafts (record_id)
    WHERE status = 'pending';
