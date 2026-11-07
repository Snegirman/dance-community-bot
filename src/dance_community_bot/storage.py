from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from .models import (
    CardAction,
    CardActionName,
    CardBinding,
    CardData,
    ClassData,
    Draft,
    EventData,
    Record,
    Source,
    SourceInput,
)


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
            if schema_version > 2:
                raise RuntimeError("Версия схемы базы данных новее поддерживаемой.")
            if schema_version == 0:
                for statement in migration.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                schema_version = 1
                connection.execute("PRAGMA user_version = 1")
            if schema_version < 2:
                intake_migration = (
                    files("dance_community_bot")
                    .joinpath("migrations", "002_intake.sql")
                    .read_text(encoding="utf-8")
                )
                for statement in intake_migration.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("PRAGMA user_version = 2")

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

    def _bind_card_message_sync(self, binding: CardBinding) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO reply_bindings "
                "(chat_id, bot_message_id, record_id, draft_id, version, revision, "
                "source_id, page) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id, bot_message_id) DO UPDATE SET "
                "record_id = excluded.record_id, draft_id = excluded.draft_id, "
                "version = excluded.version, revision = excluded.revision, "
                "source_id = excluded.source_id, page = excluded.page",
                (
                    binding.chat_id,
                    binding.bot_message_id,
                    binding.record_id,
                    binding.draft_id,
                    binding.version,
                    binding.revision,
                    binding.source_id,
                    binding.page,
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
    ) -> CardAction:
        token = uuid4().hex
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO card_actions "
                "(token, action, record_id, draft_id, version, revision, source_id, page) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (token, action, record_id, draft_id, version, revision, source_id, page),
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
