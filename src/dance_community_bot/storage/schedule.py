"""Изменения расписания и история исключений."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from ..models import (
    CancelOccurrence,
    ChangeDraft,
    ChangeSeries,
    EndSeries,
    EventData,
    OccurrenceChange,
    RemoveException,
    ScheduleChange,
    ScheduleException,
    SeriesVersion,
    TransferOccurrence,
)
from .automation import capture_weekly_snapshot, mark_published_week_changes
from .base import StorageBase

_SCHEDULE_CHANGE_ADAPTER = TypeAdapter(ScheduleChange)
_SCHEDULE_EXCEPTIONS_ADAPTER = TypeAdapter(list[ScheduleException])
_SERIES_VERSIONS_ADAPTER = TypeAdapter(list[SeriesVersion])
_MOSCOW = ZoneInfo("Europe/Moscow")


class ScheduleStorage(StorageBase):
    async def list_pending_schedule_changes(self) -> list[ChangeDraft]:
        return await asyncio.to_thread(self._list_pending_schedule_changes_sync)

    async def get_schedule_exception_revision(
        self, series_id: int, original_date: date
    ) -> int | None:
        return await asyncio.to_thread(
            self._get_schedule_exception_revision_sync, series_id, original_date
        )

    async def propose_schedule_change(self, change: ScheduleChange, now: datetime) -> ChangeDraft:
        return await asyncio.to_thread(self._propose_schedule_change_sync, change, now)

    async def resolve_schedule_change(
        self,
        draft_id: int,
        version: int,
        *,
        original_date: date,
        resolution: Literal["remove", "detach"],
    ) -> ChangeDraft:
        return await asyncio.to_thread(
            self._resolve_schedule_change_sync,
            draft_id,
            version,
            original_date,
            resolution,
        )

    async def confirm_schedule_change(self, draft_id: int, version: int, now: datetime) -> None:
        await asyncio.to_thread(self._confirm_schedule_change_sync, draft_id, version, now)

    async def discard_schedule_change(self, draft_id: int, version: int) -> None:
        await asyncio.to_thread(self._discard_schedule_change_sync, draft_id, version)

    def _list_pending_schedule_changes_sync(self) -> list[ChangeDraft]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM schedule_change_drafts WHERE status = 'pending' ORDER BY id"
            ).fetchall()
            return [self._schedule_draft_from_row(row) for row in rows]

    def _get_schedule_exception_revision_sync(
        self, series_id: int, original_date: date
    ) -> int | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT revision FROM schedule_exceptions "
                "WHERE series_id = ? AND original_date = ?",
                (series_id, original_date.isoformat()),
            ).fetchone()
            return row["revision"] if row is not None else None

    def _propose_schedule_change_sync(self, change: ScheduleChange, now: datetime) -> ChangeDraft:
        now = self._aware_moscow_now(now)
        change_json = change.model_dump_json()
        target_key = (
            f"occurrence:{change.original_date.isoformat()}"
            if isinstance(change, OccurrenceChange)
            else "series"
        )
        with self._transaction() as connection:
            if change.source_id is not None:
                if self._source_row(connection, change.source_id) is None:
                    raise ValueError("Неизвестный источник изменения расписания.")
                prior = connection.execute(
                    "SELECT * FROM schedule_change_drafts WHERE source_id = ?",
                    (change.source_id,),
                ).fetchone()
                if prior is not None:
                    prior_change = _SCHEDULE_CHANGE_ADAPTER.validate_json(prior["change_json"])
                    same_change = prior_change.model_dump_json() == change_json
                    if isinstance(prior_change, (ChangeSeries, EndSeries)) and isinstance(
                        change, (ChangeSeries, EndSeries)
                    ):
                        same_change = (
                            prior_change.model_copy(update={"resolutions": {}}).model_dump_json()
                            == change.model_copy(update={"resolutions": {}}).model_dump_json()
                        )
                    if not same_change:
                        raise ValueError("Источник уже связан с другим изменением расписания.")
                    return self._schedule_draft_from_row(prior)

            record = connection.execute(
                "SELECT * FROM records WHERE id = ? AND status = 'active' AND data_type = 'class'",
                (change.series_id,),
            ).fetchone()
            if record is None:
                raise ValueError("Неизвестная или неактивная серия занятий.")
            if record["revision"] != change.expected_revision:
                raise ValueError("Ревизия расписания устарела.")

            base_exception_revision: int | None = None
            if isinstance(change, OccurrenceChange):
                version = self._version_on_date(connection, change.series_id, change.original_date)
                if version is None:
                    raise ValueError("На эту дату нет регулярного проведения.")
                exception_row = self._exception_row(
                    connection, change.series_id, change.original_date
                )
                base_exception_revision = (
                    exception_row["revision"] if exception_row is not None else None
                )
                if change.expected_exception_revision != base_exception_revision:
                    raise ValueError("Ревизия исключения устарела.")
                if isinstance(change, RemoveException) and (
                    exception_row is None or exception_row["archived"]
                ):
                    raise ValueError("Для этой даты нет активного исключения.")
            else:
                self._validate_series_change_date(change, now)

            affected: list[ScheduleException] = []
            superseded: list[SeriesVersion] = []
            if isinstance(change, (ChangeSeries, EndSeries)):
                affected = self._affected_exceptions(connection, change)
                superseded = self._versions_superseded_by(connection, change)
                unresolved_dates = {item.original_date for item in affected}
                if set(change.resolutions) - unresolved_dates:
                    raise ValueError("Решение задано для незатронутого исключения.")
                for item in affected:
                    if change.resolutions.get(item.original_date) == "detach" and item.cancelled:
                        raise ValueError("Отменённое проведение нельзя отделить от серии.")

            previous = connection.execute(
                "SELECT id FROM schedule_change_drafts "
                "WHERE series_id = ? AND target_key = ? AND status = 'pending'",
                (change.series_id, target_key),
            ).fetchone()
            if previous is not None:
                connection.execute(
                    "UPDATE schedule_change_drafts SET status = 'superseded' "
                    "WHERE series_id = ? AND target_key = ? AND status = 'pending'",
                    (change.series_id, target_key),
                )
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
                "FROM schedule_change_drafts WHERE series_id = ? AND target_key = ?",
                (change.series_id, target_key),
            ).fetchone()
            if version_row is None or not isinstance(version_row["next_version"], int):
                raise RuntimeError("Не удалось определить версию изменения расписания.")
            draft_version = version_row["next_version"]
            cursor = connection.execute(
                "INSERT INTO schedule_change_drafts "
                "(version, series_id, base_revision, target_key, change_json, "
                "affected_exceptions_json, superseded_versions_json, base_exception_revision, "
                "source_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    draft_version,
                    change.series_id,
                    change.expected_revision,
                    target_key,
                    change_json,
                    _SCHEDULE_EXCEPTIONS_ADAPTER.dump_json(affected).decode("utf-8"),
                    _SERIES_VERSIONS_ADAPTER.dump_json(superseded).decode("utf-8"),
                    base_exception_revision,
                    change.source_id,
                    now.isoformat(),
                ),
            )
            draft_id = cursor.lastrowid
            if draft_id is None:
                raise RuntimeError("Не удалось создать изменение расписания.")
            if change.source_id is not None:
                connection.execute(
                    "UPDATE sources SET status = 'processed', error_reason = NULL WHERE id = ?",
                    (change.source_id,),
                )
            row = connection.execute(
                "SELECT * FROM schedule_change_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("Изменение расписания не сохранилось.")
            return self._schedule_draft_from_row(row)

    def _resolve_schedule_change_sync(
        self,
        draft_id: int,
        version: int,
        original_date: date,
        resolution: Literal["remove", "detach"],
    ) -> ChangeDraft:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM schedule_change_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Неизвестное изменение расписания.")
            if row["status"] != "pending":
                raise ValueError("Подтверждение изменения устарело.")
            change = _SCHEDULE_CHANGE_ADAPTER.validate_json(row["change_json"])
            if not isinstance(change, (ChangeSeries, EndSeries)):
                raise ValueError("Для этого изменения решения не требуются.")
            affected = _SCHEDULE_EXCEPTIONS_ADAPTER.validate_json(row["affected_exceptions_json"])
            exception = next(
                (item for item in affected if item.original_date == original_date), None
            )
            if exception is None:
                raise ValueError("Исключение не относится к этому изменению серии.")
            if resolution == "detach" and exception.cancelled:
                raise ValueError("Отменённое проведение нельзя отделить от серии.")
            resolutions = dict(change.resolutions)
            if version <= 0 or row["version"] < version:
                raise ValueError("Подтверждение изменения устарело.")
            if resolutions.get(original_date) == resolution:
                return self._schedule_draft_from_row(row)
            if row["version"] != version:
                raise ValueError("Подтверждение изменения устарело.")
            resolutions[original_date] = resolution
            updated_change = change.model_copy(update={"resolutions": resolutions})
            next_version = version + 1
            connection.execute(
                "UPDATE schedule_change_drafts SET version = ?, change_json = ? "
                "WHERE id = ? AND version = ? AND status = 'pending'",
                (next_version, updated_change.model_dump_json(), draft_id, version),
            )
            updated = connection.execute(
                "SELECT * FROM schedule_change_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if updated is None:
                raise RuntimeError("Изменение расписания не найдено после решения.")
            return self._schedule_draft_from_row(updated)

    def _confirm_schedule_change_sync(self, draft_id: int, version: int, now: datetime) -> None:
        now = self._aware_moscow_now(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM schedule_change_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Неизвестное изменение расписания.")
            if row["version"] != version:
                raise ValueError("Версия изменения расписания устарела.")
            if row["status"] == "confirmed":
                return
            if row["status"] != "pending":
                raise ValueError("Изменение расписания уже заменено.")

            change = _SCHEDULE_CHANGE_ADAPTER.validate_json(row["change_json"])
            record = connection.execute(
                "SELECT * FROM records WHERE id = ? AND status = 'active' AND data_type = 'class'",
                (row["series_id"],),
            ).fetchone()
            if record is None or record["revision"] != row["base_revision"]:
                raise ValueError("Ревизия расписания изменилась после предпросмотра.")

            if isinstance(change, ChangeSeries) and change.poster_source_id is not None:
                poster = self._source_row(connection, change.poster_source_id)
                if (
                    poster is None
                    or not poster["is_photo"]
                    or poster["attachment_status"] != "saved"
                    or poster["poster_path"] is None
                    or not (self.path.parent / poster["poster_path"]).is_file()
                ):
                    raise ValueError("Афиша новой версии ещё не сохранена полностью.")

            before_snapshot = capture_weekly_snapshot(connection)
            if isinstance(change, OccurrenceChange):
                if change.expected_exception_revision != row["base_exception_revision"]:
                    raise ValueError("Снимок исключения в изменении повреждён.")
                current_exception = self._exception_row(
                    connection, change.series_id, change.original_date
                )
                current_revision = (
                    current_exception["revision"] if current_exception is not None else None
                )
                if current_revision != row["base_exception_revision"]:
                    raise ValueError("Исключение изменилось после предпросмотра.")
                if (
                    self._version_on_date(connection, change.series_id, change.original_date)
                    is None
                ):
                    raise ValueError("Проведение изменилось после предпросмотра.")
                self._apply_occurrence_change(connection, change, current_exception, now)
            elif isinstance(change, (ChangeSeries, EndSeries)):
                self._validate_series_change_date(change, now)
                previous_affected = _SCHEDULE_EXCEPTIONS_ADAPTER.validate_json(
                    row["affected_exceptions_json"]
                )
                current_affected = self._affected_exceptions(connection, change)
                if previous_affected != current_affected:
                    raise ValueError("Состав исключений изменился; повторно откройте предпросмотр.")
                expected_dates = {item.original_date for item in current_affected}
                if set(change.resolutions) != expected_dates:
                    raise ValueError("Сначала выберите решение для каждого затронутого исключения.")
                for item in current_affected:
                    resolution = change.resolutions[item.original_date]
                    if resolution == "detach" and item.cancelled:
                        raise ValueError("Отменённое проведение нельзя отделить от серии.")
                    self._apply_series_exception_resolution(
                        connection,
                        row["series_id"],
                        item,
                        resolution,
                        change,
                        now,
                    )
                if isinstance(change, ChangeSeries):
                    self._apply_series_change(connection, record, change)
                else:
                    self._apply_series_end(connection, record, change)
            else:
                raise RuntimeError("Неизвестный тип изменения расписания.")

            connection.execute(
                "UPDATE schedule_change_drafts SET status = 'confirmed' "
                "WHERE id = ? AND version = ? AND status = 'pending'",
                (draft_id, version),
            )
            mark_published_week_changes(
                connection, before_snapshot, capture_weekly_snapshot(connection)
            )

    def _discard_schedule_change_sync(self, draft_id: int, version: int) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT version, status FROM schedule_change_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Неизвестное изменение расписания.")
            if row["version"] != version or row["status"] != "pending":
                raise ValueError("Предпросмотр устарел и уже не может быть удалён.")
            connection.execute(
                "UPDATE schedule_change_drafts SET status = 'superseded' "
                "WHERE id = ? AND version = ? AND status = 'pending'",
                (draft_id, version),
            )

    @staticmethod
    def _aware_moscow_now(now: datetime) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Для изменения расписания требуется время с часовым поясом.")
        return now.astimezone(_MOSCOW)

    def _validate_series_change_date(self, change: ChangeSeries | EndSeries, now: datetime) -> None:
        today = self._aware_moscow_now(now).date()
        if isinstance(change, ChangeSeries):
            tomorrow = self._day_after(today)
            if tomorrow is None or change.effective_from < tomorrow:
                raise ValueError("Новое расписание может действовать не раньше завтрашнего дня.")
        elif change.last_date < today:
            raise ValueError("Последняя дата серии не может быть раньше сегодняшнего дня.")

    @classmethod
    def _schedule_draft_from_row(cls, row: sqlite3.Row) -> ChangeDraft:
        change_json = row["change_json"]
        affected_json = row["affected_exceptions_json"]
        versions_json = row["superseded_versions_json"]
        if not all(isinstance(value, str) for value in (change_json, affected_json, versions_json)):
            raise ValueError("Повреждены данные изменения расписания.")
        return ChangeDraft(
            id=row["id"],
            version=row["version"],
            base_revision=row["base_revision"],
            change=_SCHEDULE_CHANGE_ADAPTER.validate_json(change_json),
            affected_exceptions=_SCHEDULE_EXCEPTIONS_ADAPTER.validate_json(affected_json),
            superseded_versions=_SERIES_VERSIONS_ADAPTER.validate_json(versions_json),
            status=row["status"],
        )

    @staticmethod
    def _exception_row(
        connection: sqlite3.Connection, series_id: int, original_date: date
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM schedule_exceptions WHERE series_id = ? AND original_date = ?",
            (series_id, original_date.isoformat()),
        ).fetchone()

    @classmethod
    def _version_on_date(
        cls, connection: sqlite3.Connection, series_id: int, occurrence_date: date
    ) -> SeriesVersion | None:
        rows = connection.execute(
            "SELECT * FROM schedule_versions WHERE series_id = ? AND status = 'active' "
            "ORDER BY revision DESC",
            (series_id,),
        ).fetchall()
        for row in rows:
            version = cls._series_version_from_row(row)
            data = version.data
            if (
                version.effective_from <= occurrence_date
                and (version.effective_until is None or occurrence_date < version.effective_until)
                and data.first_date <= occurrence_date
                and (data.last_date is None or occurrence_date <= data.last_date)
                and (occurrence_date - data.first_date).days % 7 == 0
            ):
                return version
        return None

    @classmethod
    def _version_at_boundary(
        cls, connection: sqlite3.Connection, series_id: int, boundary: date
    ) -> SeriesVersion | None:
        rows = connection.execute(
            "SELECT * FROM schedule_versions WHERE series_id = ? AND status = 'active' "
            "ORDER BY effective_from, revision",
            (series_id,),
        ).fetchall()
        versions = [cls._series_version_from_row(row) for row in rows]
        for version in reversed(versions):
            if version.effective_from <= boundary:
                return version
        return versions[0] if versions else None

    @classmethod
    def _affected_exceptions(
        cls, connection: sqlite3.Connection, change: ChangeSeries | EndSeries
    ) -> list[ScheduleException]:
        rows = connection.execute(
            "SELECT * FROM schedule_exceptions WHERE series_id = ? AND archived = 0 "
            "ORDER BY original_date",
            (change.series_id,),
        ).fetchall()
        affected: list[ScheduleException] = []
        for row in rows:
            exception = cls._schedule_exception_from_row(row)
            if isinstance(change, ChangeSeries):
                boundary = change.effective_from
                includes = exception.original_date >= boundary or (
                    exception.new_date is not None and exception.new_date >= boundary
                )
            else:
                boundary = change.last_date
                includes = exception.original_date > boundary or (
                    exception.new_date is not None and exception.new_date > boundary
                )
            if includes:
                affected.append(exception)
        return affected

    @classmethod
    def _versions_superseded_by(
        cls, connection: sqlite3.Connection, change: ChangeSeries | EndSeries
    ) -> list[SeriesVersion]:
        boundary = change.effective_from if isinstance(change, ChangeSeries) else change.last_date
        rows = connection.execute(
            "SELECT * FROM schedule_versions WHERE series_id = ? AND status = 'active' "
            "ORDER BY effective_from, revision",
            (change.series_id,),
        ).fetchall()
        return [
            cls._series_version_from_row(row)
            for row in rows
            if (
                date.fromisoformat(row["effective_from"]) >= boundary
                if isinstance(change, ChangeSeries)
                else date.fromisoformat(row["effective_from"]) > boundary
            )
        ]

    def _apply_occurrence_change(
        self,
        connection: sqlite3.Connection,
        change: OccurrenceChange,
        current: sqlite3.Row | None,
        now: datetime,
    ) -> None:
        if isinstance(change, RemoveException):
            if current is None or current["archived"]:
                raise ValueError("Для этой даты нет активного исключения.")
            self._archive_exception(connection, current, now)
            return

        if current is not None:
            self._append_exception_history(connection, current, now)
            revision = current["revision"] + 1
            preserve_transfer = not bool(current["archived"])
        else:
            revision = 1
            preserve_transfer = False

        if isinstance(change, CancelOccurrence):
            cancelled = True
            new_date = current["new_date"] if preserve_transfer and current is not None else None
            new_time = current["new_time"] if preserve_transfer and current is not None else None
            place = current["place"] if preserve_transfer and current is not None else None
        elif isinstance(change, TransferOccurrence):
            cancelled = False
            new_date = change.new_date.isoformat()
            new_time = change.new_time.isoformat()
            place = change.place
        else:
            raise RuntimeError("Неизвестный тип исключения расписания.")
        source_id = change.source_id
        if source_id is None and current is not None:
            source_id = current["source_id"]
        self._upsert_schedule_exception(
            connection,
            series_id=change.series_id,
            original_date=change.original_date,
            revision=revision,
            cancelled=cancelled,
            new_date=new_date,
            new_time=new_time,
            place=place,
            source_id=source_id,
            archived=False,
        )

    def _apply_series_exception_resolution(
        self,
        connection: sqlite3.Connection,
        series_id: int,
        exception: ScheduleException,
        resolution: Literal["remove", "detach"],
        change: ChangeSeries | EndSeries,
        now: datetime,
    ) -> None:
        current = self._exception_row(connection, series_id, exception.original_date)
        if current is None or current["archived"] or current["revision"] != exception.revision:
            raise ValueError("Исключение изменилось; повторно откройте предпросмотр.")
        if resolution == "remove":
            self._archive_exception(connection, current, now)
            return

        if exception.cancelled or exception.new_date is None or exception.new_time is None:
            raise ValueError("Отменённое проведение нельзя отделить от серии.")
        original_version = self._version_on_date(connection, series_id, exception.original_date)
        if original_version is None:
            raise ValueError("Исходная версия регулярного занятия не найдена.")
        original_data = original_version.data
        detached = EventData(
            title=original_data.title,
            event_type="Разовое занятие",
            start_date=exception.new_date,
            start_time=exception.new_time,
            place=exception.place or original_data.place,
            price=original_data.price,
            description=original_data.description,
        )
        cursor = connection.execute(
            "INSERT INTO records (revision, data_type, data_json, poster_source_id, status) "
            "VALUES (1, 'event', ?, ?, 'active')",
            (detached.model_dump_json(), original_version.poster_source_id),
        )
        event_record_id = cursor.lastrowid
        if event_record_id is None:
            raise RuntimeError("Не удалось создать отдельную запись проведения.")
        connection.execute(
            "INSERT INTO detached_occurrence_links "
            "(event_record_id, series_id, original_date, source_id, exception_source_id, "
            "poster_source_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                event_record_id,
                series_id,
                exception.original_date.isoformat(),
                original_version.source_id,
                exception.source_id,
                original_version.poster_source_id,
            ),
        )
        self._append_exception_history(connection, current, now)
        detached_revision = current["revision"] + 1
        self._upsert_schedule_exception(
            connection,
            series_id=series_id,
            original_date=exception.original_date,
            revision=detached_revision,
            cancelled=exception.cancelled,
            new_date=current["new_date"],
            new_time=current["new_time"],
            place=current["place"],
            source_id=current["source_id"],
            archived=True,
        )
        if isinstance(change, ChangeSeries):
            leave_tombstone = exception.original_date < change.effective_from
        else:
            leave_tombstone = exception.original_date <= change.last_date
        if leave_tombstone:
            archived = self._exception_row(connection, series_id, exception.original_date)
            if archived is None:
                raise RuntimeError("Отделённое исключение не найдено.")
            self._append_exception_history(connection, archived, now)
            self._upsert_schedule_exception(
                connection,
                series_id=series_id,
                original_date=exception.original_date,
                revision=detached_revision + 1,
                cancelled=True,
                new_date=None,
                new_time=None,
                place=None,
                source_id=exception.source_id,
                archived=False,
            )

    def _apply_series_change(
        self,
        connection: sqlite3.Connection,
        record: sqlite3.Row,
        change: ChangeSeries,
    ) -> None:
        new_revision = record["revision"] + 1
        boundary = change.effective_from.isoformat()
        version_at_boundary = self._version_at_boundary(
            connection, change.series_id, change.effective_from
        )
        versions = connection.execute(
            "SELECT * FROM schedule_versions WHERE series_id = ? AND status = 'active'",
            (change.series_id,),
        ).fetchall()
        for row in versions:
            start = date.fromisoformat(row["effective_from"])
            end = date.fromisoformat(row["effective_until"]) if row["effective_until"] else None
            if start >= change.effective_from:
                connection.execute(
                    "UPDATE schedule_versions SET status = 'superseded' "
                    "WHERE series_id = ? AND revision = ? AND status = 'active'",
                    (change.series_id, row["revision"]),
                )
            elif end is None or end > change.effective_from:
                connection.execute(
                    "UPDATE schedule_versions SET effective_until = ? "
                    "WHERE series_id = ? AND revision = ? AND status = 'active'",
                    (boundary, change.series_id, row["revision"]),
                )
        if change.remove_poster:
            poster_source_id = None
        elif change.poster_source_id is not None:
            poster_source_id = change.poster_source_id
        elif version_at_boundary is not None:
            poster_source_id = version_at_boundary.poster_source_id
        else:
            poster_source_id = record["poster_source_id"]
        connection.execute(
            "INSERT INTO schedule_versions "
            "(series_id, revision, effective_from, effective_until, data_json, "
            "poster_source_id, source_id, status) VALUES (?, ?, ?, NULL, ?, ?, ?, 'active')",
            (
                change.series_id,
                new_revision,
                boundary,
                change.data.model_dump_json(),
                poster_source_id,
                change.source_id,
            ),
        )
        updated = connection.execute(
            "UPDATE records SET revision = ?, data_json = ?, poster_source_id = ? "
            "WHERE id = ? AND revision = ? AND status = 'active'",
            (
                new_revision,
                change.data.model_dump_json(),
                poster_source_id,
                change.series_id,
                record["revision"],
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("Серия изменилась до подтверждения расписания.")

    def _apply_series_end(
        self,
        connection: sqlite3.Connection,
        record: sqlite3.Row,
        change: EndSeries,
    ) -> None:
        boundary = self._day_after(change.last_date)
        versions = connection.execute(
            "SELECT * FROM schedule_versions WHERE series_id = ? AND status = 'active'",
            (change.series_id,),
        ).fetchall()
        for row in versions:
            start = date.fromisoformat(row["effective_from"])
            end = date.fromisoformat(row["effective_until"]) if row["effective_until"] else None
            if start > change.last_date:
                connection.execute(
                    "UPDATE schedule_versions SET status = 'superseded' "
                    "WHERE series_id = ? AND revision = ? AND status = 'active'",
                    (change.series_id, row["revision"]),
                )
            elif start <= change.last_date and (end is None or change.last_date < end):
                connection.execute(
                    "UPDATE schedule_versions SET effective_until = ? "
                    "WHERE series_id = ? AND revision = ? AND status = 'active'",
                    (
                        boundary.isoformat() if boundary is not None else None,
                        change.series_id,
                        row["revision"],
                    ),
                )
        version_row = connection.execute(
            "SELECT * FROM schedule_versions WHERE series_id = ? AND status = 'active' "
            "ORDER BY effective_from DESC, revision DESC LIMIT 1",
            (change.series_id,),
        ).fetchone()
        if version_row is None:
            updated = connection.execute(
                "UPDATE records SET revision = revision + 1 "
                "WHERE id = ? AND revision = ? AND status = 'active'",
                (change.series_id, record["revision"]),
            )
        else:
            latest_version = self._series_version_from_row(version_row)
            updated = connection.execute(
                "UPDATE records SET revision = revision + 1, data_json = ?, poster_source_id = ? "
                "WHERE id = ? AND revision = ? AND status = 'active'",
                (
                    latest_version.data.model_dump_json(),
                    latest_version.poster_source_id,
                    change.series_id,
                    record["revision"],
                ),
            )
        if updated.rowcount != 1:
            raise ValueError("Серия изменилась до завершения расписания.")

    @staticmethod
    def _append_exception_history(
        connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> None:
        connection.execute(
            "INSERT INTO schedule_exception_history "
            "(exception_id, series_id, original_date, revision, cancelled, new_date, "
            "new_time, place, source_id, archived, changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                row["series_id"],
                row["original_date"],
                row["revision"],
                row["cancelled"],
                row["new_date"],
                row["new_time"],
                row["place"],
                row["source_id"],
                row["archived"],
                now.isoformat(),
            ),
        )

    @staticmethod
    def _archive_exception(connection: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> None:
        ScheduleStorage._append_exception_history(connection, row, now)
        updated = connection.execute(
            "UPDATE schedule_exceptions SET revision = revision + 1, archived = 1 "
            "WHERE id = ? AND revision = ? AND archived = 0",
            (row["id"], row["revision"]),
        )
        if updated.rowcount != 1:
            raise ValueError("Исключение изменилось до снятия.")

    @staticmethod
    def _upsert_schedule_exception(
        connection: sqlite3.Connection,
        *,
        series_id: int,
        original_date: date,
        revision: int,
        cancelled: bool,
        new_date: str | None,
        new_time: str | None,
        place: str | None,
        source_id: int | None,
        archived: bool,
    ) -> None:
        existing = ScheduleStorage._exception_row(connection, series_id, original_date)
        if existing is None:
            connection.execute(
                "INSERT INTO schedule_exceptions "
                "(series_id, original_date, revision, cancelled, new_date, new_time, place, "
                "source_id, archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    series_id,
                    original_date.isoformat(),
                    revision,
                    cancelled,
                    new_date,
                    new_time,
                    place,
                    source_id,
                    archived,
                ),
            )
            return
        updated = connection.execute(
            "UPDATE schedule_exceptions SET revision = ?, cancelled = ?, new_date = ?, "
            "new_time = ?, place = ?, source_id = ?, archived = ? "
            "WHERE id = ? AND revision = ?",
            (
                revision,
                cancelled,
                new_date,
                new_time,
                place,
                source_id,
                archived,
                existing["id"],
                existing["revision"],
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("Исключение изменилось до сохранения.")
