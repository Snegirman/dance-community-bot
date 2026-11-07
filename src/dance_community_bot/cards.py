"""Карточки и привязки сообщений к сохранённым версиям."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .models import (
    CardActionName,
    CardBinding,
    CardData,
    ClassData,
    Draft,
    EventData,
    Record,
    Source,
)
from .storage import Store

_MOSCOW = ZoneInfo("Europe/Moscow")
_MESSAGE_LIMIT = 3500
_EVENTS_PAGE_SIZE = 5


def _date_label(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _time_label(value: time | None) -> str:
    return value.strftime("%H:%M") if value is not None else "не указано"


def _week_label(start: date, end: date | None) -> str:
    last_date = end or start
    first_week = start.isocalendar()
    last_week = last_date.isocalendar()
    first_label = f"{first_week.year}-W{first_week.week:02d}"
    last_label = f"{last_week.year}-W{last_week.week:02d}"
    if first_label == last_label:
        return f"Неделя ISO {first_label}"
    return f"Недели ISO {first_label} — {last_label}"


def format_card(data: CardData) -> str:
    """Показать все поля шаблона без обрезки и подстановки времени."""
    if isinstance(data, EventData):
        end_date = f" — {_date_label(data.end_date)}" if data.end_date is not None else ""
        lines = [
            f"Название: {data.title}",
            f"Дата: {_date_label(data.start_date)}{end_date}",
            f"Тип: {data.event_type}",
            f"Время: {_time_label(data.start_time)}",
            f"Место: {data.place or 'не указано'}",
            f"Стоимость: {data.price or 'не указана'}",
            f"Описание: {data.description or 'не указано'}",
            _week_label(data.start_date, data.end_date),
        ]
        return "\n".join(lines)

    if isinstance(data, ClassData):
        end_date = f" — {_date_label(data.last_date)}" if data.last_date is not None else ""
        lines = [
            f"Название: {data.title}",
            f"Первая дата: {_date_label(data.first_date)}{end_date}",
            "Тип: Занятие",
            f"Время: {_time_label(data.start_time)}",
            f"Место: {data.place or 'не указано'}",
            f"Стоимость: {data.price or 'не указана'}",
            f"Описание: {data.description or 'не указано'}",
            _week_label(data.first_date, data.last_date),
        ]
        return "\n".join(lines)

    raise ValueError("Неизвестный вид карточки.")


def _card_warnings(
    data: CardData,
    records: list[Record],
    *,
    excluded_record_id: int | None = None,
) -> list[str]:
    now = datetime.now(_MOSCOW)
    start_date = data.start_date if isinstance(data, EventData) else data.first_date
    start_time = data.start_time
    if start_date < now.date() or (
        start_date == now.date()
        and start_time is not None
        and start_time < now.time().replace(tzinfo=None)
    ):
        warnings = ["Предупреждение: дата или время начала уже прошли."]
    else:
        warnings = []

    for record in _matching_records(data, records, excluded_record_id=excluded_record_id):
        warnings.append(f"Предупреждение: похоже на событие в карточке №{record.id}.")
    return warnings


def _matching_records(
    data: CardData,
    records: list[Record],
    *,
    excluded_record_id: int | None,
) -> list[Record]:
    start_date = data.start_date if isinstance(data, EventData) else data.first_date
    title = data.title.casefold()
    matching: list[Record] = []
    for record in records:
        if (
            record.status != "active"
            or record.id == excluded_record_id
            or record.data.title.casefold() != title
        ):
            continue
        other_start_date = (
            record.data.start_date if isinstance(record.data, EventData) else record.data.first_date
        )
        if other_start_date == start_date and record.data.start_time == data.start_time:
            matching.append(record)
    return matching


def _chunks(text: str) -> list[str]:
    chunks: list[str] = []
    remainder = text
    while len(remainder) > _MESSAGE_LIMIT:
        boundary = remainder.rfind("\n", 0, _MESSAGE_LIMIT)
        if boundary <= 0:
            boundary = _MESSAGE_LIMIT
            chunks.append(remainder[:boundary])
            remainder = remainder[boundary:]
        else:
            chunks.append(remainder[: boundary + 1])
            remainder = remainder[boundary + 1 :]
    if remainder:
        chunks.append(remainder)
    return chunks


async def _send_bound_text(
    message: Message,
    text: str,
    store: Store,
    binding: CardBinding,
    keyboard: InlineKeyboardMarkup | None = None,
) -> list[Message]:
    sent_messages: list[Message] = []
    for index, chunk in enumerate(_chunks(text)):
        sent = await message.answer(chunk, reply_markup=keyboard if index == 0 else None)
        await store.bind_card_message(
            binding.model_copy(update={"chat_id": sent.chat.id, "bot_message_id": sent.message_id})
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
        sent_messages.append(sent)
    return sent_messages


async def _action_button(
    store: Store,
    label: str,
    action_name: CardActionName,
    *,
    record_id: int | None = None,
    draft_id: int | None = None,
    version: int | None = None,
    revision: int | None = None,
    source_id: int | None = None,
    page: int | None = None,
) -> InlineKeyboardButton:
    action = await store.create_card_action(
        action_name,
        record_id=record_id,
        draft_id=draft_id,
        version=version,
        revision=revision,
        source_id=source_id,
        page=page,
    )
    return InlineKeyboardButton(text=label, callback_data=f"a:{action.token}")


async def show_draft(message: Message, draft: Draft, store: Store) -> None:
    """Показать черновик с действиями, чьи намерения переживают перезапуск."""
    rows = [
        [
            await _action_button(
                store,
                "Сохранить",
                "confirm",
                record_id=draft.record_id,
                draft_id=draft.id,
                version=draft.version,
                revision=draft.base_revision,
            ),
            await _action_button(
                store,
                "Исправить",
                "edit",
                record_id=draft.record_id,
                draft_id=draft.id,
                version=draft.version,
                revision=draft.base_revision,
            ),
            await _action_button(
                store,
                "Отклонить",
                "reject",
                record_id=draft.record_id,
                draft_id=draft.id,
                version=draft.version,
            ),
        ]
    ]
    if draft.poster_source_id is not None:
        source = await store.get_source_by_id(draft.poster_source_id)
        if source is not None:
            if source.attachment_status in {"pending", "failed"}:
                rows.append(
                    [
                        await _action_button(
                            store,
                            "Повторить загрузку афиши",
                            "retry_poster",
                            record_id=draft.record_id,
                            draft_id=draft.id,
                            version=draft.version,
                            source_id=source.id,
                        ),
                    ]
                )
            rows.append(
                [
                    await _action_button(
                        store,
                        "Убрать афишу",
                        "remove_poster",
                        record_id=draft.record_id,
                        draft_id=draft.id,
                        version=draft.version,
                        revision=draft.base_revision,
                        source_id=source.id,
                    ),
                ]
            )

    lines = [f"Черновик №{draft.record_id} · версия {draft.version}", format_card(draft.data)]
    if draft.poster_source_id is None:
        lines.append("Афиша: не добавлена")
    else:
        source = await store.get_source_by_id(draft.poster_source_id)
        if source is None or source.attachment_status != "saved":
            lines.append("Афиша: загрузка не завершена")
            if source is not None and source.error_reason:
                lines.append(f"Ошибка вложения: {source.error_reason}")
        else:
            lines.append("Афиша: добавлена")

    records = await store.list_records()
    lines.extend(_card_warnings(draft.data, records, excluded_record_id=draft.record_id))
    duplicate_buttons = [
        await _action_button(
            store,
            f"Открыть №{record.id}",
            "view",
            record_id=record.id,
            revision=record.revision,
        )
        for record in _matching_records(draft.data, records, excluded_record_id=draft.record_id)
    ]
    if duplicate_buttons:
        rows.append(duplicate_buttons)
    text = "\n\n".join(lines)
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    binding = CardBinding(
        chat_id=message.chat.id,
        bot_message_id=0,
        record_id=draft.record_id,
        draft_id=draft.id,
        version=draft.version,
        revision=draft.base_revision,
        source_id=draft.source_id,
    )
    await _send_bound_text(message, text, store, binding, keyboard)


async def show_record(message: Message, record: Record, store: Store) -> None:
    row = [
        await _action_button(
            store,
            "Исправить",
            "edit",
            record_id=record.id,
            revision=record.revision,
        ),
        await _action_button(
            store,
            "Отменить",
            "cancel",
            record_id=record.id,
            revision=record.revision,
        ),
    ]
    if record.poster_source_id is not None:
        row.append(
            await _action_button(
                store,
                "Убрать афишу",
                "remove_poster",
                record_id=record.id,
                revision=record.revision,
                source_id=record.poster_source_id,
            )
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[row])
    poster = "Афиша: добавлена" if record.poster_source_id is not None else "Афиша: не добавлена"
    binding = CardBinding(
        chat_id=message.chat.id,
        bot_message_id=0,
        record_id=record.id,
        revision=record.revision,
    )
    await _send_bound_text(
        message,
        f"Запись №{record.id} · ревизия {record.revision}\n{format_card(record.data)}\n{poster}",
        store,
        binding,
        keyboard,
    )


async def show_source(message: Message, source: Source, store: Store) -> None:
    """Показать сохранённый, но ещё не обработанный источник и доступные действия."""
    rows: list[list[InlineKeyboardButton]] = []
    if source.is_photo and source.attachment_status in {"pending", "failed"}:
        rows.append(
            [
                await _action_button(
                    store,
                    "Повторить загрузку афиши",
                    "retry_poster",
                    source_id=source.id,
                ),
                await _action_button(
                    store,
                    "Убрать афишу",
                    "remove_poster",
                    source_id=source.id,
                ),
            ]
        )
    text = _source_card_text(source)
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    reply_binding = (
        await store.get_card_binding(source.chat_id, source.reply_to_message_id)
        if source.reply_to_message_id is not None
        else None
    )
    if reply_binding is not None and reply_binding.record_id is not None:
        binding = reply_binding.model_copy(update={"bot_message_id": 0, "source_id": source.id})
    else:
        binding = CardBinding(
            chat_id=message.chat.id,
            bot_message_id=0,
            source_id=source.id,
        )
    await _send_bound_text(message, text, store, binding, keyboard)


async def show_events(message: Message, store: Store, *, page: int = 0) -> None:
    """Показать активные записи, черновики и незавершённые источники постранично."""
    records = await store.list_records()
    drafts = await store.list_drafts()
    sources = await store.list_sources()

    entries: list[Record | Draft | Source] = []
    for record in records:
        if record.status != "active":
            continue
        entries.append(record)

    for draft in drafts:
        if draft.status != "pending":
            continue
        entries.append(draft)

    drafted_source_ids = {
        source_id
        for draft in drafts
        for source_id in (draft.source_id, draft.poster_source_id)
        if source_id is not None
    }
    for source in sources:
        if source.id in drafted_source_ids or not _source_needs_attention(source):
            continue
        entries.append(source)

    total_pages = max(1, (len(entries) + _EVENTS_PAGE_SIZE - 1) // _EVENTS_PAGE_SIZE)
    safe_page = min(max(page, 0), total_pages - 1)
    start = safe_page * _EVENTS_PAGE_SIZE
    visible = entries[start : start + _EVENTS_PAGE_SIZE]
    lines = [f"События и черновики · Страница {safe_page + 1} из {total_pages}"]
    rows: list[list[InlineKeyboardButton]] = []
    for item in visible:
        lines.append(await _event_entry_text(item, store))
        buttons = await _event_entry_buttons(item, store)
        if buttons:
            rows.append(buttons)
    if not visible:
        lines.append("Активных событий, черновиков и незавершённых сообщений пока нет.")

    navigation: list[InlineKeyboardButton] = []
    if safe_page > 0:
        navigation.append(await _action_button(store, "← Назад", "events_page", page=safe_page - 1))
    if safe_page + 1 < total_pages:
        navigation.append(
            await _action_button(store, "Дальше →", "events_page", page=safe_page + 1)
        )
    if navigation:
        rows.append(navigation)
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    binding = CardBinding(chat_id=message.chat.id, bot_message_id=0, page=safe_page)
    await _send_bound_text(message, "\n\n".join(lines), store, binding, keyboard)


async def _event_entry_text(item: Record | Draft | Source, store: Store) -> str:
    if isinstance(item, Record):
        poster = "добавлена" if item.poster_source_id is not None else "не добавлена"
        return (
            f"Запись №{item.id} · ревизия {item.revision}\n{format_card(item.data)}\n"
            f"Афиша: {poster}"
        )
    if isinstance(item, Draft):
        status = "новой записи" if item.base_revision is None else "исправления"
        source = (
            await store.get_source_by_id(item.poster_source_id)
            if item.poster_source_id is not None
            else None
        )
        if item.poster_source_id is None:
            poster = "не добавлена"
        elif source is None or source.attachment_status != "saved":
            poster = "загрузка не завершена"
        else:
            poster = "добавлена"
        lines = [
            f"Черновик №{item.record_id} · версия {item.version} ({status})",
            format_card(item.data),
            f"Афиша: {poster}",
        ]
        if source is not None and source.error_reason:
            lines.append(f"Ошибка вложения: {source.error_reason}")
        return "\n".join(lines)

    return _source_card_text(item)


async def _event_entry_buttons(
    item: Record | Draft | Source, store: Store
) -> list[InlineKeyboardButton]:
    if isinstance(item, Record):
        return [
            await _action_button(
                store,
                f"Открыть №{item.id}",
                "view",
                record_id=item.id,
                revision=item.revision,
            )
        ]

    if isinstance(item, Draft):
        return [
            await _action_button(
                store,
                f"Открыть №{item.record_id}",
                "view",
                record_id=item.record_id,
                draft_id=item.id,
                version=item.version,
                revision=item.base_revision,
            )
        ]

    return [
        await _action_button(
            store,
            f"Открыть сообщение {item.message_id}",
            "view",
            source_id=item.id,
        )
    ]


def _source_card_text(source: Source) -> str:
    reason = source.error_reason or "обработка не завершена"
    label = "Афиша" if source.is_photo else "Сообщение"
    state = {
        "none": "без вложения",
        "pending": "загрузка не завершена",
        "saved": "загружена",
        "failed": "ошибка загрузки",
        "removed": "вложение убрано",
    }[source.attachment_status]
    return f"{label} из сообщения {source.message_id} · {state}\nПричина: {reason}"


def _source_needs_attention(source: Source) -> bool:
    return source.status in {"pending", "failed"} or source.attachment_status in {
        "pending",
        "failed",
    }
