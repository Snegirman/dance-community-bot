"""Хранение черновиков и подтверждённых записей."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from ..models import CardData, ClassData, Draft, Record
from .automation import capture_weekly_snapshot, mark_published_week_changes
from .base import StorageBase


class RecordsStorage(StorageBase):
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

            before_snapshot = capture_weekly_snapshot(connection)
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
            mark_published_week_changes(
                connection, before_snapshot, capture_weekly_snapshot(connection)
            )
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
            before_snapshot = capture_weekly_snapshot(connection)
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
            mark_published_week_changes(
                connection, before_snapshot, capture_weekly_snapshot(connection)
            )
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
