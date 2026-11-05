from __future__ import annotations

from datetime import date, time

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
