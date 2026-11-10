"""Расчёт проведений регулярных занятий для выбранной недели."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, time, timedelta

from .models import Occurrence, ScheduleException, SeriesVersion


def _version_for_occurrence(
    versions: Sequence[SeriesVersion], series_id: int, original_date: date
) -> SeriesVersion | None:
    matches = [
        version
        for version in versions
        if version.series_id == series_id
        and version.effective_from <= original_date
        and (version.effective_until is None or original_date < version.effective_until)
        and version.data.first_date <= original_date
        and (version.data.last_date is None or original_date <= version.data.last_date)
        and (original_date - version.data.first_date).days % 7 == 0
    ]
    return max(matches, key=lambda version: version.revision, default=None)


def _actual_date(exception: ScheduleException | None, original_date: date) -> date:
    if exception is not None and exception.new_date is not None:
        return exception.new_date
    return original_date


def _occurrence(
    version: SeriesVersion,
    original_date: date,
    exception: ScheduleException | None,
) -> Occurrence:
    data = version.data
    transferred = exception is not None and exception.new_date is not None
    return Occurrence(
        series_id=version.series_id,
        original_date=original_date,
        actual_date=_actual_date(exception, original_date),
        start_time=(
            exception.new_time
            if transferred and exception.new_time is not None
            else data.start_time
        ),
        title=data.title,
        place=(exception.place if transferred and exception.place is not None else data.place),
        price=data.price,
        description=data.description,
        poster_source_id=version.poster_source_id,
        cancelled=exception.cancelled if exception is not None else False,
    )


def _in_week(value: date, start: date, end: date) -> bool:
    return start <= value < end


def _regular_dates(version: SeriesVersion, week_start: date, week_end: date) -> list[date]:
    first_date = max(week_start, version.effective_from, version.data.first_date)
    end_ordinal = week_end.toordinal()
    if version.effective_until is not None:
        end_ordinal = min(end_ordinal, version.effective_until.toordinal())
    if version.data.last_date is not None:
        end_ordinal = min(end_ordinal, version.data.last_date.toordinal() + 1)
    offset = (version.data.first_date.weekday() - first_date.weekday()) % 7
    return [
        date.fromordinal(ordinal)
        for ordinal in range(first_date.toordinal() + offset, end_ordinal, 7)
    ]


def _mark_conflicts(occurrences: list[Occurrence]) -> list[Occurrence]:
    matching_times: dict[tuple[date, time], list[int]] = defaultdict(list)
    for index, occurrence in enumerate(occurrences):
        if not occurrence.cancelled:
            matching_times[(occurrence.actual_date, occurrence.start_time)].append(index)

    conflicted = {
        index for indices in matching_times.values() if len(indices) > 1 for index in indices
    }
    return [
        occurrence.model_copy(update={"warnings": ("Совпадение времени с другим проведением.",)})
        if index in conflicted
        else occurrence
        for index, occurrence in enumerate(occurrences)
    ]


def expand_schedule(
    versions: Sequence[SeriesVersion],
    exceptions: Sequence[ScheduleException],
    week_start: date,
    *,
    include_cancelled: bool = False,
) -> list[Occurrence]:
    """Вернуть проведения недели; `week_start` должен быть понедельником."""
    if week_start.weekday() != 0:
        raise ValueError("Неделя должна начинаться в понедельник.")

    if (date.max - week_start).days < 7:
        raise ValueError("Границы недели выходят за допустимый диапазон дат.")
    week_end = week_start + timedelta(days=7)
    current_exceptions: dict[tuple[int, date], ScheduleException] = {}
    for exception in exceptions:
        if exception.archived:
            continue
        key = (exception.series_id, exception.original_date)
        previous = current_exceptions.get(key)
        if previous is None or exception.revision > previous.revision:
            current_exceptions[key] = exception

    occurrences: list[Occurrence] = []
    for version in versions:
        for original_date in _regular_dates(version, week_start, week_end):
            exception = current_exceptions.get((version.series_id, original_date))
            occurrence = _occurrence(version, original_date, exception)
            if exception is not None and exception.cancelled and not include_cancelled:
                continue
            if _in_week(occurrence.actual_date, week_start, week_end):
                occurrences.append(occurrence)

    for exception in current_exceptions.values():
        if _in_week(exception.original_date, week_start, week_end):
            continue
        if exception.cancelled and not include_cancelled:
            continue
        version = _version_for_occurrence(versions, exception.series_id, exception.original_date)
        if version is None:
            continue
        occurrence = _occurrence(version, exception.original_date, exception)
        if _in_week(occurrence.actual_date, week_start, week_end):
            occurrences.append(occurrence)

    occurrences.sort(key=lambda item: (item.actual_date, item.start_time, item.series_id))
    return _mark_conflicts(occurrences)
