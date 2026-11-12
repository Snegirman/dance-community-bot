"""Согласованные снимки расписания и каталога."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from pathlib import Path

from ..models import Record, ScheduleException, SeriesVersion
from .base import StorageBase


class SnapshotsStorage(StorageBase):
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

    async def get_weekly_snapshot(
        self,
    ) -> tuple[
        list[Record],
        list[SeriesVersion],
        list[ScheduleException],
        dict[int, Path | None],
    ]:
        return await asyncio.to_thread(self._get_weekly_snapshot_sync)

    def _catalog_revision_from_connection(self, connection: sqlite3.Connection) -> str:
        from ..weekly import _catalog_revision

        records, versions, exceptions, poster_paths = self._weekly_snapshot_from_connection(
            connection
        )
        return _catalog_revision(records, versions, exceptions, poster_paths)

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

    def _get_weekly_snapshot_sync(
        self,
    ) -> tuple[
        list[Record],
        list[SeriesVersion],
        list[ScheduleException],
        dict[int, Path | None],
    ]:
        with self._connection() as connection:
            connection.execute("BEGIN")
            return self._weekly_snapshot_from_connection(connection)

    def _weekly_snapshot_from_connection(
        self, connection: sqlite3.Connection
    ) -> tuple[
        list[Record],
        list[SeriesVersion],
        list[ScheduleException],
        dict[int, Path | None],
    ]:
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
            "WHERE status = 'active' AND poster_source_id IS NOT NULL) "
            "ORDER BY id"
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
            [self._record_from_row(row) for row in records],
            [self._series_version_from_row(row) for row in versions],
            [self._schedule_exception_from_row(row) for row in exceptions],
            poster_paths,
        )
