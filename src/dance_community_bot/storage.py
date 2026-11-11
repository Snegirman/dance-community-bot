from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, datetime, time
from importlib.resources import files
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from .models import (
    CancelOccurrence,
    CardAction,
    CardActionName,
    CardBinding,
    CardData,
    ChangeDraft,
    ChangeSeries,
    ClassData,
    Draft,
    EndSeries,
    EventData,
    OccurrenceChange,
    Record,
    RemoveException,
    ScheduleChange,
    ScheduleException,
    SeriesVersion,
    Source,
    SourceInput,
    TransferOccurrence,
)

_SCHEDULE_CHANGE_ADAPTER = TypeAdapter(ScheduleChange)
_SCHEDULE_EXCEPTIONS_ADAPTER = TypeAdapter(list[ScheduleException])
_SERIES_VERSIONS_ADAPTER = TypeAdapter(list[SeriesVersion])
_MOSCOW = ZoneInfo("Europe/Moscow")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def save_source(self, source: SourceInput) -> Source:
        return await asyncio.to_thread(self._save_source_sync, source)

    async def get_source(self, chat_id: int, message_id: int) -> Source | None:
        return await asyncio.to_thread(self._get_source_sync, chat_id, message_id)

    async def get_source_by_id(self, source_id: int) -> Source | None:
        return await asyncio.to_thread(self._get_source_by_id_sync, source_id)

    async def list_sources(self) -> list[Source]:
        return await asyncio.to_thread(self._list_sources_sync)

    async def set_source_error(self, source_id: int, reason: str) -> None:
        await asyncio.to_thread(self._set_source_error_sync, source_id, reason)

    async def retry_source(self, source_id: int) -> Source:
        return await asyncio.to_thread(self._retry_source_sync, source_id)

    async def mark_source_processed(self, source_id: int) -> Source:
        return await asyncio.to_thread(self._mark_source_processed_sync, source_id)

    async def set_source_poster_path(self, source_id: int, poster_path: Path) -> Source:
        return await asyncio.to_thread(self._set_source_poster_path_sync, source_id, poster_path)

    async def remove_source_poster(self, source_id: int) -> Source:
        return await asyncio.to_thread(self._remove_source_poster_sync, source_id)

    async def save_draft(
        self,
        source_id: int,
        data: CardData,
        poster_source_id: int | None,
        record_id: int | None = None,
        base_revision: int | None = None,
        expected_draft_id: int | None = None,
    ) -> Draft:
        """Создать версию; до первого подтверждения базовая ревизия — None."""
        return await asyncio.to_thread(
            self._save_draft_sync,
            source_id,
            data,
            poster_source_id,
            record_id,
            base_revision,
            expected_draft_id,
        )

    async def confirm_draft(self, draft_id: int, version: int) -> Record:
        return await asyncio.to_thread(self._confirm_draft_sync, draft_id, version)

    async def reject_draft(self, draft_id: int, version: int) -> None:
        await asyncio.to_thread(self._reject_draft_sync, draft_id, version)

    async def cancel_record(self, record_id: int, revision: int) -> Record:
        return await asyncio.to_thread(self._cancel_record_sync, record_id, revision)

    async def get_record(self, record_id: int) -> Record | None:
        return await asyncio.to_thread(self._get_record_sync, record_id)

    async def get_draft(self, draft_id: int) -> Draft | None:
        return await asyncio.to_thread(self._get_draft_sync, draft_id)

    async def list_records(self) -> list[Record]:
        return await asyncio.to_thread(self._list_records_sync)

    async def list_drafts(self) -> list[Draft]:
        return await asyncio.to_thread(self._list_drafts_sync)

    async def list_pending_schedule_changes(self) -> list[ChangeDraft]:
        return await asyncio.to_thread(self._list_pending_schedule_changes_sync)

    async def get_schedule(self) -> tuple[list[SeriesVersion], list[ScheduleException]]:
        return await asyncio.to_thread(self._get_schedule_sync)

    async def get_schedule_snapshot(
        self,
    ) -> tuple[
        list[SeriesVersion],
        list[ScheduleException],
        dict[tuple[int, date], int],
        list[Record],
    ]:
        return await asyncio.to_thread(self._get_schedule_snapshot_sync)

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

    async def bind_card_message(self, binding: CardBinding) -> None:
        await asyncio.to_thread(self._bind_card_message_sync, binding)

    async def get_card_binding(self, chat_id: int, bot_message_id: int) -> CardBinding | None:
        return await asyncio.to_thread(self._get_card_binding_sync, chat_id, bot_message_id)

    async def create_card_action(
        self,
        action: CardActionName,
        *,
        record_id: int | None = None,
        draft_id: int | None = None,
        version: int | None = None,
        revision: int | None = None,
        source_id: int | None = None,
        page: int | None = None,
        series_id: int | None = None,
        schedule_revision: int | None = None,
        original_date: date | None = None,
        expected_exception_revision: int | None = None,
        schedule_draft_id: int | None = None,
        schedule_draft_version: int | None = None,
        remove_poster: bool = False,
    ) -> CardAction:
        return await asyncio.to_thread(
            self._create_card_action_sync,
            action,
            record_id,
            draft_id,
            version,
            revision,
            source_id,
            page,
            series_id,
            schedule_revision,
            original_date,
            expected_exception_revision,
            schedule_draft_id,
            schedule_draft_version,
            remove_poster,
        )

    async def get_card_action(self, token: str) -> CardAction | None:
        return await asyncio.to_thread(self._get_card_action_sync, token)

    async def bind_card_actions(self, chat_id: int, bot_message_id: int, tokens: list[str]) -> None:
        await asyncio.to_thread(self._bind_card_actions_sync, chat_id, bot_message_id, tokens)

    async def complete_card_action(self, token: str, outcome: str) -> CardAction:
        return await asyncio.to_thread(self._complete_card_action_sync, token, outcome)

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        (self.path.parent / "posters").mkdir(parents=True, exist_ok=True)
        schema = files("dance_community_bot").joinpath("schema.sql").read_text(encoding="utf-8")

        with self._transaction() as connection:
            row = connection.execute("PRAGMA user_version").fetchone()
            if row is None:
                raise RuntimeError("Не удалось прочитать версию схемы базы данных.")
            schema_version = row[0]
            if not isinstance(schema_version, int):
                raise RuntimeError("Некорректная версия схемы базы данных.")
            if schema_version not in (0, 4):
                raise RuntimeError(
                    f"Неподдерживаемая версия схемы базы данных: {schema_version}. Ожидается 4."
                )
            if schema_version == 0:
                for statement in schema.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("PRAGMA user_version = 4")

    def _save_source_sync(self, source: SourceInput) -> Source:
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sources "
                "(chat_id, message_id, text, received_at, telegram_file_id, media_group_id, "
                "reply_to_message_id, attachment_status, is_photo) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source.chat_id,
                    source.message_id,
                    source.text,
                    source.received_at.isoformat(),
                    source.telegram_file_id,
                    source.media_group_id,
                    source.reply_to_message_id,
                    "pending" if source.telegram_file_id is not None else "none",
                    source.is_photo,
                ),
            )
            row = connection.execute(
                "SELECT * FROM sources WHERE chat_id = ? AND message_id = ?",
                (source.chat_id, source.message_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Источник не сохранился.")
            return self._source_from_row(row)

    def _get_source_sync(self, chat_id: int, message_id: int) -> Source | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            return self._source_from_row(row) if row is not None else None

    def _get_source_by_id_sync(self, source_id: int) -> Source | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            return self._source_from_row(row) if row is not None else None

    def _list_sources_sync(self) -> list[Source]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM sources ORDER BY id").fetchall()
            return [self._source_from_row(row) for row in rows]

    def _set_source_error_sync(self, source_id: int, reason: str) -> None:
        with self._transaction() as connection:
            updated = connection.execute(
                "UPDATE sources SET status = 'failed', error_reason = ?, "
                "attachment_status = CASE WHEN attachment_status = 'pending' THEN 'failed' "
                "ELSE attachment_status END WHERE id = ?",
                (reason, source_id),
            )
            if updated.rowcount != 1:
                raise ValueError("Неизвестный источник.")

    def _retry_source_sync(self, source_id: int) -> Source:
        with self._transaction() as connection:
            updated = connection.execute(
                "UPDATE sources SET status = 'pending', error_reason = NULL, "
                "attachment_status = CASE WHEN telegram_file_id IS NOT NULL "
                "AND poster_path IS NULL AND attachment_status != 'removed' THEN 'pending' "
                "ELSE attachment_status END WHERE id = ?",
                (source_id,),
            )
            if updated.rowcount != 1:
                raise ValueError("Неизвестный источник.")
            row = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row is None:
                raise RuntimeError("Источник повторной обработки не найден.")
            return self._source_from_row(row)

    def _mark_source_processed_sync(self, source_id: int) -> Source:
        with self._transaction() as connection:
            updated = connection.execute(
                "UPDATE sources SET status = 'processed', error_reason = NULL WHERE id = ?",
                (source_id,),
            )
            if updated.rowcount != 1:
                raise ValueError("Неизвестный источник.")
            row = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row is None:
                raise RuntimeError("Обработанный источник не найден.")
            return self._source_from_row(row)

    def _set_source_poster_path_sync(self, source_id: int, poster_path: Path) -> Source:
        if poster_path.is_absolute() or ".." in poster_path.parts:
            raise ValueError("Путь афиши должен быть относительным.")
        if not (self.path.parent / poster_path).is_file():
            raise ValueError("Файл афиши ещё не сохранён на диске.")
        with self._transaction() as connection:
            updated = connection.execute(
                "UPDATE sources SET poster_path = ?, attachment_status = 'saved', "
                "error_reason = NULL WHERE id = ? AND telegram_file_id IS NOT NULL",
                (poster_path.as_posix(), source_id),
            )
            if updated.rowcount != 1:
                raise ValueError("Неизвестный источник афиши.")
            row = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row is None:
                raise RuntimeError("Источник афиши не найден.")
            return self._source_from_row(row)

    def _remove_source_poster_sync(self, source_id: int) -> Source:
        with self._transaction() as connection:
            updated = connection.execute(
                "UPDATE sources SET attachment_status = 'removed' "
                "WHERE id = ? AND telegram_file_id IS NOT NULL AND poster_path IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM records WHERE poster_source_id = sources.id) "
                "AND NOT EXISTS (SELECT 1 FROM drafts WHERE poster_source_id = sources.id)",
                (source_id,),
            )
            if updated.rowcount != 1:
                raise ValueError("У источника нет афиши для удаления.")
            row = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row is None:
                raise RuntimeError("Источник афиши не найден.")
            return self._source_from_row(row)

    def _save_draft_sync(
        self,
        source_id: int,
        data: CardData,
        poster_source_id: int | None,
        record_id: int | None,
        base_revision: int | None,
        expected_draft_id: int | None,
    ) -> Draft:
        data_type, data_json = self._serialize_data(data)
        with self._transaction() as connection:
            if self._source_row(connection, source_id) is None:
                raise ValueError("Неизвестный источник черновика.")
            if (
                poster_source_id is not None
                and self._source_row(connection, poster_source_id) is None
            ):
                raise ValueError("Неизвестный источник афиши.")

            existing = connection.execute(
                "SELECT * FROM drafts WHERE source_id = ?", (source_id,)
            ).fetchone()
            if existing is not None:
                if (
                    (record_id is not None and existing["record_id"] != record_id)
                    or existing["base_revision"] != base_revision
                    or existing["data_type"] != data_type
                    or existing["data_json"] != data_json
                    or existing["poster_source_id"] != poster_source_id
                ):
                    raise ValueError("Источник уже связан с другой версией черновика.")
                return self._draft_from_row(existing)

            if expected_draft_id is not None:
                current_draft = connection.execute(
                    "SELECT id FROM drafts WHERE record_id = ? AND status = 'pending'",
                    (record_id,),
                ).fetchone()
                if current_draft is None or current_draft["id"] != expected_draft_id:
                    raise ValueError("Черновик изменился до сохранения правки.")

            if record_id is None:
                if base_revision is not None:
                    raise ValueError("Новая запись не может иметь базовую ревизию.")
                cursor = connection.execute(
                    "INSERT INTO records "
                    "(revision, data_type, data_json, poster_source_id, status) "
                    "VALUES (0, ?, ?, ?, 'pending')",
                    (data_type, data_json, poster_source_id),
                )
                new_record_id = cursor.lastrowid
                if new_record_id is None:
                    raise RuntimeError("Не удалось создать запись для черновика.")
                record_id = new_record_id
                version = 1
            else:
                record = connection.execute(
                    "SELECT revision, status, data_type FROM records WHERE id = ?", (record_id,)
                ).fetchone()
                if record is None or record["status"] not in ("pending", "active"):
                    raise ValueError("Нельзя изменить неизвестную или неактивную запись.")
                if record["status"] == "active" and record["data_type"] == "class":
                    raise ValueError("Изменяйте регулярное занятие через датированное расписание.")
                expected_revision = record["revision"] if record["status"] == "active" else None
                if expected_revision != base_revision:
                    raise ValueError("Черновик основан на устаревшей ревизии.")
                version_row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM drafts WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
                if version_row is None or not isinstance(version_row[0], int):
                    raise RuntimeError("Не удалось определить версию черновика.")
                version = version_row[0]
                connection.execute(
                    "UPDATE drafts SET status = 'superseded' "
                    "WHERE record_id = ? AND status = 'pending'",
                    (record_id,),
                )

            cursor = connection.execute(
                "INSERT INTO drafts "
                "(record_id, version, base_revision, data_type, data_json, source_id, "
                "poster_source_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (
                    record_id,
                    version,
                    base_revision,
                    data_type,
                    data_json,
                    source_id,
                    poster_source_id,
                ),
            )
            draft_id = cursor.lastrowid
            if draft_id is None:
                raise RuntimeError("Не удалось создать черновик.")
            row = connection.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if row is None:
                raise RuntimeError("Черновик не сохранился.")
            draft = self._draft_from_row(row)
            connection.execute(
                "UPDATE sources SET status = 'processed', error_reason = NULL WHERE id = ?",
                (source_id,),
            )
            return draft

    def _confirm_draft_sync(self, draft_id: int, version: int) -> Record:
        with self._transaction() as connection:
            draft = connection.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if draft is None:
                raise ValueError("Неизвестный черновик.")
            if draft["version"] != version:
                raise ValueError("Версия черновика устарела.")
            record = connection.execute(
                "SELECT * FROM records WHERE id = ?", (draft["record_id"],)
            ).fetchone()
            if record is None:
                raise RuntimeError("У черновика отсутствует запись.")
            if draft["status"] == "confirmed":
                if record["status"] == "pending":
                    raise RuntimeError("Подтверждённая запись находится в состоянии черновика.")
                return self._record_from_row(record)
            if draft["status"] != "pending":
                raise ValueError("Черновик больше нельзя подтвердить.")
            if record["status"] == "active" and record["data_type"] == "class":
                raise ValueError("Изменяйте регулярное занятие через датированное расписание.")

            poster_source_id = draft["poster_source_id"]
            if poster_source_id is not None:
                poster_source = self._source_row(connection, poster_source_id)
                if (
                    poster_source is None
                    or poster_source["attachment_status"] != "saved"
                    or poster_source["poster_path"] is None
                ):
                    raise ValueError("Нельзя подтвердить черновик: афиша ещё не сохранена.")
                poster_path = Path(poster_source["poster_path"])
                if (
                    poster_path.is_absolute()
                    or ".." in poster_path.parts
                    or not (self.path.parent / poster_path).is_file()
                ):
                    raise ValueError("Нельзя подтвердить черновик: файл афиши недоступен.")

            if record["status"] == "pending":
                if draft["base_revision"] is not None or record["revision"] != 0:
                    raise ValueError("Черновик создания имеет неверную базовую ревизию.")
                new_revision = 1
            elif record["status"] == "active":
                if draft["base_revision"] != record["revision"]:
                    raise ValueError("Черновик основан на устаревшей ревизии.")
                new_revision = record["revision"] + 1
            else:
                raise ValueError("Нельзя подтвердить черновик отменённой записи.")

            update = connection.execute(
                "UPDATE records SET revision = ?, data_type = ?, data_json = ?, "
                "poster_source_id = ?, status = 'active' "
                "WHERE id = ? AND revision = ? AND status = ?",
                (
                    new_revision,
                    draft["data_type"],
                    draft["data_json"],
                    draft["poster_source_id"],
                    draft["record_id"],
                    record["revision"],
                    record["status"],
                ),
            )
            if update.rowcount != 1:
                raise ValueError("Запись изменилась до подтверждения черновика.")
            confirmed = connection.execute(
                "UPDATE drafts SET status = 'confirmed' WHERE id = ? AND status = 'pending'",
                (draft_id,),
            )
            if confirmed.rowcount != 1:
                raise ValueError("Черновик больше нельзя подтвердить.")
            if draft["data_type"] == "class" and record["status"] == "pending":
                class_data = ClassData.model_validate_json(draft["data_json"])
                effective_until = self._day_after(class_data.last_date)
                connection.execute(
                    "INSERT INTO schedule_versions "
                    "(series_id, revision, effective_from, effective_until, data_json, "
                    "poster_source_id, source_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
                    (
                        draft["record_id"],
                        new_revision,
                        class_data.first_date.isoformat(),
                        effective_until.isoformat() if effective_until is not None else None,
                        class_data.model_dump_json(),
                        draft["poster_source_id"],
                        draft["source_id"],
                    ),
                )
            updated_record = connection.execute(
                "SELECT * FROM records WHERE id = ?", (draft["record_id"],)
            ).fetchone()
            if updated_record is None:
                raise RuntimeError("Подтверждённая запись не найдена.")
            return self._record_from_row(updated_record)

    def _reject_draft_sync(self, draft_id: int, version: int) -> None:
        with self._transaction() as connection:
            draft = connection.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if draft is None:
                raise ValueError("Неизвестный черновик.")
            if draft["version"] != version:
                raise ValueError("Версия черновика устарела.")
            if draft["status"] == "rejected":
                return
            if draft["status"] != "pending":
                raise ValueError("Черновик больше нельзя отклонить.")
            connection.execute(
                "UPDATE drafts SET status = 'rejected' WHERE id = ? AND status = 'pending'",
                (draft_id,),
            )

    def _cancel_record_sync(self, record_id: int, revision: int) -> Record:
        with self._transaction() as connection:
            record = connection.execute(
                "SELECT * FROM records WHERE id = ?", (record_id,)
            ).fetchone()
            if (
                record is not None
                and record["status"] == "cancelled"
                and record["revision"] == revision + 1
            ):
                return self._record_from_row(record)
            if record is None or record["status"] != "active":
                raise ValueError("Нельзя отменить неизвестную или неактивную запись.")
            if record["data_type"] == "class":
                raise ValueError("Регулярное занятие завершайте через управление расписанием.")
            if record["revision"] != revision:
                raise ValueError("Ревизия записи устарела.")
            updated = connection.execute(
                "UPDATE records SET revision = revision + 1, status = 'cancelled' "
                "WHERE id = ? AND revision = ? AND status = 'active'",
                (record_id, revision),
            )
            if updated.rowcount != 1:
                raise ValueError("Запись изменилась до отмены.")
            connection.execute(
                "UPDATE drafts SET status = 'superseded' "
                "WHERE record_id = ? AND status = 'pending'",
                (record_id,),
            )
            cancelled = connection.execute(
                "SELECT * FROM records WHERE id = ?", (record_id,)
            ).fetchone()
            if cancelled is None:
                raise RuntimeError("Отменённая запись не найдена.")
            return self._record_from_row(cancelled)

    def _get_record_sync(self, record_id: int) -> Record | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM records WHERE id = ? AND status != 'pending'", (record_id,)
            ).fetchone()
            return self._record_from_row(row) if row is not None else None

    def _get_draft_sync(self, draft_id: int) -> Draft | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            return self._draft_from_row(row) if row is not None else None

    def _list_records_sync(self) -> list[Record]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE status IN ('active', 'cancelled') ORDER BY id"
            ).fetchall()
            return [self._record_from_row(row) for row in rows]

    def _list_drafts_sync(self) -> list[Draft]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM drafts ORDER BY id").fetchall()
            return [self._draft_from_row(row) for row in rows]

    def _list_pending_schedule_changes_sync(self) -> list[ChangeDraft]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM schedule_change_drafts WHERE status = 'pending' ORDER BY id"
            ).fetchall()
            return [self._schedule_draft_from_row(row) for row in rows]

    def _get_schedule_sync(self) -> tuple[list[SeriesVersion], list[ScheduleException]]:
        with self._connection() as connection:
            connection.execute("BEGIN")
            versions = connection.execute(
                "SELECT * FROM schedule_versions WHERE status = 'active' "
                "ORDER BY effective_from, series_id, revision"
            ).fetchall()
            exceptions = connection.execute(
                "SELECT * FROM schedule_exceptions WHERE archived = 0 "
                "ORDER BY original_date, series_id"
            ).fetchall()
            return (
                [self._series_version_from_row(row) for row in versions],
                [self._schedule_exception_from_row(row) for row in exceptions],
            )

    def _get_schedule_snapshot_sync(
        self,
    ) -> tuple[
        list[SeriesVersion],
        list[ScheduleException],
        dict[tuple[int, date], int],
        list[Record],
    ]:
        with self._connection() as connection:
            connection.execute("BEGIN")
            versions = connection.execute(
                "SELECT * FROM schedule_versions WHERE status = 'active' "
                "ORDER BY effective_from, series_id, revision"
            ).fetchall()
            exceptions = connection.execute(
                "SELECT * FROM schedule_exceptions WHERE archived = 0 "
                "ORDER BY original_date, series_id"
            ).fetchall()
            revisions = connection.execute(
                "SELECT series_id, original_date, revision FROM schedule_exceptions"
            ).fetchall()
            records = connection.execute(
                "SELECT * FROM records WHERE status = 'active' AND data_type = 'class' ORDER BY id"
            ).fetchall()
            return (
                [self._series_version_from_row(row) for row in versions],
                [self._schedule_exception_from_row(row) for row in exceptions],
                {
                    (row["series_id"], date.fromisoformat(row["original_date"])): row["revision"]
                    for row in revisions
                },
                [self._record_from_row(row) for row in records],
            )

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

    @staticmethod
    def _day_after(value: date | None) -> date | None:
        if value is None or value == date.max:
            return None
        return date.fromordinal(value.toordinal() + 1)

    def _validate_series_change_date(self, change: ChangeSeries | EndSeries, now: datetime) -> None:
        today = self._aware_moscow_now(now).date()
        if isinstance(change, ChangeSeries):
            tomorrow = self._day_after(today)
            if tomorrow is None or change.effective_from < tomorrow:
                raise ValueError("Новое расписание может действовать не раньше завтрашнего дня.")
        elif change.last_date < today:
            raise ValueError("Последняя дата серии не может быть раньше сегодняшнего дня.")

    @classmethod
    def _series_version_from_row(cls, row: sqlite3.Row) -> SeriesVersion:
        data_json = row["data_json"]
        if not isinstance(data_json, str):
            raise ValueError("Повреждены данные версии расписания.")
        return SeriesVersion(
            series_id=row["series_id"],
            revision=row["revision"],
            effective_from=date.fromisoformat(row["effective_from"]),
            effective_until=(
                date.fromisoformat(row["effective_until"])
                if row["effective_until"] is not None
                else None
            ),
            data=ClassData.model_validate_json(data_json),
            poster_source_id=row["poster_source_id"],
            source_id=row["source_id"],
        )

    @staticmethod
    def _schedule_exception_from_row(row: sqlite3.Row) -> ScheduleException:
        return ScheduleException(
            series_id=row["series_id"],
            original_date=date.fromisoformat(row["original_date"]),
            revision=row["revision"],
            cancelled=bool(row["cancelled"]),
            new_date=date.fromisoformat(row["new_date"]) if row["new_date"] else None,
            new_time=time.fromisoformat(row["new_time"]) if row["new_time"] else None,
            place=row["place"],
            source_id=row["source_id"],
            archived=bool(row["archived"]),
        )

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
        Store._append_exception_history(connection, row, now)
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
        existing = Store._exception_row(connection, series_id, original_date)
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

    def _bind_card_message_sync(self, binding: CardBinding) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO reply_bindings "
                "(chat_id, bot_message_id, record_id, draft_id, version, revision, "
                "source_id, page, schedule_action, schedule_series_id, schedule_revision, "
                "schedule_original_date, expected_exception_revision, schedule_draft_id, "
                "schedule_draft_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id, bot_message_id) DO UPDATE SET "
                "record_id = excluded.record_id, draft_id = excluded.draft_id, "
                "version = excluded.version, revision = excluded.revision, "
                "source_id = excluded.source_id, page = excluded.page, "
                "schedule_action = excluded.schedule_action, "
                "schedule_series_id = excluded.schedule_series_id, "
                "schedule_revision = excluded.schedule_revision, "
                "schedule_original_date = excluded.schedule_original_date, "
                "expected_exception_revision = excluded.expected_exception_revision, "
                "schedule_draft_id = excluded.schedule_draft_id, "
                "schedule_draft_version = excluded.schedule_draft_version",
                (
                    binding.chat_id,
                    binding.bot_message_id,
                    binding.record_id,
                    binding.draft_id,
                    binding.version,
                    binding.revision,
                    binding.source_id,
                    binding.page,
                    binding.schedule_action,
                    binding.schedule_series_id,
                    binding.schedule_revision,
                    (
                        binding.schedule_original_date.isoformat()
                        if binding.schedule_original_date is not None
                        else None
                    ),
                    binding.expected_exception_revision,
                    binding.schedule_draft_id,
                    binding.schedule_draft_version,
                ),
            )

    def _get_card_binding_sync(self, chat_id: int, bot_message_id: int) -> CardBinding | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM reply_bindings WHERE chat_id = ? AND bot_message_id = ?",
                (chat_id, bot_message_id),
            ).fetchone()
            if row is None:
                return None
            return CardBinding(
                chat_id=row["chat_id"],
                bot_message_id=row["bot_message_id"],
                record_id=row["record_id"],
                draft_id=row["draft_id"],
                version=row["version"],
                revision=row["revision"],
                source_id=row["source_id"],
                page=row["page"],
                schedule_action=row["schedule_action"],
                schedule_series_id=row["schedule_series_id"],
                schedule_revision=row["schedule_revision"],
                schedule_original_date=(
                    date.fromisoformat(row["schedule_original_date"])
                    if row["schedule_original_date"] is not None
                    else None
                ),
                expected_exception_revision=row["expected_exception_revision"],
                schedule_draft_id=row["schedule_draft_id"],
                schedule_draft_version=row["schedule_draft_version"],
            )

    def _create_card_action_sync(
        self,
        action: CardActionName,
        record_id: int | None,
        draft_id: int | None,
        version: int | None,
        revision: int | None,
        source_id: int | None,
        page: int | None,
        series_id: int | None,
        schedule_revision: int | None,
        original_date: date | None,
        expected_exception_revision: int | None,
        schedule_draft_id: int | None,
        schedule_draft_version: int | None,
        remove_poster: bool,
    ) -> CardAction:
        token = uuid4().hex
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO card_actions "
                "(token, action, record_id, draft_id, version, revision, source_id, page, "
                "series_id, schedule_revision, original_date, expected_exception_revision, "
                "schedule_draft_id, schedule_draft_version, remove_poster) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token,
                    action,
                    record_id,
                    draft_id,
                    version,
                    revision,
                    source_id,
                    page,
                    series_id,
                    schedule_revision,
                    original_date.isoformat() if original_date is not None else None,
                    expected_exception_revision,
                    schedule_draft_id,
                    schedule_draft_version,
                    remove_poster,
                ),
            )
            row = connection.execute(
                "SELECT * FROM card_actions WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                raise RuntimeError("Действие карточки не сохранилось.")
            return self._card_action_from_row(row)

    def _get_card_action_sync(self, token: str) -> CardAction | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM card_actions WHERE token = ?", (token,)
            ).fetchone()
            return self._card_action_from_row(row) if row is not None else None

    def _bind_card_actions_sync(self, chat_id: int, bot_message_id: int, tokens: list[str]) -> None:
        with self._transaction() as connection:
            for token in tokens:
                updated = connection.execute(
                    "UPDATE card_actions SET chat_id = ?, bot_message_id = ? "
                    "WHERE token = ? AND (chat_id IS NULL OR "
                    "(chat_id = ? AND bot_message_id = ?))",
                    (chat_id, bot_message_id, token, chat_id, bot_message_id),
                )
                if updated.rowcount != 1:
                    raise ValueError("Кнопка уже связана с другой карточкой.")

    def _complete_card_action_sync(self, token: str, outcome: str) -> CardAction:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE card_actions SET completed = 1, outcome = ? "
                "WHERE token = ? AND completed = 0",
                (outcome, token),
            )
            row = connection.execute(
                "SELECT * FROM card_actions WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                raise ValueError("Неизвестное действие карточки.")
            return self._card_action_from_row(row)

    @staticmethod
    def _source_row(connection: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()

    @staticmethod
    def _serialize_data(data: CardData) -> tuple[str, str]:
        data_type = "event" if isinstance(data, EventData) else "class"
        return data_type, data.model_dump_json()

    @staticmethod
    def _deserialize_data(data_type: str, data_json: str) -> CardData:
        if data_type == "event":
            return EventData.model_validate_json(data_json)
        if data_type == "class":
            return ClassData.model_validate_json(data_json)
        raise ValueError("Неизвестный тип сохранённых данных.")

    @classmethod
    def _source_from_row(cls, row: sqlite3.Row) -> Source:
        return Source(
            id=row["id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            text=row["text"],
            received_at=row["received_at"],
            telegram_file_id=row["telegram_file_id"],
            is_photo=bool(row["is_photo"]),
            media_group_id=row["media_group_id"],
            status=row["status"],
            error_reason=row["error_reason"],
            poster_path=row["poster_path"],
            reply_to_message_id=row["reply_to_message_id"],
            attachment_status=row["attachment_status"],
        )

    @staticmethod
    def _card_action_from_row(row: sqlite3.Row) -> CardAction:
        return CardAction(
            token=row["token"],
            chat_id=row["chat_id"],
            bot_message_id=row["bot_message_id"],
            action=row["action"],
            record_id=row["record_id"],
            draft_id=row["draft_id"],
            version=row["version"],
            revision=row["revision"],
            source_id=row["source_id"],
            page=row["page"],
            series_id=row["series_id"],
            schedule_revision=row["schedule_revision"],
            original_date=(
                date.fromisoformat(row["original_date"]) if row["original_date"] else None
            ),
            expected_exception_revision=row["expected_exception_revision"],
            schedule_draft_id=row["schedule_draft_id"],
            schedule_draft_version=row["schedule_draft_version"],
            remove_poster=bool(row["remove_poster"]),
            completed=bool(row["completed"]),
            outcome=row["outcome"],
        )

    @classmethod
    def _draft_from_row(cls, row: sqlite3.Row) -> Draft:
        data_type = row["data_type"]
        data_json = row["data_json"]
        if not isinstance(data_type, str) or not isinstance(data_json, str):
            raise ValueError("Повреждённые данные черновика.")
        return Draft(
            id=row["id"],
            record_id=row["record_id"],
            version=row["version"],
            base_revision=row["base_revision"],
            data=cls._deserialize_data(data_type, data_json),
            source_id=row["source_id"],
            poster_source_id=row["poster_source_id"],
            status=row["status"],
        )

    @classmethod
    def _record_from_row(cls, row: sqlite3.Row) -> Record:
        data_type = row["data_type"]
        data_json = row["data_json"]
        if not isinstance(data_type, str) or not isinstance(data_json, str):
            raise ValueError("Повреждённые данные записи.")
        return Record(
            id=row["id"],
            revision=row["revision"],
            data=cls._deserialize_data(data_type, data_json),
            poster_source_id=row["poster_source_id"],
            status=row["status"],
        )
