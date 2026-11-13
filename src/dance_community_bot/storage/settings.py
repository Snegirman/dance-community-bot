"""Постоянная настройка времени автопубликации и привязки её prompt."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, time

from ..models import WeeklyPublicationPrompt, WeeklyPublicationSetting
from .base import StorageBase


class SettingsStorage(StorageBase):
    async def initialize_weekly_publication_setting(self, initial_time: time | None) -> None:
        await asyncio.to_thread(self._initialize_weekly_publication_setting_sync, initial_time)

    async def get_weekly_publication_setting(self) -> WeeklyPublicationSetting:
        return await asyncio.to_thread(self._get_weekly_publication_setting_sync)

    async def set_weekly_publication_time(
        self,
        value: time | None,
        *,
        expected_revision: int,
        changed_at: datetime | None = None,
    ) -> WeeklyPublicationSetting | None:
        return await asyncio.to_thread(
            self._set_weekly_publication_time_sync,
            value,
            expected_revision,
            changed_at,
        )

    async def bind_weekly_publication_prompt(
        self, chat_id: int, message_id: int, revision: int
    ) -> bool:
        return await asyncio.to_thread(
            self._bind_weekly_publication_prompt_sync, chat_id, message_id, revision
        )

    async def get_weekly_publication_prompt(
        self, chat_id: int, message_id: int
    ) -> WeeklyPublicationPrompt | None:
        return await asyncio.to_thread(
            self._get_weekly_publication_prompt_sync, chat_id, message_id
        )

    async def complete_weekly_publication_prompt(
        self,
        chat_id: int,
        message_id: int,
        value: time,
        *,
        changed_at: datetime | None = None,
    ) -> WeeklyPublicationSetting | None:
        return await asyncio.to_thread(
            self._complete_weekly_publication_prompt_sync,
            chat_id,
            message_id,
            value,
            changed_at,
        )

    def _initialize_weekly_publication_setting_sync(self, initial_time: time | None) -> None:
        value = self._time_text(initial_time)
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO weekly_publication_settings "
                "(id, publication_time, revision, changed_at) VALUES (1, ?, 0, NULL)",
                (value,),
            )

    def _get_weekly_publication_setting_sync(self) -> WeeklyPublicationSetting:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM weekly_publication_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("Не инициализировано время автопубликации.")
            return self._weekly_publication_setting_from_row(row)

    def _set_weekly_publication_time_sync(
        self,
        value: time | None,
        expected_revision: int,
        changed_at: datetime | None,
    ) -> WeeklyPublicationSetting | None:
        value_text = self._time_text(value)
        timestamp = self._timestamp_text(changed_at)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM weekly_publication_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("Не инициализировано время автопубликации.")
            current = self._weekly_publication_setting_from_row(row)
            if current.revision != expected_revision:
                return None
            if current.weekly_publication_time == value:
                connection.execute(
                    "UPDATE weekly_publication_prompts SET status = 'superseded' "
                    "WHERE status = 'pending'"
                )
                return current

            next_setting = WeeklyPublicationSetting(
                weekly_publication_time=value,
                revision=current.revision + 1,
                changed_at=datetime.fromisoformat(timestamp),
            )
            connection.execute(
                "UPDATE weekly_publication_settings SET publication_time = ?, revision = ?, "
                "changed_at = ? WHERE id = 1 AND revision = ?",
                (
                    value_text,
                    next_setting.revision,
                    timestamp,
                    current.revision,
                ),
            )
            connection.execute(
                "UPDATE weekly_publication_prompts SET status = 'superseded' "
                "WHERE status = 'pending'"
            )
            return next_setting

    def _bind_weekly_publication_prompt_sync(
        self, chat_id: int, message_id: int, revision: int
    ) -> bool:
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT status FROM weekly_publication_prompts "
                "WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            if existing is not None:
                return existing["status"] == "pending"
            setting = connection.execute(
                "SELECT revision FROM weekly_publication_settings WHERE id = 1"
            ).fetchone()
            if setting is None:
                raise RuntimeError("Не инициализировано время автопубликации.")
            is_current = setting["revision"] == revision
            status = "pending" if is_current else "superseded"
            if is_current:
                connection.execute(
                    "UPDATE weekly_publication_prompts SET status = 'superseded' "
                    "WHERE chat_id = ? AND status = 'pending'",
                    (chat_id,),
                )
            connection.execute(
                "INSERT INTO weekly_publication_prompts "
                "(chat_id, message_id, revision, status) VALUES (?, ?, ?, ?)",
                (chat_id, message_id, revision, status),
            )
            return is_current

    def _get_weekly_publication_prompt_sync(
        self, chat_id: int, message_id: int
    ) -> WeeklyPublicationPrompt | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT revision, status FROM weekly_publication_prompts "
                "WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            if row is None:
                return None
            return WeeklyPublicationPrompt(revision=row["revision"], status=row["status"])

    def _complete_weekly_publication_prompt_sync(
        self,
        chat_id: int,
        message_id: int,
        value: time,
        changed_at: datetime | None,
    ) -> WeeklyPublicationSetting | None:
        value_text = self._time_text(value)
        timestamp = self._timestamp_text(changed_at)
        with self._transaction() as connection:
            prompt = connection.execute(
                "SELECT revision, status FROM weekly_publication_prompts "
                "WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            if prompt is None or prompt["status"] != "pending":
                return None
            row = connection.execute(
                "SELECT * FROM weekly_publication_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("Не инициализировано время автопубликации.")
            current = self._weekly_publication_setting_from_row(row)
            if current.revision != prompt["revision"]:
                connection.execute(
                    "UPDATE weekly_publication_prompts SET status = 'superseded' "
                    "WHERE chat_id = ? AND message_id = ? AND status = 'pending'",
                    (chat_id, message_id),
                )
                return None

            next_revision = current.revision
            updated_at = current.changed_at
            if current.weekly_publication_time != value:
                next_revision += 1
                updated_at = datetime.fromisoformat(timestamp)
                connection.execute(
                    "UPDATE weekly_publication_settings SET publication_time = ?, revision = ?, "
                    "changed_at = ? WHERE id = 1 AND revision = ?",
                    (value_text, next_revision, timestamp, current.revision),
                )
            connection.execute(
                "UPDATE weekly_publication_prompts SET status = CASE "
                "WHEN chat_id = ? AND message_id = ? THEN 'consumed' ELSE 'superseded' END "
                "WHERE status = 'pending'",
                (chat_id, message_id),
            )
            return WeeklyPublicationSetting(
                weekly_publication_time=value,
                revision=next_revision,
                changed_at=updated_at,
            )

    @staticmethod
    def _weekly_publication_setting_from_row(
        row: sqlite3.Row,
    ) -> WeeklyPublicationSetting:
        value = row["publication_time"]
        changed_at = row["changed_at"]
        if value is not None and not isinstance(value, str):
            raise ValueError("Некорректное время автопубликации в базе.")
        if changed_at is not None and not isinstance(changed_at, str):
            raise ValueError("Некорректная дата изменения автопубликации в базе.")
        return WeeklyPublicationSetting(
            weekly_publication_time=time.fromisoformat(value) if value is not None else None,
            revision=row["revision"],
            changed_at=datetime.fromisoformat(changed_at) if changed_at is not None else None,
        )

    @staticmethod
    def _time_text(value: time | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is not None or value.second != 0 or value.microsecond != 0:
            raise ValueError("Время публикации указывается строго в формате ЧЧ:ММ.")
        return value.strftime("%H:%M")

    @staticmethod
    def _timestamp_text(value: datetime | None) -> str:
        timestamp = value if value is not None else datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Дата изменения настройки должна содержать часовой пояс.")
        return timestamp.isoformat()
