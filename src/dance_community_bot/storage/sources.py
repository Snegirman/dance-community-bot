"""Хранение исходных сообщений и вложений."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from ..models import Source, SourceInput
from .base import StorageBase


class SourcesStorage(StorageBase):
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
