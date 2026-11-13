"""Состояние выпусков, частей и попыток отправки."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from typing import Literal

from pydantic import TypeAdapter

from ..models import (
    PostPart,
    PublicationClaimResult,
    PublicationPartState,
    PublicationPreviewResult,
    PublicationState,
    PublicationStatus,
    WeeklyPost,
)
from .snapshots import SnapshotsStorage

_POST_PART_ADAPTER = TypeAdapter(PostPart)


class PublicationsStorage(SnapshotsStorage):
    async def prepare_publication(
        self,
        post: WeeklyPost,
        group_id: int,
        owner_chat_id: int,
        owner_message_id: int,
    ) -> PublicationPreviewResult:
        return await asyncio.to_thread(
            self._prepare_publication_sync,
            post,
            group_id,
            owner_chat_id,
            owner_message_id,
        )

    async def get_publication(self, publication_id: int) -> PublicationState | None:
        return await asyncio.to_thread(self._get_publication_sync, publication_id)

    async def get_publication_for_week(
        self, group_id: int, week_start: date
    ) -> PublicationState | None:
        return await asyncio.to_thread(self._get_publication_for_week_sync, group_id, week_start)

    async def bind_publication_message(
        self,
        publication_id: int,
        expected_preview_revision: int,
        owner_chat_id: int,
        owner_message_id: int,
    ) -> PublicationState | None:
        return await asyncio.to_thread(
            self._bind_publication_message_sync,
            publication_id,
            expected_preview_revision,
            owner_chat_id,
            owner_message_id,
        )

    async def claim_publication(
        self,
        post: WeeklyPost,
        group_id: int,
        active_token: str,
        *,
        automatic: bool = False,
        expected_publication_id: int | None = None,
        expected_preview_revision: int | None = None,
        owner_chat_id: int | None = None,
        owner_message_id: int | None = None,
    ) -> PublicationClaimResult:
        return await asyncio.to_thread(
            self._claim_publication_sync,
            post,
            group_id,
            active_token,
            automatic,
            expected_publication_id,
            expected_preview_revision,
            owner_chat_id,
            owner_message_id,
        )

    async def claim_next_publication_part(
        self, publication_id: int, active_token: str
    ) -> PublicationClaimResult:
        return await asyncio.to_thread(
            self._claim_next_publication_part_sync, publication_id, active_token
        )

    async def finish_publication_part(
        self,
        publication_id: int,
        part_index: int,
        active_token: str,
        status: Literal["sent", "failed", "unknown"],
        message_id: int | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._finish_publication_part_sync,
            publication_id,
            part_index,
            active_token,
            status,
            message_id,
        )

    async def release_publication_claim(self, publication_id: int, active_token: str) -> None:
        await asyncio.to_thread(self._release_publication_claim_sync, publication_id, active_token)

    async def resolve_publication_part(
        self,
        publication_id: int,
        part_index: int,
        delivered: bool,
        *,
        expected_attempt_revision: int | None = None,
        expected_preview_revision: int | None = None,
        owner_chat_id: int | None = None,
        owner_message_id: int | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._resolve_publication_part_sync,
            publication_id,
            part_index,
            delivered,
            expected_attempt_revision,
            expected_preview_revision,
            owner_chat_id,
            owner_message_id,
        )

    async def retry_publication_part(
        self,
        publication_id: int,
        part_index: int,
        group_id: int,
        active_token: str,
        expected_attempt_revision: int,
        expected_preview_revision: int,
        owner_chat_id: int,
        owner_message_id: int,
    ) -> PublicationClaimResult:
        return await asyncio.to_thread(
            self._retry_publication_part_sync,
            publication_id,
            part_index,
            group_id,
            active_token,
            expected_attempt_revision,
            expected_preview_revision,
            owner_chat_id,
            owner_message_id,
        )

    async def continue_publication(
        self,
        publication_id: int,
        group_id: int,
        active_token: str,
        expected_preview_revision: int,
        owner_chat_id: int,
        owner_message_id: int,
    ) -> PublicationClaimResult:
        return await asyncio.to_thread(
            self._continue_publication_sync,
            publication_id,
            group_id,
            active_token,
            expected_preview_revision,
            owner_chat_id,
            owner_message_id,
        )

    async def recover_publications_on_startup(self) -> None:
        await asyncio.to_thread(self._recover_publications_on_startup_sync)

    @staticmethod
    def _insert_publication_parts(
        connection: sqlite3.Connection, publication_id: int, post: WeeklyPost
    ) -> None:
        connection.executemany(
            "INSERT INTO publication_parts "
            "(publication_id, part_index, part_json, status) VALUES (?, ?, ?, 'prepared')",
            [
                (publication_id, index, part.model_dump_json())
                for index, part in enumerate(post.parts)
            ],
        )

    @staticmethod
    def _insert_publication(
        connection: sqlite3.Connection,
        post: WeeklyPost,
        group_id: int,
        *,
        owner_chat_id: int | None,
        owner_message_id: int | None,
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO publications "
            "(group_id, week_start, catalog_revision, snapshot_json, status, preview_revision, "
            "owner_chat_id, owner_message_id) VALUES (?, ?, ?, ?, 'prepared', 1, ?, ?)",
            (
                group_id,
                post.week_start.isoformat(),
                post.catalog_revision,
                post.model_dump_json(),
                owner_chat_id,
                owner_message_id,
            ),
        )
        publication_id = cursor.lastrowid
        if not isinstance(publication_id, int):
            raise RuntimeError("Не удалось сохранить выпуск.")
        PublicationsStorage._insert_publication_parts(connection, publication_id, post)
        return publication_id

    def _publication_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> PublicationState:
        part_rows = connection.execute(
            "SELECT * FROM publication_parts WHERE publication_id = ? ORDER BY part_index",
            (row["id"],),
        ).fetchall()
        return PublicationState(
            id=row["id"],
            group_id=row["group_id"],
            week_start=date.fromisoformat(row["week_start"]),
            catalog_revision=row["catalog_revision"],
            snapshot=WeeklyPost.model_validate_json(row["snapshot_json"]),
            status=row["status"],
            preview_revision=row["preview_revision"],
            owner_chat_id=row["owner_chat_id"],
            owner_message_id=row["owner_message_id"],
            active_token=row["active_token"],
            notification_revision=row["notification_revision"],
            notified_revision=row["notified_revision"],
            parts=tuple(
                PublicationPartState(
                    index=part_row["part_index"],
                    part=_POST_PART_ADAPTER.validate_json(part_row["part_json"]),
                    status=part_row["status"],
                    attempt_revision=part_row["attempt_revision"],
                    message_id=part_row["message_id"],
                )
                for part_row in part_rows
            ),
        )

    def _get_publication_by_id(
        self, connection: sqlite3.Connection, publication_id: int
    ) -> PublicationState | None:
        row = connection.execute(
            "SELECT * FROM publications WHERE id = ?", (publication_id,)
        ).fetchone()
        return self._publication_from_row(connection, row) if row is not None else None

    def _prepare_publication_sync(
        self,
        post: WeeklyPost,
        group_id: int,
        owner_chat_id: int,
        owner_message_id: int,
    ) -> PublicationPreviewResult:
        if post.week_start.weekday() != 0 or not post.parts:
            raise ValueError("Для публикации нужна непустая неделя, начинающаяся в понедельник.")
        with self._transaction() as connection:
            if self._catalog_revision_from_connection(connection) != post.catalog_revision:
                return PublicationPreviewResult(outcome="stale", publication=None)
            row = connection.execute(
                "SELECT * FROM publications WHERE group_id = ? AND week_start = ?",
                (group_id, post.week_start.isoformat()),
            ).fetchone()
            if row is not None and (row["status"] != "prepared" or row["active_token"] is not None):
                return PublicationPreviewResult(
                    outcome="existing", publication=self._publication_from_row(connection, row)
                )

            if row is None:
                publication_id = self._insert_publication(
                    connection,
                    post,
                    group_id,
                    owner_chat_id=owner_chat_id,
                    owner_message_id=owner_message_id,
                )
            else:
                publication_id = row["id"]
                connection.execute(
                    "UPDATE publications SET catalog_revision = ?, snapshot_json = ?, "
                    "preview_revision = preview_revision + 1, "
                    "owner_chat_id = ?, owner_message_id = ? "
                    "WHERE id = ? AND status = 'prepared' AND active_token IS NULL",
                    (
                        post.catalog_revision,
                        post.model_dump_json(),
                        owner_chat_id,
                        owner_message_id,
                        publication_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM publication_parts WHERE publication_id = ?", (publication_id,)
                )
                self._insert_publication_parts(connection, publication_id, post)

            saved = self._get_publication_by_id(connection, publication_id)
            if saved is None:
                raise RuntimeError("Не удалось прочитать подготовленный выпуск.")
            return PublicationPreviewResult(outcome="ready", publication=saved)

    def _get_publication_sync(self, publication_id: int) -> PublicationState | None:
        with self._connection() as connection:
            return self._get_publication_by_id(connection, publication_id)

    def _get_publication_for_week_sync(
        self, group_id: int, week_start: date
    ) -> PublicationState | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM publications WHERE group_id = ? AND week_start = ?",
                (group_id, week_start.isoformat()),
            ).fetchone()
            return self._publication_from_row(connection, row) if row is not None else None

    def _bind_publication_message_sync(
        self,
        publication_id: int,
        expected_preview_revision: int,
        owner_chat_id: int,
        owner_message_id: int,
    ) -> PublicationState | None:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE publications SET owner_chat_id = ?, owner_message_id = ?, "
                "preview_revision = preview_revision + 1 "
                "WHERE id = ? AND preview_revision = ?",
                (owner_chat_id, owner_message_id, publication_id, expected_preview_revision),
            )
            if cursor.rowcount != 1:
                return None
            return self._get_publication_by_id(connection, publication_id)

    def _claim_publication_sync(
        self,
        post: WeeklyPost,
        group_id: int,
        active_token: str,
        automatic: bool,
        expected_publication_id: int | None,
        expected_preview_revision: int | None,
        owner_chat_id: int | None,
        owner_message_id: int | None,
    ) -> PublicationClaimResult:
        if post.week_start.weekday() != 0 or not post.parts:
            raise ValueError("Для публикации нужна непустая неделя, начинающаяся в понедельник.")
        with self._transaction() as connection:
            if self._catalog_revision_from_connection(connection) != post.catalog_revision:
                return PublicationClaimResult(outcome="stale", publication=None, part=None)
            row = connection.execute(
                "SELECT * FROM publications WHERE group_id = ? AND week_start = ?",
                (group_id, post.week_start.isoformat()),
            ).fetchone()

            if row is None:
                if expected_publication_id is not None:
                    return PublicationClaimResult(outcome="invalid", publication=None, part=None)
                publication_id = self._insert_publication(
                    connection,
                    post,
                    group_id,
                    owner_chat_id=None,
                    owner_message_id=None,
                )
                row = connection.execute(
                    "SELECT * FROM publications WHERE id = ?", (publication_id,)
                ).fetchone()
            elif expected_publication_id is not None and (
                row["id"] != expected_publication_id
                or row["preview_revision"] != expected_preview_revision
                or row["owner_chat_id"] != owner_chat_id
                or row["owner_message_id"] != owner_message_id
            ):
                return PublicationClaimResult(
                    outcome="invalid",
                    publication=self._publication_from_row(connection, row),
                    part=None,
                )

            if row is None:
                raise RuntimeError("Не удалось прочитать выпуск для отправки.")
            publication = self._publication_from_row(connection, row)
            if publication.status != "prepared" or publication.active_token is not None:
                return PublicationClaimResult(
                    outcome="existing", publication=publication, part=None
                )
            if publication.catalog_revision != post.catalog_revision:
                if not automatic or expected_publication_id is not None:
                    return PublicationClaimResult(
                        outcome="stale", publication=publication, part=None
                    )
                connection.execute(
                    "UPDATE publications SET catalog_revision = ?, snapshot_json = ?, "
                    "preview_revision = preview_revision + 1 WHERE id = ? AND status = 'prepared'",
                    (post.catalog_revision, post.model_dump_json(), publication.id),
                )
                connection.execute(
                    "DELETE FROM publication_parts WHERE publication_id = ?", (publication.id,)
                )
                self._insert_publication_parts(connection, publication.id, post)

            cursor = connection.execute(
                "UPDATE publications SET status = 'sending', active_token = ? "
                "WHERE id = ? AND status = 'prepared' AND active_token IS NULL",
                (active_token, publication.id),
            )
            if cursor.rowcount != 1:
                current = self._get_publication_by_id(connection, publication.id)
                return PublicationClaimResult(outcome="existing", publication=current, part=None)
            next_row = connection.execute(
                "SELECT * FROM publication_parts WHERE publication_id = ? "
                "AND status = 'prepared' ORDER BY part_index LIMIT 1",
                (publication.id,),
            ).fetchone()
            if next_row is None:
                connection.execute(
                    "UPDATE publications SET status = 'delivered', active_token = NULL "
                    "WHERE id = ?",
                    (publication.id,),
                )
                current = self._get_publication_by_id(connection, publication.id)
                return PublicationClaimResult(outcome="existing", publication=current, part=None)
            connection.execute(
                "UPDATE publication_parts SET status = 'sending', "
                "attempt_revision = attempt_revision + 1 "
                "WHERE publication_id = ? AND part_index = ?",
                (publication.id, next_row["part_index"]),
            )
            saved = self._get_publication_by_id(connection, publication.id)
            if saved is None:
                raise RuntimeError("Не удалось прочитать захваченную часть выпуска.")
            part = next(item for item in saved.parts if item.index == next_row["part_index"])
            return PublicationClaimResult(outcome="claimed", publication=saved, part=part)

    def _claim_next_publication_part_sync(
        self, publication_id: int, active_token: str
    ) -> PublicationClaimResult:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM publications WHERE id = ?", (publication_id,)
            ).fetchone()
            if row is None:
                return PublicationClaimResult(outcome="invalid", publication=None, part=None)
            publication = self._publication_from_row(connection, row)
            if publication.active_token != active_token or publication.status != "sending":
                return PublicationClaimResult(
                    outcome="existing", publication=publication, part=None
                )
            next_row = connection.execute(
                "SELECT * FROM publication_parts WHERE publication_id = ? "
                "AND status = 'prepared' ORDER BY part_index LIMIT 1",
                (publication_id,),
            ).fetchone()
            if next_row is None:
                status: PublicationStatus
                if any(part.status == "unknown" for part in publication.parts):
                    status = "unknown"
                elif any(part.status == "failed" for part in publication.parts):
                    status = "partial"
                else:
                    status = "delivered"
                connection.execute(
                    "UPDATE publications SET status = ?, active_token = NULL WHERE id = ?",
                    (status, publication_id),
                )
                saved = self._get_publication_by_id(connection, publication_id)
                return PublicationClaimResult(outcome="existing", publication=saved, part=None)
            connection.execute(
                "UPDATE publication_parts SET status = 'sending', "
                "attempt_revision = attempt_revision + 1 "
                "WHERE publication_id = ? AND part_index = ?",
                (publication_id, next_row["part_index"]),
            )
            saved = self._get_publication_by_id(connection, publication_id)
            if saved is None:
                raise RuntimeError("Не удалось прочитать захваченную часть выпуска.")
            part = next(item for item in saved.parts if item.index == next_row["part_index"])
            return PublicationClaimResult(outcome="claimed", publication=saved, part=part)

    def _finish_publication_part_sync(
        self,
        publication_id: int,
        part_index: int,
        active_token: str,
        status: Literal["sent", "failed", "unknown"],
        message_id: int | None,
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT active_token FROM publications WHERE id = ?", (publication_id,)
            ).fetchone()
            if row is None or row["active_token"] != active_token:
                raise ValueError("Захват выпуска устарел.")
            cursor = connection.execute(
                "UPDATE publication_parts SET status = ?, message_id = ? "
                "WHERE publication_id = ? AND part_index = ? AND status = 'sending'",
                (status, message_id if status == "sent" else None, publication_id, part_index),
            )
            if cursor.rowcount != 1:
                raise ValueError("Часть выпуска больше не отправляется.")
            if status in ("failed", "unknown"):
                publication_status = "unknown" if status == "unknown" else "partial"
                connection.execute(
                    "UPDATE publications SET status = ? WHERE id = ?",
                    (publication_status, publication_id),
                )

    def _release_publication_claim_sync(self, publication_id: int, active_token: str) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT active_token FROM publications WHERE id = ?", (publication_id,)
            ).fetchone()
            if row is None or row["active_token"] != active_token:
                return
            connection.execute(
                "UPDATE publication_parts SET status = 'unknown' "
                "WHERE publication_id = ? AND status = 'sending'",
                (publication_id,),
            )
            parts = connection.execute(
                "SELECT status FROM publication_parts WHERE publication_id = ?",
                (publication_id,),
            ).fetchall()
            statuses = [part["status"] for part in parts]
            if "unknown" in statuses:
                status: PublicationStatus = "unknown"
            elif "failed" in statuses:
                status = "partial"
            elif all(part_status == "sent" for part_status in statuses):
                status = "delivered"
            else:
                status = "partial"
            connection.execute(
                "UPDATE publications SET status = ?, active_token = NULL WHERE id = ?",
                (status, publication_id),
            )

    def _resolve_publication_part_sync(
        self,
        publication_id: int,
        part_index: int,
        delivered: bool,
        expected_attempt_revision: int | None,
        expected_preview_revision: int | None,
        owner_chat_id: int | None,
        owner_message_id: int | None,
    ) -> None:
        with self._transaction() as connection:
            publication_row = connection.execute(
                "SELECT * FROM publications WHERE id = ?", (publication_id,)
            ).fetchone()
            if publication_row is None:
                raise ValueError("Неизвестный выпуск.")
            if expected_preview_revision is not None and (
                publication_row["preview_revision"] != expected_preview_revision
                or publication_row["owner_chat_id"] != owner_chat_id
                or publication_row["owner_message_id"] != owner_message_id
            ):
                raise ValueError("Кнопка относится к устаревшему предпросмотру.")
            part_row = connection.execute(
                "SELECT status, attempt_revision FROM publication_parts "
                "WHERE publication_id = ? AND part_index = ?",
                (publication_id, part_index),
            ).fetchone()
            if (
                part_row is None
                or part_row["status"] != "unknown"
                or publication_row["active_token"] is not None
                or (
                    expected_attempt_revision is not None
                    and (part_row["attempt_revision"] != expected_attempt_revision)
                )
            ):
                raise ValueError("Решение по этой части уже принято или устарело.")
            connection.execute(
                "UPDATE publication_parts SET status = ?, message_id = NULL "
                "WHERE publication_id = ? AND part_index = ? AND status = 'unknown'",
                ("sent" if delivered else "failed", publication_id, part_index),
            )
            statuses = connection.execute(
                "SELECT status FROM publication_parts WHERE publication_id = ?",
                (publication_id,),
            ).fetchall()
            values = [row["status"] for row in statuses]
            if "unknown" in values:
                status: PublicationStatus = "unknown"
            elif "prepared" in values or "failed" in values:
                status = "partial"
            else:
                status = "delivered"
            connection.execute(
                "UPDATE publications SET status = ? WHERE id = ?", (status, publication_id)
            )

    def _retry_publication_part_sync(
        self,
        publication_id: int,
        part_index: int,
        group_id: int,
        active_token: str,
        expected_attempt_revision: int,
        expected_preview_revision: int,
        owner_chat_id: int,
        owner_message_id: int,
    ) -> PublicationClaimResult:
        with self._transaction() as connection:
            publication_row = connection.execute(
                "SELECT * FROM publications WHERE id = ?", (publication_id,)
            ).fetchone()
            if publication_row is None:
                return PublicationClaimResult(outcome="invalid", publication=None, part=None)
            publication = self._publication_from_row(connection, publication_row)
            if (
                publication.group_id != group_id
                or publication.status != "partial"
                or publication.active_token is not None
                or publication.preview_revision != expected_preview_revision
                or publication.owner_chat_id != owner_chat_id
                or publication.owner_message_id != owner_message_id
            ):
                return PublicationClaimResult(outcome="invalid", publication=publication, part=None)
            part_row = connection.execute(
                "SELECT * FROM publication_parts WHERE publication_id = ? AND part_index = ?",
                (publication_id, part_index),
            ).fetchone()
            if (
                part_row is None
                or part_row["status"] != "failed"
                or part_row["attempt_revision"] != expected_attempt_revision
            ):
                return PublicationClaimResult(outcome="invalid", publication=publication, part=None)
            connection.execute(
                "UPDATE publication_parts SET status = 'sending', "
                "attempt_revision = attempt_revision + 1 "
                "WHERE publication_id = ? AND part_index = ?",
                (publication_id, part_index),
            )
            connection.execute(
                "UPDATE publications SET status = 'sending', active_token = ? WHERE id = ?",
                (active_token, publication_id),
            )
            saved = self._get_publication_by_id(connection, publication_id)
            if saved is None:
                raise RuntimeError("Не удалось прочитать часть выпуска для повтора.")
            part = next(item for item in saved.parts if item.index == part_index)
            return PublicationClaimResult(outcome="claimed", publication=saved, part=part)

    def _continue_publication_sync(
        self,
        publication_id: int,
        group_id: int,
        active_token: str,
        expected_preview_revision: int,
        owner_chat_id: int,
        owner_message_id: int,
    ) -> PublicationClaimResult:
        with self._transaction() as connection:
            publication_row = connection.execute(
                "SELECT * FROM publications WHERE id = ?", (publication_id,)
            ).fetchone()
            if publication_row is None:
                return PublicationClaimResult(outcome="invalid", publication=None, part=None)
            publication = self._publication_from_row(connection, publication_row)
            if (
                publication.group_id != group_id
                or publication.status != "partial"
                or publication.active_token is not None
                or publication.preview_revision != expected_preview_revision
                or publication.owner_chat_id != owner_chat_id
                or publication.owner_message_id != owner_message_id
                or any(part.status in ("failed", "unknown") for part in publication.parts)
            ):
                return PublicationClaimResult(outcome="invalid", publication=publication, part=None)
            next_part = next(
                (part for part in publication.parts if part.status == "prepared"), None
            )
            if next_part is None:
                connection.execute(
                    "UPDATE publications SET status = 'delivered' WHERE id = ?",
                    (publication_id,),
                )
                saved = self._get_publication_by_id(connection, publication_id)
                return PublicationClaimResult(outcome="existing", publication=saved, part=None)
            connection.execute(
                "UPDATE publication_parts SET status = 'sending', "
                "attempt_revision = attempt_revision + 1 "
                "WHERE publication_id = ? AND part_index = ? AND status = 'prepared'",
                (publication_id, next_part.index),
            )
            connection.execute(
                "UPDATE publications SET status = 'sending', active_token = ? WHERE id = ?",
                (active_token, publication_id),
            )
            saved = self._get_publication_by_id(connection, publication_id)
            if saved is None:
                raise RuntimeError("Не удалось прочитать часть выпуска для продолжения.")
            claimed_part = next(item for item in saved.parts if item.index == next_part.index)
            return PublicationClaimResult(outcome="claimed", publication=saved, part=claimed_part)

    def _recover_publications_on_startup_sync(self) -> None:
        with self._transaction() as connection:
            active = connection.execute(
                "SELECT DISTINCT publication_id FROM publication_parts WHERE status = 'sending'"
            ).fetchall()
            connection.execute(
                "UPDATE publication_parts SET status = 'unknown' WHERE status = 'sending'"
            )
            for row in active:
                connection.execute(
                    "UPDATE publications SET status = 'unknown', active_token = NULL WHERE id = ?",
                    (row["publication_id"],),
                )
            interrupted = connection.execute(
                "SELECT id FROM publications WHERE active_token IS NOT NULL"
            ).fetchall()
            for row in interrupted:
                statuses = connection.execute(
                    "SELECT status FROM publication_parts WHERE publication_id = ?",
                    (row["id"],),
                ).fetchall()
                values = [part["status"] for part in statuses]
                if "unknown" in values:
                    status = "unknown"
                elif "failed" in values:
                    status = "partial"
                elif all(value == "sent" for value in values):
                    status = "delivered"
                else:
                    status = "partial"
                connection.execute(
                    "UPDATE publications SET status = ?, active_token = NULL WHERE id = ?",
                    (status, row["id"]),
                )
