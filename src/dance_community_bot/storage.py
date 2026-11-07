from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from .models import CardData, ClassData, Draft, EventData, Record, Source, SourceInput


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def save_source(self, source: SourceInput) -> Source:
        return await asyncio.to_thread(self._save_source_sync, source)

    async def save_draft(
        self,
        source_id: int,
        data: CardData,
        poster_source_id: int | None,
        record_id: int | None = None,
        base_revision: int | None = None,
    ) -> Draft:
        """Создать версию; до первого подтверждения базовая ревизия — None."""
        return await asyncio.to_thread(
            self._save_draft_sync,
            source_id,
            data,
            poster_source_id,
            record_id,
            base_revision,
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
        migration = (
            files("dance_community_bot")
            .joinpath("migrations", "001_initial.sql")
            .read_text(encoding="utf-8")
        )

        with self._transaction() as connection:
            row = connection.execute("PRAGMA user_version").fetchone()
            if row is None:
                raise RuntimeError("Не удалось прочитать версию схемы базы данных.")
            schema_version = row[0]
            if not isinstance(schema_version, int):
                raise RuntimeError("Некорректная версия схемы базы данных.")
            if schema_version > 1:
                raise RuntimeError("Версия схемы базы данных новее поддерживаемой.")
            if schema_version == 0:
                for statement in migration.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("PRAGMA user_version = 1")

    def _save_source_sync(self, source: SourceInput) -> Source:
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sources "
                "(chat_id, message_id, text, received_at, telegram_file_id, media_group_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    source.chat_id,
                    source.message_id,
                    source.text,
                    source.received_at.isoformat(),
                    source.telegram_file_id,
                    source.media_group_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM sources WHERE chat_id = ? AND message_id = ?",
                (source.chat_id, source.message_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Источник не сохранился.")
            return self._source_from_row(row)

    def _save_draft_sync(
        self,
        source_id: int,
        data: CardData,
        poster_source_id: int | None,
        record_id: int | None,
        base_revision: int | None,
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
                    "SELECT revision, status FROM records WHERE id = ?", (record_id,)
                ).fetchone()
                if record is None or record["status"] not in ("pending", "active"):
                    raise ValueError("Нельзя изменить неизвестную или неактивную запись.")
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
            media_group_id=row["media_group_id"],
            status=row["status"],
            error_reason=row["error_reason"],
            poster_path=row["poster_path"],
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
