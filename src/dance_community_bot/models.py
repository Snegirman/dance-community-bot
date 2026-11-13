from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class WeeklyPublicationSetting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    weekly_publication_time: time | None
    revision: int
    changed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_setting(self) -> Self:
        if self.revision < 0:
            raise ValueError("Ревизия настройки публикации не может быть отрицательной.")
        if self.weekly_publication_time is not None and (
            self.weekly_publication_time.tzinfo is not None
            or self.weekly_publication_time.second != 0
            or self.weekly_publication_time.microsecond != 0
        ):
            raise ValueError("Время публикации должно быть задано с точностью до минуты.")
        if self.changed_at is not None and (
            self.changed_at.tzinfo is None or self.changed_at.utcoffset() is None
        ):
            raise ValueError("Дата изменения настройки должна содержать часовой пояс.")
        return self


class WeeklyPublicationPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int
    status: Literal["pending", "consumed", "superseded"]


class SeriesVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: int
    revision: int
    effective_from: date
    effective_until: date | None = None
    data: ClassData
    poster_source_id: int | None = None
    source_id: int | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> SeriesVersion:
        if self.revision < 1:
            raise ValueError("Ревизия расписания должна быть положительной.")
        if self.data.first_date < self.effective_from:
            raise ValueError("Первая дата занятия не может быть раньше начала версии.")
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("Конец версии должен быть позже её начала.")
        if self.data.last_date is not None and self.effective_from > self.data.last_date:
            raise ValueError("Версия расписания не содержит возможных занятий.")
        return self


class ScheduleException(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: int
    original_date: date
    revision: int
    cancelled: bool = False
    new_date: date | None = None
    new_time: time | None = None
    place: str | None = None
    source_id: int | None = None
    archived: bool = False

    @field_validator("place", mode="before")
    @classmethod
    def normalize_place(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_transfer(self) -> ScheduleException:
        if self.revision < 1:
            raise ValueError("Ревизия исключения должна быть положительной.")
        if (self.new_date is None) != (self.new_time is None):
            raise ValueError("Для переноса укажите дату и время вместе.")
        if not self.cancelled and self.new_date is None:
            raise ValueError("Перенос должен содержать новую дату и время.")
        return self


class Occurrence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: int
    original_date: date
    actual_date: date
    start_time: time
    title: str
    place: str | None
    price: str | None
    description: str | None
    poster_source_id: int | None
    cancelled: bool = False
    warnings: tuple[str, ...] = ()


class OccurrenceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: int = Field(gt=0)
    expected_revision: int = Field(gt=0)
    original_date: date
    expected_exception_revision: int | None = Field(default=None, gt=0)
    source_id: int | None = None


class CancelOccurrence(OccurrenceChange):
    action: Literal["cancel_occurrence"]


class TransferOccurrence(OccurrenceChange):
    action: Literal["transfer_occurrence"]
    new_date: date
    new_time: time
    place: str | None = None

    @field_validator("place", mode="before")
    @classmethod
    def normalize_place(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class RemoveException(OccurrenceChange):
    action: Literal["remove_exception"]


ScheduleResolution = Literal["remove", "detach"]


class ChangeSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["change_series"]
    series_id: int = Field(gt=0)
    expected_revision: int = Field(gt=0)
    effective_from: date
    data: ClassData
    source_id: int | None = None
    poster_source_id: int | None = None
    remove_poster: bool = False
    resolutions: dict[date, ScheduleResolution] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_first_date(self) -> ChangeSeries:
        if self.data.first_date < self.effective_from:
            raise ValueError("Первая дата нового расписания не может быть раньше даты действия.")
        if self.remove_poster and self.poster_source_id is not None:
            raise ValueError("Нельзя одновременно заменить и убрать афишу.")
        return self


class EndSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["end_series"]
    series_id: int = Field(gt=0)
    expected_revision: int = Field(gt=0)
    last_date: date
    source_id: int | None = None
    resolutions: dict[date, ScheduleResolution] = Field(default_factory=dict)


ScheduleChange = Annotated[
    CancelOccurrence | TransferOccurrence | RemoveException | ChangeSeries | EndSeries,
    Field(discriminator="action"),
]


class ChangeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    version: int
    base_revision: int
    change: ScheduleChange
    affected_exceptions: list[ScheduleException] = Field(default_factory=list)
    superseded_versions: list[SeriesVersion] = Field(default_factory=list)
    status: Literal["pending", "confirmed", "superseded"] = "pending"


CardData = EventData | ClassData


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: int
    message_id: int
    text: str | None = None
    received_at: datetime
    telegram_file_id: str | None = None
    is_photo: bool = False
    media_group_id: str | None = None
    reply_to_message_id: int | None = None


class Source(SourceInput):
    id: int
    status: Literal["pending", "processed", "failed"]
    error_reason: str | None = None
    poster_path: Path | None = None
    attachment_status: Literal["none", "pending", "saved", "failed", "removed"]

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


class TextPostPart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["text"] = "text"
    text: str


class PhotoPostPart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["photo"] = "photo"
    poster_source_id: int
    poster_path: Path
    caption: str

    @field_validator("poster_path")
    @classmethod
    def validate_relative_poster_path(cls, path: Path) -> Path:
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Путь афиши должен быть относительным.")
        return path


PostPart = Annotated[TextPostPart | PhotoPostPart, Field(discriminator="kind")]


class WeeklyPost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    week_start: date
    catalog_revision: str
    parts: tuple[PostPart, ...]
    records: tuple[Record, ...]
    occurrences: tuple[Occurrence, ...]


PublicationStatus = Literal["prepared", "sending", "partial", "unknown", "delivered"]
PublicationPartStatus = Literal["prepared", "sending", "sent", "failed", "unknown"]


class PublicationPartState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int
    part: PostPart
    status: PublicationPartStatus
    attempt_revision: int
    message_id: int | None


class PublicationState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    group_id: int
    week_start: date
    catalog_revision: str
    snapshot: WeeklyPost
    status: PublicationStatus
    preview_revision: int
    owner_chat_id: int | None
    owner_message_id: int | None
    active_token: str | None
    notification_revision: int = 0
    notified_revision: int = 0
    parts: tuple[PublicationPartState, ...]


class PublicationChangeNotification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    publication_id: int
    revision: int
    text: str


class SchedulerNotice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    week_start: date
    kind: Literal["empty", "missed"]
    text: str
    status: Literal["pending", "sending", "sent"]
    setting_revision: int = 0


class AutomationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    incomplete_attachments: int
    publications: dict[str, int]
    pending_publication_notifications: int
    pending_scheduler_notices: int


class PublicationPreviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["ready", "stale", "existing"]
    publication: PublicationState | None


class PublicationClaimResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["claimed", "stale", "existing", "invalid"]
    publication: PublicationState | None
    part: PublicationPartState | None


CardActionName = Literal[
    "confirm",
    "edit",
    "reject",
    "cancel",
    "retry_poster",
    "remove_poster",
    "events_page",
    "view",
    "schedule_cancel",
    "schedule_transfer",
    "schedule_remove_exception",
    "schedule_change_series",
    "schedule_end_series",
    "schedule_confirm",
    "schedule_resolve_remove",
    "schedule_resolve_detach",
    "schedule_cancel_draft",
]


class CardBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: int
    bot_message_id: int
    record_id: int | None = None
    draft_id: int | None = None
    version: int | None = None
    revision: int | None = None
    source_id: int | None = None
    page: int | None = None
    schedule_action: str | None = None
    schedule_series_id: int | None = None
    schedule_revision: int | None = None
    schedule_original_date: date | None = None
    expected_exception_revision: int | None = None
    schedule_draft_id: int | None = None
    schedule_draft_version: int | None = None


class CardAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    action: CardActionName
    chat_id: int | None = None
    bot_message_id: int | None = None
    record_id: int | None = None
    draft_id: int | None = None
    version: int | None = None
    revision: int | None = None
    source_id: int | None = None
    page: int | None = None
    series_id: int | None = None
    schedule_revision: int | None = None
    original_date: date | None = None
    expected_exception_revision: int | None = None
    schedule_draft_id: int | None = None
    schedule_draft_version: int | None = None
    remove_poster: bool = False
    completed: bool = False
    outcome: str | None = None
