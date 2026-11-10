CREATE TABLE schedule_versions (
    series_id INTEGER NOT NULL REFERENCES records(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision > 0),
    effective_from TEXT NOT NULL,
    effective_until TEXT,
    data_json TEXT NOT NULL,
    poster_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT,
    source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    PRIMARY KEY (series_id, revision)
);

CREATE INDEX schedule_versions_active_start
    ON schedule_versions (series_id, effective_from)
    WHERE status = 'active';

INSERT INTO schedule_versions (
    series_id, revision, effective_from, effective_until, data_json,
    poster_source_id, source_id, status
)
SELECT
    records.id,
    records.revision,
    json_extract(records.data_json, '$.first_date'),
    CASE
        WHEN json_extract(records.data_json, '$.last_date') IS NULL THEN NULL
        ELSE date(json_extract(records.data_json, '$.last_date'), '+1 day')
    END,
    records.data_json,
    records.poster_source_id,
    (
        SELECT drafts.source_id
        FROM drafts
        WHERE drafts.record_id = records.id AND drafts.status = 'confirmed'
        ORDER BY drafts.version DESC
        LIMIT 1
    ),
    'active'
FROM records
WHERE records.status = 'active' AND records.data_type = 'class';

CREATE TABLE schedule_exceptions (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES records(id) ON DELETE RESTRICT,
    original_date TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    cancelled INTEGER NOT NULL CHECK (cancelled IN (0, 1)),
    new_date TEXT,
    new_time TEXT,
    place TEXT,
    source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    UNIQUE (series_id, original_date),
    CHECK (
        (new_date IS NULL AND new_time IS NULL)
        OR (new_date IS NOT NULL AND new_time IS NOT NULL)
    ),
    CHECK (cancelled = 1 OR new_date IS NOT NULL)
);

CREATE TABLE schedule_exception_history (
    id INTEGER PRIMARY KEY,
    exception_id INTEGER NOT NULL,
    series_id INTEGER NOT NULL REFERENCES records(id) ON DELETE RESTRICT,
    original_date TEXT NOT NULL,
    revision INTEGER NOT NULL,
    cancelled INTEGER NOT NULL CHECK (cancelled IN (0, 1)),
    new_date TEXT,
    new_time TEXT,
    place TEXT,
    source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT,
    archived INTEGER NOT NULL CHECK (archived IN (0, 1)),
    changed_at TEXT NOT NULL
);

CREATE TABLE schedule_change_drafts (
    id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version > 0),
    series_id INTEGER NOT NULL REFERENCES records(id) ON DELETE RESTRICT,
    base_revision INTEGER NOT NULL CHECK (base_revision > 0),
    target_key TEXT NOT NULL,
    change_json TEXT NOT NULL,
    affected_exceptions_json TEXT NOT NULL,
    superseded_versions_json TEXT NOT NULL,
    base_exception_revision INTEGER,
    source_id INTEGER UNIQUE REFERENCES sources(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'superseded')),
    created_at TEXT NOT NULL,
    UNIQUE (series_id, target_key, version)
);

CREATE UNIQUE INDEX one_pending_schedule_change
    ON schedule_change_drafts (series_id, target_key)
    WHERE status = 'pending';

CREATE TABLE detached_occurrence_links (
    event_record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE RESTRICT,
    series_id INTEGER NOT NULL REFERENCES records(id) ON DELETE RESTRICT,
    original_date TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT,
    exception_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT,
    poster_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT,
    UNIQUE (series_id, original_date)
);

ALTER TABLE reply_bindings ADD COLUMN schedule_action TEXT;
ALTER TABLE reply_bindings ADD COLUMN schedule_series_id INTEGER;
ALTER TABLE reply_bindings ADD COLUMN schedule_revision INTEGER;
ALTER TABLE reply_bindings ADD COLUMN schedule_original_date TEXT;
ALTER TABLE reply_bindings ADD COLUMN expected_exception_revision INTEGER;
ALTER TABLE reply_bindings ADD COLUMN schedule_draft_id INTEGER;
ALTER TABLE reply_bindings ADD COLUMN schedule_draft_version INTEGER;

CREATE TABLE card_actions_v3 (
    token TEXT PRIMARY KEY,
    chat_id INTEGER,
    bot_message_id INTEGER,
    action TEXT NOT NULL CHECK (
        action IN (
            'view', 'confirm', 'edit', 'reject', 'cancel', 'retry_poster', 'remove_poster',
            'events_page', 'schedule_cancel', 'schedule_transfer', 'schedule_remove_exception',
            'schedule_change_series', 'schedule_end_series', 'schedule_confirm',
            'schedule_resolve_remove', 'schedule_resolve_detach', 'schedule_cancel_draft'
        )
    ),
    record_id INTEGER,
    draft_id INTEGER,
    version INTEGER,
    revision INTEGER,
    source_id INTEGER,
    page INTEGER,
    series_id INTEGER,
    schedule_revision INTEGER,
    original_date TEXT,
    expected_exception_revision INTEGER,
    schedule_draft_id INTEGER,
    schedule_draft_version INTEGER,
    remove_poster INTEGER NOT NULL DEFAULT 0 CHECK (remove_poster IN (0, 1)),
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
    outcome TEXT,
    FOREIGN KEY (record_id) REFERENCES records(id),
    FOREIGN KEY (draft_id) REFERENCES drafts(id),
    FOREIGN KEY (source_id) REFERENCES sources(id)
);

INSERT INTO card_actions_v3 (
    token, chat_id, bot_message_id, action, record_id, draft_id, version, revision,
    source_id, page, completed, outcome
)
SELECT
    token, chat_id, bot_message_id, action, record_id, draft_id, version, revision,
    source_id, page, completed, outcome
FROM card_actions;

DROP TABLE card_actions;
ALTER TABLE card_actions_v3 RENAME TO card_actions;
