from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class EventData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    event_type: str
    start_date: date
    end_date: date | None = None
    start_time: time | None = None
    place: str | None = None
    price: str | None = None
    description: str | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("Название обязательно.")
        return title

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        event_type = value.strip()
        if not event_type:
            raise ValueError("Тип события обязателен.")
        return event_type

    @field_validator("place", "price", "description", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_date_range(self) -> EventData:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("Дата окончания не может быть раньше даты начала.")
        return self


class ClassData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    first_date: date
    start_time: time
    last_date: date | None = None
    place: str | None = None
    price: str | None = None
    description: str | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("Название обязательно.")
        return title

    @field_validator("place", "price", "description", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_date_range(self) -> ClassData:
        if self.last_date is not None and self.last_date < self.first_date:
            raise ValueError("Последняя дата не может быть раньше первой даты.")
        return self


CardData = EventData | ClassData


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: int
    message_id: int
    text: str | None = None
    received_at: datetime
    telegram_file_id: str | None = None
    media_group_id: str | None = None


class Source(SourceInput):
    id: int
    status: Literal["pending", "processed", "failed"]
    error_reason: str | None = None
    poster_path: Path | None = None

    @field_validator("poster_path")
    @classmethod
    def validate_relative_poster_path(cls, path: Path | None) -> Path | None:
        if path is not None and (path.is_absolute() or ".." in path.parts):
            raise ValueError("Путь афиши должен быть относительным.")
        return path


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    record_id: int
    version: int
    base_revision: int | None
    data: CardData
    source_id: int
    poster_source_id: int | None
    status: Literal["pending", "confirmed", "rejected", "superseded"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    revision: int
    data: CardData
    poster_source_id: int | None
    status: Literal["pending", "active", "cancelled"]
