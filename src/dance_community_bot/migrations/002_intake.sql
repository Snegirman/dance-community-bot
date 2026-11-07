ALTER TABLE sources ADD COLUMN reply_to_message_id INTEGER;
ALTER TABLE sources ADD COLUMN is_photo INTEGER NOT NULL DEFAULT 0 CHECK (is_photo IN (0, 1));
ALTER TABLE sources ADD COLUMN attachment_status TEXT NOT NULL DEFAULT 'none'
    CHECK (attachment_status IN ('none', 'pending', 'saved', 'failed', 'removed'));
UPDATE sources
SET attachment_status = CASE
    WHEN poster_path IS NOT NULL THEN 'saved'
    WHEN telegram_file_id IS NOT NULL THEN 'pending'
    ELSE 'none'
END;

CREATE TABLE reply_bindings (
    chat_id INTEGER NOT NULL,
    bot_message_id INTEGER NOT NULL,
    record_id INTEGER,
    draft_id INTEGER,
    version INTEGER,
    revision INTEGER,
    source_id INTEGER,
    page INTEGER,
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
        action IN ('view', 'confirm', 'edit', 'reject', 'cancel', 'retry_poster', 'remove_poster', 'events_page')
    ),
    record_id INTEGER,
    draft_id INTEGER,
    version INTEGER,
    revision INTEGER,
    source_id INTEGER,
    page INTEGER,
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
    outcome TEXT,
    FOREIGN KEY (record_id) REFERENCES records(id),
    FOREIGN KEY (draft_id) REFERENCES drafts(id),
    FOREIGN KEY (source_id) REFERENCES sources(id)
);
