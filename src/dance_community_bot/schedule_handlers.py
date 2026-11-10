"""Команды и действия владельца для управления регулярным расписанием."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .cards import _chunks, format_card
from .models import (
    CancelOccurrence,
    CardAction,
    CardActionName,
    CardBinding,
    ChangeDraft,
    ChangeSeries,
    ClassData,
    EndSeries,
    Occurrence,
    OccurrenceChange,
    RemoveException,
    ScheduleChange,
    ScheduleException,
    SeriesVersion,
    Source,
    TransferOccurrence,
)
from .schedule import expand_schedule
from .storage import Store
from .templates import TemplateError, parse_class_template

_MOSCOW = ZoneInfo("Europe/Moscow")
_DATE_PATTERN = re.compile(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}\Z")
_TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


def parse_schedule_date(value: str) -> date:
    """Проверить дату в строгом формате ДД.ММ.ГГГГ."""
    if _DATE_PATTERN.fullmatch(value) is None:
        raise ValueError("Введите дату строго в формате ДД.ММ.ГГГГ.")
    day, month, year = (int(part) for part in value.split("."))
    try:
        return date(year, month, day)
    except ValueError as error:
        raise ValueError("Такой календарной даты не существует.") from error


def _date_label(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _time_label(value: time) -> str:
    return value.strftime("%H:%M")


def _display_date(occurrence: Occurrence, start: date) -> date:
    end = date.fromordinal(start.toordinal() + 7)
    if start <= occurrence.actual_date < end:
        return occurrence.actual_date
    return occurrence.original_date


def _management_occurrences(
    versions: list[SeriesVersion],
    exceptions: list[ScheduleException],
    week_start: date,
) -> list[Occurrence]:
    by_key: dict[tuple[int, date], Occurrence] = {}
    exception_by_key = {
        (exception.series_id, exception.original_date): exception for exception in exceptions
    }
    for occurrence in expand_schedule(versions, [], week_start, include_cancelled=True):
        exception = exception_by_key.get((occurrence.series_id, occurrence.original_date))
        if exception is None:
            by_key[(occurrence.series_id, occurrence.original_date)] = occurrence
            continue
        is_transfer = exception.new_date is not None
        by_key[(occurrence.series_id, occurrence.original_date)] = occurrence.model_copy(
            update={
                "actual_date": exception.new_date or exception.original_date,
                "start_time": exception.new_time
                if is_transfer and exception.new_time
                else occurrence.start_time,
                "place": exception.place if is_transfer and exception.place else occurrence.place,
                "cancelled": exception.cancelled,
            }
        )

    for occurrence in expand_schedule(versions, exceptions, week_start, include_cancelled=True):
        by_key[(occurrence.series_id, occurrence.original_date)] = occurrence

    visible = list(by_key.values())
    visible.sort(
        key=lambda occurrence: (
            _display_date(occurrence, week_start),
            occurrence.start_time,
            occurrence.series_id,
            occurrence.original_date,
        )
    )
    return visible


async def _button(
    store: Store,
    title: str,
    action_name: CardActionName,
    *,
    series_id: int | None = None,
    schedule_revision: int | None = None,
    original_date: date | None = None,
    expected_exception_revision: int | None = None,
    schedule_draft_id: int | None = None,
    schedule_draft_version: int | None = None,
    remove_poster: bool = False,
) -> InlineKeyboardButton:
    action = await store.create_card_action(
        action_name,
        series_id=series_id,
        schedule_revision=schedule_revision,
        original_date=original_date,
        expected_exception_revision=expected_exception_revision,
        schedule_draft_id=schedule_draft_id,
        schedule_draft_version=schedule_draft_version,
        remove_poster=remove_poster,
    )
    return InlineKeyboardButton(text=title, callback_data=f"a:{action.token}")


async def _send_bound(
    message: Message,
    text: str,
    store: Store,
    *,
    keyboard: InlineKeyboardMarkup | None = None,
    binding: CardBinding | None = None,
) -> None:
    for index, chunk in enumerate(_chunks(text)):
        sent = await message.answer(chunk, reply_markup=keyboard if index == 0 else None)
        if binding is not None:
            await store.bind_card_message(
                binding.model_copy(
                    update={"chat_id": sent.chat.id, "bot_message_id": sent.message_id}
                )
            )
        if index == 0 and keyboard is not None:
            tokens = [
                button.callback_data.removeprefix("a:")
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data is not None and button.callback_data.startswith("a:")
            ]
            if tokens:
                await store.bind_card_actions(sent.chat.id, sent.message_id, tokens)


def _occurrence_text(occurrence: Occurrence, display_date: date) -> str:
    lines = [
        f"Проведение · {_date_label(display_date)} · {_time_label(occurrence.start_time)}",
        f"Название: {occurrence.title}",
        f"Серия: №{occurrence.series_id}",
        f"Исходная дата: {_date_label(occurrence.original_date)}",
        f"Фактическая дата: {_date_label(occurrence.actual_date)}",
        f"Место: {occurrence.place or 'не указано'}",
        f"Стоимость: {occurrence.price or 'не указана'}",
    ]
    if occurrence.cancelled:
        lines.append("Статус: отменено")
    elif occurrence.actual_date != occurrence.original_date:
        lines.append("Статус: перенесено")
    if occurrence.warnings:
        lines.extend(f"Предупреждение: {warning}" for warning in occurrence.warnings)
    return "\n".join(lines)


async def show_schedule_week(message: Message, store: Store, week_start: date) -> None:
    """Показать полную неделю, включая отменённые даты и исходные даты переносов."""
    versions, exceptions, exception_revisions, records = await store.get_schedule_snapshot()
    series_records = {record.id: record for record in records}
    try:
        occurrences = _management_occurrences(versions, exceptions, week_start)
    except ValueError as error:
        await message.answer(str(error))
        return

    last_weekday = date.fromordinal(week_start.toordinal() + 6)
    await message.answer(f"Расписание · {_date_label(week_start)} — {_date_label(last_weekday)}")
    if not versions:
        await message.answer("Регулярных занятий пока нет.")
        return

    versions_by_series: dict[int, list[SeriesVersion]] = {}
    for version in versions:
        versions_by_series.setdefault(version.series_id, []).append(version)
    for series_id, series_versions in sorted(versions_by_series.items()):
        record = series_records.get(series_id)
        if record is None:
            continue
        schedule_revision = record.revision
        lines = [f"Серия №{series_id} · ревизия {schedule_revision}"]
        for version in sorted(series_versions, key=lambda item: item.revision):
            end_label = (
                _date_label(date.fromordinal(version.effective_until.toordinal() - 1))
                if version.effective_until is not None
                else "без окончания"
            )
            lines.append(
                f"\nВерсия {version.revision} · действует "
                f"{_date_label(version.effective_from)} — {end_label}\n"
                f"{format_card(version.data)}"
            )
        row = [
            await _button(
                store,
                "Изменить серию",
                "schedule_change_series",
                series_id=series_id,
                schedule_revision=schedule_revision,
            ),
            await _button(
                store,
                "Завершить серию",
                "schedule_end_series",
                series_id=series_id,
                schedule_revision=schedule_revision,
            ),
        ]
        if record.poster_source_id is not None:
            row.append(
                await _button(
                    store,
                    "Убрать афишу",
                    "schedule_change_series",
                    series_id=series_id,
                    schedule_revision=schedule_revision,
                    remove_poster=True,
                )
            )
        await _send_bound(
            message,
            "\n".join(lines),
            store,
            keyboard=InlineKeyboardMarkup(inline_keyboard=[row]),
        )

    exception_keys = {(item.series_id, item.original_date) for item in exceptions}
    for occurrence in occurrences:
        record = series_records.get(occurrence.series_id)
        if record is None:
            continue
        schedule_revision = record.revision
        exception_revision = exception_revisions.get(
            (occurrence.series_id, occurrence.original_date)
        )
        buttons: list[InlineKeyboardButton] = []
        if not occurrence.cancelled:
            buttons.extend(
                [
                    await _button(
                        store,
                        "Отменить",
                        "schedule_cancel",
                        series_id=occurrence.series_id,
                        schedule_revision=schedule_revision,
                        original_date=occurrence.original_date,
                        expected_exception_revision=exception_revision,
                    ),
                    await _button(
                        store,
                        "Перенести",
                        "schedule_transfer",
                        series_id=occurrence.series_id,
                        schedule_revision=schedule_revision,
                        original_date=occurrence.original_date,
                        expected_exception_revision=exception_revision,
                    ),
                ]
            )
        if (occurrence.series_id, occurrence.original_date) in exception_keys:
            buttons.append(
                await _button(
                    store,
                    "Убрать исключение",
                    "schedule_remove_exception",
                    series_id=occurrence.series_id,
                    schedule_revision=schedule_revision,
                    original_date=occurrence.original_date,
                    expected_exception_revision=exception_revision,
                )
            )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
        await _send_bound(
            message,
            _occurrence_text(occurrence, _display_date(occurrence, week_start)),
            store,
            keyboard=keyboard,
        )
    if not occurrences:
        await message.answer("На этой неделе нет регулярных проведений.")


async def schedule_command_handler(message: Message, store: Store) -> None:
    """Показать текущую московскую неделю либо неделю выбранной даты."""
    parts = (message.text or "").split()
    try:
        if len(parts) == 1:
            selected_date = datetime.now(_MOSCOW).date()
        elif len(parts) == 2:
            selected_date = parse_schedule_date(parts[1])
        else:
            raise ValueError("Используйте /schedule или /schedule ДД.ММ.ГГГГ.")
    except ValueError as error:
        await message.answer(str(error))
        return
    week_start = date.fromordinal(selected_date.toordinal() - selected_date.weekday())
    await show_schedule_week(message, store, week_start)


def _parse_transfer_text(text: str) -> tuple[date, time, str | None]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        label, separator, value = line.partition(":")
        key = label.strip().casefold()
        if not separator or key not in {"дата", "время", "место"}:
            raise ValueError("Используйте поля «Дата», «Время» и необязательное «Место».")
        if key in values:
            raise ValueError(f"Поле «{label.strip()}» указано несколько раз.")
        values[key] = value.strip()
    if set(values) - {"дата", "время", "место"} or "дата" not in values or "время" not in values:
        raise ValueError("Укажите новую дату и время; место можно оставить пустым.")
    if _TIME_PATTERN.fullmatch(values["время"]) is None:
        raise ValueError("Введите время строго в формате ЧЧ:ММ.")
    hour, minute = (int(part) for part in values["время"].split(":"))
    return parse_schedule_date(values["дата"]), time(hour, minute), values.get("место") or None


def _parse_series_text(text: str) -> tuple[date, ClassData]:
    lines = text.splitlines()
    if not lines:
        raise ValueError("Отправьте дату действия и полный шаблон регулярного занятия.")
    label, separator, raw_date = lines[0].partition(":")
    if not separator or label.strip().casefold() != "действует с":
        raise ValueError("Первая строка должна иметь формат «Действует с: ДД.ММ.ГГГГ».")
    effective_from = parse_schedule_date(raw_date.strip())
    data = parse_class_template("\n".join(lines[1:]))
    return effective_from, data


def _parse_effective_date(text: str) -> date:
    lines = text.splitlines()
    if len(lines) != 1:
        raise ValueError("Укажите только одну строку «Действует с: ДД.ММ.ГГГГ».")
    label, separator, raw_date = lines[0].partition(":")
    if not separator or label.strip().casefold() != "действует с":
        raise ValueError("Укажите «Действует с: ДД.ММ.ГГГГ».")
    return parse_schedule_date(raw_date.strip())


def _version_on_date(
    versions: list[SeriesVersion], series_id: int, effective_from: date
) -> SeriesVersion | None:
    matching = [
        version
        for version in versions
        if version.series_id == series_id
        and version.effective_from <= effective_from
        and (version.effective_until is None or effective_from < version.effective_until)
    ]
    return max(matching, key=lambda version: version.revision, default=None)


def _transfer_conflicts(
    versions: list[SeriesVersion],
    exceptions: list[ScheduleException],
    change: TransferOccurrence,
) -> list[Occurrence]:
    week_start = date.fromordinal(change.new_date.toordinal() - change.new_date.weekday())
    try:
        occurrences = expand_schedule(versions, exceptions, week_start)
    except ValueError:
        return []
    return [
        occurrence
        for occurrence in occurrences
        if occurrence.actual_date == change.new_date
        and occurrence.start_time == change.new_time
        and (occurrence.series_id, occurrence.original_date)
        != (change.series_id, change.original_date)
    ]


async def _schedule_draft_preview(message: Message, draft: ChangeDraft, store: Store) -> None:
    change = draft.change
    if isinstance(change, OccurrenceChange):
        change_name = (
            "Отменить проведение"
            if isinstance(change, CancelOccurrence)
            else (
                "Убрать исключение"
                if isinstance(change, RemoveException)
                else "Перенести проведение"
            )
        )
        text = (
            f"Предпросмотр · {change_name}\n"
            f"Серия №{change.series_id} · исходная дата {_date_label(change.original_date)}\n"
            f"Базовая ревизия серии: {draft.base_revision}. "
            f"Версия подтверждения: {draft.version}."
        )
        if isinstance(change, TransferOccurrence):
            versions, exceptions = await store.get_schedule()
            text += (
                f"\nНовое проведение: {_date_label(change.new_date)} "
                f"в {_time_label(change.new_time)}"
                f"\nМесто: {change.place or 'по правилу серии'}"
            )
            conflicts = _transfer_conflicts(versions, exceptions, change)
            if conflicts:
                text += "\nПредупреждение: в это время уже есть проведение:"
                text += "".join(
                    f"\n• {_date_label(item.actual_date)}, серия №{item.series_id}, "
                    f"{_time_label(item.start_time)} — {item.title}"
                    for item in conflicts
                )
        confirm = await _button(
            store,
            "Подтвердить",
            "schedule_confirm",
            schedule_draft_id=draft.id,
            schedule_draft_version=draft.version,
        )
        cancel = await _button(
            store,
            "Не применять",
            "schedule_cancel_draft",
            schedule_draft_id=draft.id,
            schedule_draft_version=draft.version,
        )
        await _send_bound(
            message,
            text,
            store,
            keyboard=InlineKeyboardMarkup(inline_keyboard=[[confirm, cancel]]),
        )
        return

    if isinstance(change, ChangeSeries):
        heading = (
            f"Предпросмотр · новое расписание с {_date_label(change.effective_from)}\n"
            f"{format_card(change.data)}\n"
        )
        if change.remove_poster:
            heading += "Афиша будет снята с новой версии.\n"
        elif change.poster_source_id is not None:
            heading += "Афиша новой версии будет заменена.\n"
        else:
            heading += "Текущая афиша сохранится.\n"
    elif isinstance(change, EndSeries):
        heading = (
            f"Предпросмотр · серия действует по {_date_label(change.last_date)} включительно\n"
        )
    else:
        raise ValueError("Неизвестный тип изменения серии.")
    heading += f"Ревизия серии: {draft.base_revision} · версия предпросмотра: {draft.version}."
    if draft.superseded_versions:
        heading += "\n\nБудут замещены будущие версии:"
        for version in draft.superseded_versions:
            heading += f"\n• {version.revision} с {_date_label(version.effective_from)}"
    await _send_bound(message, heading, store)

    for exception in draft.affected_exceptions:
        choice = change.resolutions.get(exception.original_date)
        choice_label = {
            "remove": "снять исключение",
            "detach": "отделить проведение",
            None: "не выбрано",
        }[choice]
        status = "отменено" if exception.cancelled else "перенесено"
        target = (
            f" → {_date_label(exception.new_date)} {exception.new_time.strftime('%H:%M')}"
            if exception.new_date is not None and exception.new_time is not None
            else ""
        )
        text = (
            f"Затронуто исключение · {status}\n"
            f"Исходная дата: {_date_label(exception.original_date)}{target}\n"
            f"Решение: {choice_label}"
        )
        resolution_buttons: list[InlineKeyboardButton] = []
        for resolution, label in (
            ("remove", "Снять исключение"),
            ("detach", "Отделить проведение"),
        ):
            if resolution == "detach" and exception.cancelled:
                continue
            resolution_buttons.append(
                await _button(
                    store,
                    label,
                    "schedule_resolve_remove"
                    if resolution == "remove"
                    else "schedule_resolve_detach",
                    schedule_draft_id=draft.id,
                    schedule_draft_version=draft.version,
                    original_date=exception.original_date,
                )
            )
        await _send_bound(
            message,
            text,
            store,
            keyboard=InlineKeyboardMarkup(inline_keyboard=[resolution_buttons]),
        )

    cancel = await _button(
        store,
        "Не применять",
        "schedule_cancel_draft",
        schedule_draft_id=draft.id,
        schedule_draft_version=draft.version,
    )
    decisions_complete = all(
        exception.original_date in change.resolutions for exception in draft.affected_exceptions
    )
    buttons = [cancel]
    if decisions_complete:
        buttons.insert(
            0,
            await _button(
                store,
                "Подтвердить изменение серии",
                "schedule_confirm",
                schedule_draft_id=draft.id,
                schedule_draft_version=draft.version,
            ),
        )
    prompt = (
        "Решения для затронутых исключений сохранены. Проверьте предпросмотр и подтвердите."
        if decisions_complete
        else "Выберите решение для каждого затронутого исключения перед подтверждением."
    )
    await _send_bound(
        message,
        prompt,
        store,
        keyboard=InlineKeyboardMarkup(inline_keyboard=[buttons]),
    )


async def handle_schedule_reply(
    message: Message,
    source: Source,
    store: Store,
    *,
    poster_source_id: int | None = None,
) -> bool:
    """Обработать ответ на привязанное приглашение, после сохранения источника."""
    if source.reply_to_message_id is None:
        return False
    binding = await store.get_card_binding(source.chat_id, source.reply_to_message_id)
    if binding is None or binding.schedule_action is None:
        return False
    try:
        if binding.schedule_series_id is None or binding.schedule_revision is None:
            raise ValueError("Приглашение для изменения расписания повреждено.")
        now = datetime.now(_MOSCOW)
        if binding.schedule_action == "transfer_occurrence":
            if binding.schedule_original_date is None:
                raise ValueError("Приглашение для переноса не содержит исходную дату.")
            text = source.text or ""
            new_date, new_time, place = _parse_transfer_text(text)
            change: ScheduleChange = TransferOccurrence(
                action="transfer_occurrence",
                series_id=binding.schedule_series_id,
                expected_revision=binding.schedule_revision,
                expected_exception_revision=binding.expected_exception_revision,
                original_date=binding.schedule_original_date,
                new_date=new_date,
                new_time=new_time,
                place=place,
                source_id=source.id,
            )
        elif binding.schedule_action == "change_series":
            effective_from, data = _parse_series_text(source.text or "")
            change = ChangeSeries(
                action="change_series",
                series_id=binding.schedule_series_id,
                expected_revision=binding.schedule_revision,
                effective_from=effective_from,
                data=data,
                source_id=source.id,
                poster_source_id=poster_source_id,
            )
        elif binding.schedule_action == "remove_poster":
            if source.is_photo:
                raise ValueError("Для снятия афиши отправьте только дату текстом.")
            effective_from = _parse_effective_date(source.text or "")
            versions, _ = await store.get_schedule()
            version = _version_on_date(versions, binding.schedule_series_id, effective_from)
            if version is None:
                raise ValueError("На эту дату у серии нет действующего расписания.")
            data = _next_rule_data(version.data, effective_from)
            if version.effective_until is not None:
                last_date = date.fromordinal(version.effective_until.toordinal() - 1)
                if data.last_date is None or last_date < data.last_date:
                    if data.first_date > last_date:
                        raise ValueError(
                            "На эту дату у серии уже не осталось регулярных проведений."
                        )
                    data = data.model_copy(update={"last_date": last_date})
            change = ChangeSeries(
                action="change_series",
                series_id=binding.schedule_series_id,
                expected_revision=binding.schedule_revision,
                effective_from=effective_from,
                data=data,
                source_id=source.id,
                remove_poster=True,
            )
        elif binding.schedule_action == "end_series":
            if source.text is None:
                raise ValueError("Отправьте последнюю дату в формате ДД.ММ.ГГГГ.")
            last_date = parse_schedule_date(source.text.strip())
            change = EndSeries(
                action="end_series",
                series_id=binding.schedule_series_id,
                expected_revision=binding.schedule_revision,
                last_date=last_date,
                source_id=source.id,
            )
        else:
            raise ValueError("Неизвестное приглашение расписания.")
        draft = await store.propose_schedule_change(change, now)
        if draft.status != "pending":
            await store.mark_source_processed(source.id)
            await message.answer(
                "Это изменение расписания уже обработано или заменено новой версией."
            )
            return True
        await _schedule_draft_preview(message, draft, store)
        await store.mark_source_processed(source.id)
    except (ValueError, TemplateError) as error:
        await store.set_source_error(source.id, str(error))
        await message.answer(str(error))
    return True


def _next_rule_data(data: ClassData, effective_from: date) -> ClassData:
    first_candidate = max(data.first_date, effective_from)
    offset = (data.first_date.weekday() - first_candidate.weekday()) % 7
    ordinal = first_candidate.toordinal() + offset
    if ordinal > date.max.toordinal():
        raise ValueError("Для новой версии нет допустимой календарной даты.")
    first_date = date.fromordinal(ordinal)
    if data.last_date is not None and first_date > data.last_date:
        raise ValueError("На эту дату у серии уже не осталось регулярных проведений.")
    return ClassData(**{**data.model_dump(), "first_date": first_date})


async def handle_schedule_action(message: Message, action: CardAction, store: Store) -> str:
    """Обработать сохранённый callback расписания."""
    now = datetime.now(_MOSCOW)
    if action.action in {"schedule_cancel", "schedule_transfer", "schedule_remove_exception"}:
        if (
            action.series_id is None
            or action.schedule_revision is None
            or action.original_date is None
        ):
            raise ValueError("Кнопка проведения повреждена.")
        if action.action == "schedule_transfer":
            prompt = await message.answer(
                "Ответьте на сообщение и укажите новую дату и время. Формат:\n"
                "Дата: ДД.ММ.ГГГГ\nВремя: ЧЧ:ММ\nМесто: необязательно"
            )
            await store.bind_card_message(
                CardBinding(
                    chat_id=prompt.chat.id,
                    bot_message_id=prompt.message_id,
                    schedule_action="transfer_occurrence",
                    schedule_series_id=action.series_id,
                    schedule_revision=action.schedule_revision,
                    schedule_original_date=action.original_date,
                    expected_exception_revision=action.expected_exception_revision,
                )
            )
            return "Ожидаю дату и время переноса."
        if action.action == "schedule_cancel":
            change: ScheduleChange = CancelOccurrence(
                action="cancel_occurrence",
                series_id=action.series_id,
                expected_revision=action.schedule_revision,
                expected_exception_revision=action.expected_exception_revision,
                original_date=action.original_date,
            )
        else:
            change = RemoveException(
                action="remove_exception",
                series_id=action.series_id,
                expected_revision=action.schedule_revision,
                expected_exception_revision=action.expected_exception_revision,
                original_date=action.original_date,
            )
        draft = await store.propose_schedule_change(change, now)
        await _schedule_draft_preview(message, draft, store)
        return "Предпросмотр изменения готов."

    if action.action in {"schedule_change_series", "schedule_end_series"}:
        if action.series_id is None or action.schedule_revision is None:
            raise ValueError("Кнопка серии повреждена.")
        if action.action == "schedule_change_series" and action.remove_poster:
            prompt_text = (
                "Ответьте датой начала действия: «Действует с: ДД.ММ.ГГГГ». "
                "Снятие афиши начнётся с указанной даты."
            )
            prompt = await message.answer(prompt_text)
            await store.bind_card_message(
                CardBinding(
                    chat_id=prompt.chat.id,
                    bot_message_id=prompt.message_id,
                    schedule_action="remove_poster",
                    schedule_series_id=action.series_id,
                    schedule_revision=action.schedule_revision,
                )
            )
            return "Ожидаю дату снятия афиши."

        prompt_text = (
            "Ответьте на сообщение полным шаблоном новой серии. Первая строка — "
            "«Действует с: ДД.ММ.ГГГГ», далее поля Название, Первая дата и Время. "
            "Необязательные поля можно пропустить; текущая афиша сохранится."
            if action.action == "schedule_change_series"
            else "Ответьте на сообщение последней датой серии в формате ДД.ММ.ГГГГ."
        )
        prompt = await message.answer(prompt_text)
        await store.bind_card_message(
            CardBinding(
                chat_id=prompt.chat.id,
                bot_message_id=prompt.message_id,
                schedule_action=(
                    "change_series" if action.action == "schedule_change_series" else "end_series"
                ),
                schedule_series_id=action.series_id,
                schedule_revision=action.schedule_revision,
            )
        )
        return "Ожидаю изменение серии."

    if action.action in {"schedule_confirm", "schedule_cancel_draft"}:
        if action.schedule_draft_id is None or action.schedule_draft_version is None:
            raise ValueError("Кнопка предпросмотра повреждена.")
        if action.action == "schedule_confirm":
            await store.confirm_schedule_change(
                action.schedule_draft_id, action.schedule_draft_version, now
            )
            await message.answer("Расписание изменено.")
            return "Изменение расписания подтверждено."
        await store.discard_schedule_change(action.schedule_draft_id, action.schedule_draft_version)
        await message.answer("Предпросмотр удалён; серия и исключения не изменились.")
        return "Изменение не применено."

    if action.action in {"schedule_resolve_remove", "schedule_resolve_detach"}:
        if (
            action.schedule_draft_id is None
            or action.schedule_draft_version is None
            or action.original_date is None
        ):
            raise ValueError("Кнопка решения исключения повреждена.")
        draft = await store.resolve_schedule_change(
            action.schedule_draft_id,
            action.schedule_draft_version,
            original_date=action.original_date,
            resolution="remove" if action.action == "schedule_resolve_remove" else "detach",
        )
        await _schedule_draft_preview(message, draft, store)
        return "Решение сохранено; версия предпросмотра обновлена."

    raise ValueError("Неизвестное действие расписания.")
