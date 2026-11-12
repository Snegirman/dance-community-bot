"""Привязки сообщений и действия карточек."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from uuid import uuid4

from ..models import CardAction, CardActionName, CardBinding
from .base import StorageBase


class CardsStorage(StorageBase):
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
