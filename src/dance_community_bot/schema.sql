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
    reply_to_message_id INTEGER,
    is_photo INTEGER NOT NULL DEFAULT 0 CHECK (is_photo IN (0, 1)),
    attachment_status TEXT NOT NULL DEFAULT 'none'
        CHECK (attachment_status IN ('none', 'pending', 'saved', 'failed', 'removed')),
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

CREATE TABLE reply_bindings (
    chat_id INTEGER NOT NULL,
    bot_message_id INTEGER NOT NULL,
    record_id INTEGER,
    draft_id INTEGER,
    version INTEGER,
    revision INTEGER,
    source_id INTEGER,
    page INTEGER,
    schedule_action TEXT,
    schedule_series_id INTEGER,
    schedule_revision INTEGER,
    schedule_original_date TEXT,
    expected_exception_revision INTEGER,
    schedule_draft_id INTEGER,
    schedule_draft_version INTEGER,
    PRIMARY KEY (chat_id, bot_message_id),
    FOREIGN KEY (record_id) REFERENCES records(id),
    FOREIGN KEY (draft_id) REFERENCES drafts(id),
    FOREIGN KEY (source_id) REFERENCES sources(id)
);

CREATE TABLE card_actions (
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
    poster_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT
);

CREATE TABLE publications (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    catalog_revision TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'sending', 'partial', 'unknown', 'delivered')
    ),
    preview_revision INTEGER NOT NULL CHECK (preview_revision > 0),
    notification_revision INTEGER NOT NULL DEFAULT 0 CHECK (notification_revision >= 0),
    notified_revision INTEGER NOT NULL DEFAULT 0 CHECK (notified_revision >= 0),
    owner_chat_id INTEGER,
    owner_message_id INTEGER,
    active_token TEXT,
    UNIQUE (group_id, week_start)
);

CREATE TABLE publication_parts (
    publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL CHECK (part_index >= 0),
    part_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'sending', 'sent', 'failed', 'unknown')
    ),
    attempt_revision INTEGER NOT NULL DEFAULT 0 CHECK (attempt_revision >= 0),
    message_id INTEGER,
    PRIMARY KEY (publication_id, part_index)
);

CREATE INDEX publication_parts_by_status
    ON publication_parts (publication_id, status, part_index);

CREATE TABLE publication_notifications (
    publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision > 0),
    message_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'sent')),
    PRIMARY KEY (publication_id, revision)
);

CREATE INDEX publication_notifications_by_status
    ON publication_notifications (status, publication_id, revision);

CREATE TABLE scheduler_notices (
    week_start TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('empty', 'missed')),
    setting_revision INTEGER NOT NULL DEFAULT 0 CHECK (setting_revision >= 0),
    message_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'sent')),
    PRIMARY KEY (week_start, kind, setting_revision)
);

CREATE INDEX scheduler_notices_by_status
    ON scheduler_notices (status, week_start);

CREATE TABLE weekly_publication_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    publication_time TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    changed_at TEXT
);

CREATE TABLE weekly_publication_prompts (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'consumed', 'superseded')),
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX weekly_publication_prompts_by_status
    ON weekly_publication_prompts (chat_id, status, revision);
