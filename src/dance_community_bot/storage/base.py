"""Соединения SQLite, транзакции и общие преобразования данных."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, time
from importlib.resources import files
from pathlib import Path

from ..models import CardData, ClassData, EventData, Record, ScheduleException, SeriesVersion


class StorageBase:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

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
            if schema_version not in (0, 5):
                raise RuntimeError(
                    f"Неподдерживаемая версия схемы базы данных: {schema_version}. Ожидается 5."
                )
            if schema_version == 0:
                for statement in schema.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("PRAGMA user_version = 5")

    @staticmethod
    def _day_after(value: date | None) -> date | None:
        if value is None or value == date.max:
            return None
        return date.fromordinal(value.toordinal() + 1)

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
