"""Автопубликация и сохранённые уведомления о выпусках."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, time
from pathlib import Path
from typing import Literal

from ..models import (
    AutomationStatus,
    EventData,
    Occurrence,
    PublicationChangeNotification,
    Record,
    ScheduleException,
    SchedulerNotice,
    SeriesVersion,
    WeeklyPost,
)
from .base import StorageBase

WeeklySnapshot = tuple[
    list[Record],
    list[SeriesVersion],
    list[ScheduleException],
    dict[int, Path | None],
]
NotificationOutcome = Literal["sent", "retry"]


def capture_weekly_snapshot(connection: sqlite3.Connection) -> WeeklySnapshot:
    records = connection.execute(
        "SELECT * FROM records WHERE status = 'active' ORDER BY id"
    ).fetchall()
    versions = connection.execute(
        "SELECT * FROM schedule_versions WHERE status = 'active' "
        "ORDER BY effective_from, series_id, revision"
    ).fetchall()
    exceptions = connection.execute(
        "SELECT * FROM schedule_exceptions WHERE archived = 0 ORDER BY original_date, series_id"
    ).fetchall()
    posters = connection.execute(
        "SELECT id, poster_path FROM sources WHERE id IN ("
        "SELECT poster_source_id FROM records "
        "WHERE status = 'active' AND poster_source_id IS NOT NULL "
        "UNION SELECT poster_source_id FROM schedule_versions "
        "WHERE status = 'active' AND poster_source_id IS NOT NULL) ORDER BY id"
    ).fetchall()

    poster_paths: dict[int, Path | None] = {}
    for row in posters:
        path_value = row["poster_path"]
        if path_value is not None and not isinstance(path_value, str):
            raise ValueError("Некорректный путь сохранённой афиши.")
        path = Path(path_value) if path_value is not None else None
        if path is not None and (path.is_absolute() or ".." in path.parts):
            raise ValueError("Путь афиши должен быть относительным.")
        poster_paths[row["id"]] = path

    return (
        [StorageBase._record_from_row(row) for row in records],
        [StorageBase._series_version_from_row(row) for row in versions],
        [StorageBase._schedule_exception_from_row(row) for row in exceptions],
        poster_paths,
    )


def _date_label(value: date) -> str:
    return f"{value.day:02d}.{value.month:02d}.{value.year:04d}"


def _field_value(value: object) -> str:
    if isinstance(value, date):
        return _date_label(value)
    if isinstance(value, time):
        return f"{value.hour:02d}:{value.minute:02d}"
    if value is None:
        return "не указано"
    if isinstance(value, bool):
        return "есть" if value else "нет"
    return str(value)


def _changed_fields(before: dict[str, object], after: dict[str, object]) -> list[str]:
    labels = {
        "title": "Название",
        "event_type": "Тип",
        "start_date": "Дата",
        "end_date": "Дата окончания",
        "start_time": "Время",
        "actual_date": "Фактическая дата",
        "place": "Место",
        "price": "Стоимость",
        "description": "Описание",
        "poster_source_id": "Афиша",
    }
    changes: list[str] = []
    for key, label in labels.items():
        if before.get(key) == after.get(key):
            continue
        if key == "poster_source_id":
            action = "заменена"
            if before.get(key) is None:
                action = "добавлена"
            elif after.get(key) is None:
                action = "удалена"
            changes.append(f"Афиша: {action}")
        else:
            changes.append(f"{label}: {_field_value(before[key])} → {_field_value(after[key])}")
    return changes


def _event_fields(data: EventData, poster_source_id: int | None) -> dict[str, object]:
    return {
        "title": data.title,
        "event_type": data.event_type,
        "start_date": data.start_date,
        "end_date": data.end_date,
        "start_time": data.start_time,
        "place": data.place,
        "price": data.price,
        "description": data.description,
        "poster_source_id": poster_source_id,
    }


def _occurrence_fields(item: Occurrence) -> dict[str, object]:
    return {
        "title": item.title,
        "actual_date": item.actual_date,
        "start_time": item.start_time,
        "place": item.place,
        "price": item.price,
        "description": item.description,
        "poster_source_id": item.poster_source_id,
    }


def describe_weekly_change(before: WeeklyPost, after: WeeklyPost) -> str:
    """Описать только пользовательские факты, изменившиеся в выпуске."""
    lines: list[str] = []
    before_records = {record.id: record for record in before.records}
    after_records = {record.id: record for record in after.records}
    for record_id in sorted(before_records.keys() | after_records.keys()):
        old = before_records.get(record_id)
        new = after_records.get(record_id)
        if old is None and new is not None and isinstance(new.data, EventData):
            lines.append(
                f"Добавлено событие «{new.data.title}» — {_date_label(new.data.start_date)}."
            )
        elif new is None and old is not None and isinstance(old.data, EventData):
            lines.append(
                f"Удалено событие «{old.data.title}» — {_date_label(old.data.start_date)}."
            )
        elif (
            old is not None
            and new is not None
            and isinstance(old.data, EventData)
            and isinstance(new.data, EventData)
        ):
            changes = _changed_fields(
                _event_fields(old.data, old.poster_source_id),
                _event_fields(new.data, new.poster_source_id),
            )
            if changes:
                lines.append(f"Событие «{new.data.title}»: " + "; ".join(changes) + ".")

    before_occurrences = {(item.series_id, item.original_date): item for item in before.occurrences}
    after_occurrences = {(item.series_id, item.original_date): item for item in after.occurrences}
    for occurrence_key in sorted(before_occurrences.keys() | after_occurrences.keys()):
        old = before_occurrences.get(occurrence_key)
        new = after_occurrences.get(occurrence_key)
        if old is None and new is not None:
            lines.append(f"Добавлено занятие «{new.title}» — {_date_label(new.actual_date)}.")
        elif new is None and old is not None:
            lines.append(
                f"Занятие «{old.title}» удалено из выпуска (было {_date_label(old.actual_date)})."
            )
        elif old is not None and new is not None:
            changes = _changed_fields(_occurrence_fields(old), _occurrence_fields(new))
            if changes:
                lines.append(f"Занятие «{new.title}»: " + "; ".join(changes) + ".")

    return "\n".join(lines) or "Изменилось содержимое выпуска."


def _message_link(group_id: int, message_id: int) -> str | None:
    group = str(group_id)
    if not group.startswith("-100"):
        return None
    return f"https://t.me/c/{group[4:]}/{message_id}"


def mark_published_week_changes(
    connection: sqlite3.Connection,
    before_snapshot: WeeklySnapshot,
    after_snapshot: WeeklySnapshot,
) -> None:
    """В той же транзакции пометить изменившиеся уже начатые выпуски."""
    from ..weekly import build_weekly_post_from_snapshot

    publications = connection.execute(
        "SELECT * FROM publications WHERE status IN ('sending', 'partial', 'unknown', 'delivered') "
        "ORDER BY id"
    ).fetchall()
    before = before_snapshot
    after = after_snapshot
    for publication in publications:
        week_start = date.fromisoformat(publication["week_start"])
        old_post = build_weekly_post_from_snapshot(week_start, *before)
        new_post = build_weekly_post_from_snapshot(week_start, *after)
        old_parts = tuple(part.model_dump_json() for part in old_post.parts)
        new_parts = tuple(part.model_dump_json() for part in new_post.parts)
        if old_parts == new_parts:
            continue

        revision = publication["notification_revision"] + 1
        body = [
            f"Опубликованный выпуск за неделю с {_date_label(week_start)} устарел.",
            describe_weekly_change(old_post, new_post),
            "Пост в группе не изменён. Сообщите об изменениях в группе вручную.",
        ]
        sent_parts = connection.execute(
            "SELECT message_id FROM publication_parts WHERE publication_id = ? "
            "AND status = 'sent' AND message_id IS NOT NULL ORDER BY part_index",
            (publication["id"],),
        ).fetchall()
        links = [
            link
            for part in sent_parts
            if (link := _message_link(publication["group_id"], part["message_id"])) is not None
        ]
        if links:
            body.append("Известные сообщения выпуска: " + ", ".join(links))
        connection.execute(
            "UPDATE publications SET notification_revision = ? WHERE id = ?",
            (revision, publication["id"]),
        )
        connection.execute(
            "INSERT INTO publication_notifications "
            "(publication_id, revision, message_text, status) VALUES (?, ?, ?, 'pending')",
            (publication["id"], revision, "\n\n".join(body)),
        )


class AutomationStorage(StorageBase):
    async def has_scheduler_notice(self, week_start: date, setting_revision: int = 0) -> bool:
        return await asyncio.to_thread(
            self._has_scheduler_notice_sync, week_start, setting_revision
        )

    async def save_scheduler_notice(
        self,
        week_start: date,
        kind: Literal["empty", "missed"],
        message_text: str,
        setting_revision: int = 0,
    ) -> None:
        await asyncio.to_thread(
            self._save_scheduler_notice_sync,
            week_start,
            kind,
            message_text,
            setting_revision,
        )

    async def claim_next_scheduler_notice(self) -> SchedulerNotice | None:
        return await asyncio.to_thread(self._claim_next_scheduler_notice_sync)

    async def finish_scheduler_notice(
        self,
        week_start: date,
        kind: Literal["empty", "missed"],
        outcome: NotificationOutcome,
        setting_revision: int = 0,
    ) -> None:
        await asyncio.to_thread(
            self._finish_scheduler_notice_sync,
            week_start,
            kind,
            outcome,
            setting_revision,
        )

    async def list_pending_publication_notifications(self) -> list[PublicationChangeNotification]:
        return await asyncio.to_thread(self._list_pending_publication_notifications_sync)

    async def claim_next_publication_notification(
        self,
    ) -> PublicationChangeNotification | None:
        return await asyncio.to_thread(self._claim_next_publication_notification_sync)

    async def acknowledge_publication_notification(
        self, publication_id: int, revision: int
    ) -> None:
        await asyncio.to_thread(
            self._finish_publication_notification_sync, publication_id, revision, "sent"
        )

    async def finish_publication_notification(
        self, publication_id: int, revision: int, outcome: NotificationOutcome
    ) -> None:
        await asyncio.to_thread(
            self._finish_publication_notification_sync, publication_id, revision, outcome
        )

    async def recover_automation_notices_on_startup(self) -> None:
        await asyncio.to_thread(self._recover_automation_notices_on_startup_sync)

    async def get_automation_status(self) -> AutomationStatus:
        return await asyncio.to_thread(self._get_automation_status_sync)

    def _save_scheduler_notice_sync(
        self,
        week_start: date,
        kind: Literal["empty", "missed"],
        message_text: str,
        setting_revision: int,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO scheduler_notices "
                "(week_start, kind, setting_revision, message_text, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (week_start.isoformat(), kind, setting_revision, message_text),
            )

    def _has_scheduler_notice_sync(self, week_start: date, setting_revision: int) -> bool:
        with self._connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM scheduler_notices "
                    "WHERE week_start = ? AND setting_revision = ? LIMIT 1",
                    (week_start.isoformat(), setting_revision),
                ).fetchone()
                is not None
            )

    def _claim_next_scheduler_notice_sync(self) -> SchedulerNotice | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_notices WHERE status = 'pending' "
                "ORDER BY week_start, kind LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                "UPDATE scheduler_notices SET status = 'sending' "
                "WHERE week_start = ? AND kind = ? AND setting_revision = ? "
                "AND status = 'pending'",
                (row["week_start"], row["kind"], row["setting_revision"]),
            )
            if updated.rowcount != 1:
                return None
            return self._scheduler_notice_from_row(row, status="sending")

    def _finish_scheduler_notice_sync(
        self,
        week_start: date,
        kind: Literal["empty", "missed"],
        outcome: NotificationOutcome,
        setting_revision: int,
    ) -> None:
        status = "sent" if outcome == "sent" else "pending"
        with self._transaction() as connection:
            connection.execute(
                "UPDATE scheduler_notices SET status = ? "
                "WHERE week_start = ? AND kind = ? AND setting_revision = ? "
                "AND status = 'sending'",
                (status, week_start.isoformat(), kind, setting_revision),
            )

    def _list_pending_publication_notifications_sync(self) -> list[PublicationChangeNotification]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT n.publication_id, n.revision, n.message_text "
                "FROM publication_notifications n "
                "JOIN publications p ON p.id = n.publication_id "
                "WHERE n.status = 'pending' AND n.revision = p.notified_revision + 1 "
                "ORDER BY n.publication_id, n.revision"
            ).fetchall()
            return [
                PublicationChangeNotification(
                    publication_id=row["publication_id"],
                    revision=row["revision"],
                    text=row["message_text"],
                )
                for row in rows
            ]

    def _claim_next_publication_notification_sync(
        self,
    ) -> PublicationChangeNotification | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT n.publication_id, n.revision, n.message_text "
                "FROM publication_notifications n "
                "JOIN publications p ON p.id = n.publication_id "
                "WHERE n.status = 'pending' AND n.revision = p.notified_revision + 1 "
                "ORDER BY n.publication_id, n.revision LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                "UPDATE publication_notifications SET status = 'sending' "
                "WHERE publication_id = ? AND revision = ? AND status = 'pending'",
                (row["publication_id"], row["revision"]),
            )
            if updated.rowcount != 1:
                return None
            return PublicationChangeNotification(
                publication_id=row["publication_id"],
                revision=row["revision"],
                text=row["message_text"],
            )

    def _finish_publication_notification_sync(
        self,
        publication_id: int,
        revision: int,
        outcome: NotificationOutcome,
    ) -> None:
        status = "sent" if outcome == "sent" else "pending"
        with self._transaction() as connection:
            publication = connection.execute(
                "SELECT notified_revision FROM publications WHERE id = ?", (publication_id,)
            ).fetchone()
            if publication is None:
                return
            if revision <= publication["notified_revision"]:
                return
            if revision != publication["notified_revision"] + 1:
                raise ValueError("Нельзя подтвердить уведомление в неправильном порядке.")
            changed = connection.execute(
                "UPDATE publication_notifications SET status = ? "
                "WHERE publication_id = ? AND revision = ? "
                "AND status IN ('pending', 'sending')",
                (status, publication_id, revision),
            )
            if changed.rowcount != 1:
                return
            if outcome == "sent":
                connection.execute(
                    "UPDATE publications SET notified_revision = ? "
                    "WHERE id = ? AND notified_revision = ?",
                    (revision, publication_id, revision - 1),
                )

    def _recover_automation_notices_on_startup_sync(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE publication_notifications SET status = 'pending' WHERE status = 'sending'"
            )
            connection.execute(
                "UPDATE scheduler_notices SET status = 'pending' WHERE status = 'sending'"
            )

    def _get_automation_status_sync(self) -> AutomationStatus:
        with self._connection() as connection:
            attachment_row = connection.execute(
                "SELECT COUNT(*) FROM sources WHERE attachment_status IN ('pending', 'failed')"
            ).fetchone()
            publication_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM publications GROUP BY status"
            ).fetchall()
            notification_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM publication_notifications GROUP BY status"
            ).fetchall()
            notice_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM scheduler_notices GROUP BY status"
            ).fetchall()
            return AutomationStatus(
                incomplete_attachments=attachment_row[0] if attachment_row is not None else 0,
                publications={row["status"]: row["count"] for row in publication_rows},
                pending_publication_notifications=sum(
                    row["count"] for row in notification_rows if row["status"] == "pending"
                ),
                pending_scheduler_notices=sum(
                    row["count"] for row in notice_rows if row["status"] == "pending"
                ),
            )

    @staticmethod
    def _scheduler_notice_from_row(
        row: sqlite3.Row, *, status: Literal["pending", "sending", "sent"] | None = None
    ) -> SchedulerNotice:
        return SchedulerNotice(
            week_start=date.fromisoformat(row["week_start"]),
            kind=row["kind"],
            text=row["message_text"],
            status=status if status is not None else row["status"],
            setting_revision=row["setting_revision"],
        )
