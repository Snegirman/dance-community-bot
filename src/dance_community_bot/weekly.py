"""Снимок подтверждённых записей и предпросмотр публикации за неделю."""

from __future__ import annotations

import hashlib
import html
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, Message

from .models import (
    EventData,
    Occurrence,
    PhotoPostPart,
    PostPart,
    Record,
    ScheduleException,
    SeriesVersion,
    TextPostPart,
    WeeklyPost,
)
from .schedule import expand_schedule
from .storage import Store
from .templates import parse_strict_date

_MOSCOW = ZoneInfo("Europe/Moscow")
_TEXT_LIMIT = 4096
_CAPTION_LIMIT = 1024


class WeeklyRangeError(ValueError):
    """Неделю нельзя представить границами календаря Python."""


def week_bounds(value: date) -> tuple[datetime, datetime]:
    """Вернуть timezone-aware границы понедельника в московском времени."""
    start = value - timedelta(days=value.weekday())
    try:
        end = start + timedelta(days=7)
    except OverflowError as error:
        raise WeeklyRangeError("Границы недели выходят за допустимый диапазон дат.") from error
    return (
        datetime.combine(start, time.min, _MOSCOW),
        datetime.combine(end, time.min, _MOSCOW),
    )


def _date_label(value: date) -> str:
    return f"{value.day:02d}.{value.month:02d}.{value.year:04d}"


def _time_label(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _split_text(value: str, limit: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(value):
        end = start
        units = 0
        has_content = False
        line_boundary: int | None = None
        while end < len(value):
            character = value[end]
            character_units = 2 if ord(character) > 0xFFFF else 1
            if units + character_units > limit:
                break
            units += character_units
            has_content = has_content or not character.isspace()
            end += 1
            if character == "\n" and has_content:
                line_boundary = end
        if end == len(value):
            chunks.append(value[start:end])
            break
        boundary = line_boundary if line_boundary is not None else end
        if boundary == start:
            boundary = end
        chunks.append(value[start:boundary])
        start = boundary
    return chunks


def _catalog_revision(
    records: list[Record],
    versions: list[SeriesVersion],
    exceptions: list[ScheduleException],
    poster_paths: dict[int, Path | None],
) -> str:
    snapshot = {
        "records": [record.model_dump(mode="json") for record in records],
        "schedule_versions": [version.model_dump(mode="json") for version in versions],
        "schedule_exceptions": [exception.model_dump(mode="json") for exception in exceptions],
        "poster_paths": {
            str(source_id): path.as_posix() if path is not None else None
            for source_id, path in poster_paths.items()
        },
    }
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _event_text(data: EventData) -> str:
    last_date = data.end_date or data.start_date
    date_range = _date_label(data.start_date)
    if last_date != data.start_date:
        date_range = f"{date_range} — {_date_label(last_date)}"
    lines = [
        data.title,
        f"{date_range} · {data.event_type}",
        f"Время: {_time_label(data.start_time) if data.start_time is not None else 'не указано'}",
        f"Место: {data.place or 'не указано'}",
        f"Стоимость: {data.price or 'не указана'}",
    ]
    if data.description is not None:
        lines.append(f"Описание: {data.description}")
    return "\n".join(lines)


def _occurrence_text(occurrence: Occurrence) -> str:
    lines = [
        occurrence.title,
        _date_label(occurrence.actual_date),
        f"Время: {_time_label(occurrence.start_time)}",
        f"Место: {occurrence.place or 'не указано'}",
        f"Стоимость: {occurrence.price or 'не указана'}",
    ]
    if occurrence.description is not None:
        lines.append(f"Описание: {occurrence.description}")
    return "\n".join(lines)


def _append_text(parts: list[PostPart], value: str, limit: int = _TEXT_LIMIT) -> None:
    parts.extend(TextPostPart(text=chunk) for chunk in _split_text(value, limit))


def _section_parts(
    title: str,
    entries: list[tuple[str, int | None]],
    poster_paths: dict[int, Path | None],
) -> list[PostPart]:
    parts: list[PostPart] = []
    text_buffer = title

    for text, poster_source_id in entries:
        poster_path = poster_paths.get(poster_source_id) if poster_source_id is not None else None
        if poster_source_id is None:
            text_buffer = f"{text_buffer}\n\n{text}"
            continue
        if poster_path is None:
            text_buffer = f"{text_buffer}\n\n{text}\nАфиша недоступна."
            continue

        _append_text(parts, text_buffer)
        text_buffer = ""
        caption_chunks = _split_text(text, _CAPTION_LIMIT)
        if caption_chunks:
            parts.append(
                PhotoPostPart(
                    poster_source_id=poster_source_id,
                    poster_path=poster_path,
                    caption=caption_chunks[0],
                )
            )
            remaining = text[len(caption_chunks[0]) :]
            if remaining:
                text_buffer = remaining

    if text_buffer:
        _append_text(parts, text_buffer)
    return parts


async def build_weekly_post(week_start: date, store: Store) -> WeeklyPost:
    """Собрать сериализуемый выпуск из одного согласованного снимка Store."""
    if week_start.weekday() != 0:
        raise ValueError("Неделя должна начинаться в понедельник.")
    start, end = week_bounds(week_start)
    records, versions, exceptions, poster_paths = await store.get_weekly_snapshot()
    catalog_revision = _catalog_revision(records, versions, exceptions, poster_paths)

    week_last = date.fromordinal(end.date().toordinal() - 1)
    occurrences = expand_schedule(versions, exceptions, week_start)
    occurrences.sort(
        key=lambda item: (
            item.actual_date,
            item.start_time,
            item.series_id,
            item.original_date,
        )
    )

    events: list[tuple[Record, EventData]] = []
    for record in records:
        if not isinstance(record.data, EventData):
            continue
        if record.data.start_date > week_last:
            continue
        if (record.data.end_date or record.data.start_date) < start.date():
            continue
        events.append((record, record.data))
    events.sort(
        key=lambda item: (
            item[1].start_date,
            item[1].start_time is None,
            item[1].start_time or time.min,
            item[0].id,
        )
    )

    parts: list[PostPart] = []
    if occurrences:
        parts.extend(
            _section_parts(
                "Занятия",
                [
                    (_occurrence_text(occurrence), occurrence.poster_source_id)
                    for occurrence in occurrences
                ],
                poster_paths,
            )
        )
    if events:
        parts.extend(
            _section_parts(
                "События",
                [
                    (
                        _event_text(data),
                        record.poster_source_id,
                    )
                    for record, data in events
                ],
                poster_paths,
            )
        )

    return WeeklyPost(
        week_start=week_start,
        catalog_revision=catalog_revision,
        parts=tuple(parts),
        records=tuple(record for record, _ in events),
        occurrences=tuple(occurrences),
    )


async def preview_command_handler(message: Message, store: Store) -> None:
    """Показать владельцу все части предпросмотра выбранной московской недели."""
    text = message.text or ""
    command_parts = text.split(maxsplit=1)
    argument = command_parts[1].strip() if len(command_parts) == 2 else ""
    if argument:
        try:
            selected_date = parse_strict_date(
                argument,
                format_error="Введите дату строго в формате ДД.ММ.ГГГГ.",
                invalid_error="Такой календарной даты не существует.",
            )
        except ValueError as error:
            await message.answer(str(error))
            return
    else:
        selected_date = datetime.now(_MOSCOW).date()

    try:
        week_start = week_bounds(selected_date)[0].date()
        post = await build_weekly_post(week_start, store)
    except WeeklyRangeError as error:
        await message.answer(str(error))
        return

    if not post.records and not post.occurrences:
        await message.answer("На эту неделю записей нет.")
        return

    for part in post.parts:
        if isinstance(part, TextPostPart):
            await message.answer(html.escape(part.text), parse_mode=ParseMode.HTML)
        else:
            await message.answer_photo(
                FSInputFile(store.path.parent / part.poster_path),
                caption=html.escape(part.caption),
                parse_mode=ParseMode.HTML,
            )
