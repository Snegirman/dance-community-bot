CREATE TABLE detached_occurrence_links_v4 (
    event_record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE RESTRICT,
    series_id INTEGER NOT NULL REFERENCES records(id) ON DELETE RESTRICT,
    original_date TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT,
    exception_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT,
    poster_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT
);

INSERT INTO detached_occurrence_links_v4 (
    event_record_id, series_id, original_date, source_id, exception_source_id, poster_source_id
)
SELECT
    event_record_id, series_id, original_date, source_id, exception_source_id, poster_source_id
FROM detached_occurrence_links;

DROP TABLE detached_occurrence_links;

ALTER TABLE detached_occurrence_links_v4 RENAME TO detached_occurrence_links;
