"""Обработчики предпросмотра, подтверждения и восстановления публикаций."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .access import is_owner_private
from .config import Settings
from .models import (
    PublicationState,
    TextPostPart,
    WeeklyPost,
)
from .publishing import (
    PublicationConfirmation,
    PublicationResult,
    continue_publication,
    publish_week,
    retry_publication_part,
)
from .storage import Store
from .templates import parse_strict_date
from .weekly import WeeklyRangeError, _split_text, build_weekly_post, week_bounds

_MOSCOW = ZoneInfo("Europe/Moscow")


def _selected_week(message: Message) -> date:
    text = message.text or ""
    command_parts = text.split(maxsplit=1)
    argument = command_parts[1].strip() if len(command_parts) == 2 else ""
    if argument:
        selected_date = parse_strict_date(
            argument,
            format_error="Введите дату строго в формате ДД.ММ.ГГГГ.",
            invalid_error="Такой календарной даты не существует.",
        )
    else:
        selected_date = datetime.now(_MOSCOW).date()
    return week_bounds(selected_date)[0].date()


async def _send_preview(target: Message, post: WeeklyPost, store: Store) -> bool:
    for part in post.parts:
        if isinstance(part, TextPostPart):
            await target.answer(escape(part.text), parse_mode=ParseMode.HTML)
            continue

        poster_path = store.path.parent / part.poster_path
        try:
            photo_bytes = await asyncio.to_thread(poster_path.read_bytes)
        except OSError:
            await target.answer(
                "Не удалось прочитать афишу для предпросмотра. Публикация не подготовлена."
            )
            return False
        await target.answer_photo(
            BufferedInputFile(photo_bytes, filename=poster_path.name),
            caption=escape(part.caption),
            parse_mode=ParseMode.HTML,
        )
    return True


def _publication_text(publication: PublicationState) -> str:
    statuses = {
        "prepared": "подготовлена",
        "sending": "отправляется",
        "sent": "отправлена",
        "failed": "не отправлена",
        "unknown": "доставка неизвестна",
    }
    publication_statuses = {
        "prepared": "подготовлен",
        "sending": "отправляется",
        "partial": "отправлен не полностью",
        "unknown": "доставка неизвестна",
        "delivered": "доставлен",
    }
    title = (
        "Повтор актуального выпуска" if publication.source_publication_id is not None else "Выпуск"
    )
    lines = [
        f"{title} недели с {publication.week_start:%d.%m.%Y}: "
        f"{publication_statuses[publication.status]}.",
        f"Группа: {publication.group_id}.",
        "Части: "
        + ", ".join(
            f"{part.index + 1} — {statuses[part.status]}"
            + (f" (сообщение {part.message_id})" if part.message_id is not None else "")
            for part in publication.parts
        ),
    ]
    if publication.status == "prepared":
        if publication.source_publication_id is not None:
            lines[0] = (
                f"Актуальный предпросмотр недели с {publication.week_start:%d.%m.%Y}. "
                "Подтвердить повторную отправку?"
            )
            lines.append(
                "Если каталог изменится до отправки, предпросмотр обновится и потребуется "
                "новое подтверждение."
            )
        else:
            lines[0] = f"Предпросмотр недели с {publication.week_start:%d.%m.%Y}. Опубликовать?"
    elif publication.status == "unknown":
        lines.append(
            "Проверьте группу перед выбором: возможна доставка части. "
            "Повтор может создать дубль. Будет повторена только выбранная часть."
        )
    elif publication.status == "delivered":
        lines.append("Для нового повтора текущей недели используйте /republish.")
    return "\n".join(lines)


def _publication_keyboard(
    publication: PublicationState, preview_revision: int
) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if publication.status == "prepared":
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        "Повторить актуальный выпуск"
                        if publication.source_publication_id is not None
                        else "Опубликовать"
                    ),
                    callback_data=(
                        f"pub:republish:{publication.id}:{preview_revision}"
                        if publication.source_publication_id is not None
                        else f"pub:confirm:{publication.id}:{preview_revision}"
                    ),
                )
            ]
        )
    elif publication.status == "unknown":
        for part in publication.parts:
            if part.status != "unknown":
                continue
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Часть {part.index + 1}: доставлено",
                        callback_data=(
                            f"pub:resolve:{publication.id}:{part.index}:1:"
                            f"{preview_revision}:{part.attempt_revision}"
                        ),
                    ),
                    InlineKeyboardButton(
                        text=f"Часть {part.index + 1}: не доставлено, повторить",
                        callback_data=(
                            f"pub:resolve:{publication.id}:{part.index}:0:"
                            f"{preview_revision}:{part.attempt_revision}"
                        ),
                    ),
                ]
            )
    elif publication.status == "partial":
        for part in publication.parts:
            if part.status == "failed":
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"Повторить только часть {part.index + 1}",
                            callback_data=(
                                f"pub:retry:{publication.id}:{part.index}:"
                                f"{preview_revision}:{part.attempt_revision}"
                            ),
                        )
                    ]
                )
        if any(part.status == "prepared" for part in publication.parts) and all(
            part.status not in ("failed", "unknown") for part in publication.parts
        ):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Продолжить оставшиеся части",
                        callback_data=f"pub:continue:{publication.id}:{preview_revision}",
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _send_bound_status(
    target: Message,
    store: Store,
    publication: PublicationState,
) -> PublicationState | None:
    next_revision = publication.preview_revision + 1
    chunks = _split_text(_publication_text(publication), 4096)
    for chunk in chunks[:-1]:
        await target.answer(chunk)
    status_message = await target.answer(
        chunks[-1],
        reply_markup=_publication_keyboard(publication, next_revision),
    )
    return await store.bind_publication_message(
        publication.id,
        publication.preview_revision,
        status_message.chat.id,
        status_message.message_id,
    )


async def _prepare_preview(
    target: Message,
    store: Store,
    group_id: int,
    week_start: date,
) -> None:
    for attempt in range(3):
        post = await build_weekly_post(week_start, store)
        if not post.records and not post.occurrences:
            await target.answer("На эту неделю записей нет.")
            return
        if not await _send_preview(target, post, store):
            return

        result = await store.prepare_publication(
            post,
            group_id,
            target.chat.id,
            target.message_id,
        )
        if result.outcome == "ready" and result.publication is not None:
            await _send_bound_status(target, store, result.publication)
            return
        if result.outcome == "existing" and result.publication is not None:
            await _send_bound_status(target, store, result.publication)
            return
        if attempt == 2:
            await target.answer("Данные меняются во время предпросмотра. Повторите /publish позже.")


async def publish_command_handler(
    message: Message,
    store: Store,
    settings: Settings,
) -> None:
    """Показать снимок недели и запросить отдельное подтверждение отправки."""
    if settings.telegram_group_id is None:
        await message.answer("Для публикации настройте TELEGRAM_GROUP_ID группы или супергруппы.")
        return
    try:
        week_start = _selected_week(message)
    except (ValueError, WeeklyRangeError) as error:
        await message.answer(str(error))
        return

    existing = await store.get_publication_for_week(settings.telegram_group_id, week_start)
    if existing is not None and existing.status != "prepared":
        await _send_bound_status(message, store, existing)
        return
    await _prepare_preview(message, store, settings.telegram_group_id, week_start)


async def _prepare_republication_preview(
    target: Message,
    store: Store,
    original: PublicationState,
    post: WeeklyPost,
) -> None:
    for _attempt in range(3):
        result = await store.prepare_republication(
            post,
            original.group_id,
            original.id,
            target.chat.id,
            target.message_id,
        )
        if result.outcome == "stale":
            post = await build_weekly_post(post.week_start, store)
            if not post.records and not post.occurrences:
                await target.answer("На эту неделю записей нет. Повтор не подготовлен.")
                return
            continue
        if result.outcome == "missing":
            await target.answer(
                "Нет полностью доставленного выпуска за эту неделю. Сначала выполните /publish."
            )
            return
        publication = result.publication
        if publication is None:
            await target.answer("Не удалось подготовить повтор. Повторите /republish позже.")
            return
        if publication.status == "prepared" and not await _send_preview(
            target, publication.snapshot, store
        ):
            return
        await _send_bound_status(target, store, publication)
        return
    await target.answer("Данные меняются во время предпросмотра. Повторите /republish позже.")


async def republish_command_handler(
    message: Message,
    store: Store,
    settings: Settings,
) -> None:
    """Подготовить актуальный снимок недели для повторной отправки."""
    command_parts = (message.text or "").split(maxsplit=1)
    if len(command_parts) == 2 and command_parts[1].strip():
        await message.answer("Команда /republish не принимает дату.")
        return

    bound = await store.get_republication_for_request(message.chat.id, message.message_id)
    current_week = week_bounds(datetime.now(_MOSCOW).date())[0].date()
    if bound is not None:
        if (
            bound.status != "prepared"
            or bound.week_start != current_week
            or settings.telegram_group_id != bound.group_id
            or bound.source_publication_id is None
        ):
            await _send_bound_status(message, store, bound)
            return
        post = await build_weekly_post(bound.week_start, store)
        if not post.records and not post.occurrences:
            await message.answer("На эту неделю записей нет. Повтор не подготовлен.")
            return
        original = await store.get_publication(bound.source_publication_id)
        if original is None:
            await message.answer("Исходный выпуск недоступен. Повторите /publish.")
            return
        await _prepare_republication_preview(message, store, original, post)
        return

    if settings.telegram_group_id is None:
        await message.answer("Для публикации настройте TELEGRAM_GROUP_ID группы или супергруппы.")
        return
    original = await store.get_publication_for_week(settings.telegram_group_id, current_week)
    if original is None or original.status != "delivered":
        await message.answer(
            "Нет полностью доставленного выпуска за текущую неделю. Сначала выполните /publish."
        )
        return

    pending = await store.get_pending_republication(original.id)
    if pending is not None and pending.status != "prepared":
        attached = await store.bind_republication_request(
            pending.id, message.chat.id, message.message_id
        )
        if attached is not None:
            await _send_bound_status(message, store, attached)
        return

    post = await build_weekly_post(current_week, store)
    if not post.records and not post.occurrences:
        if pending is not None:
            await store.bind_republication_request(pending.id, message.chat.id, message.message_id)
        await message.answer("На эту неделю записей нет. Повтор не подготовлен.")
        return
    await _prepare_republication_preview(message, store, original, post)


def _parse_callback(data: str) -> tuple[str, tuple[int, ...]] | None:
    if len(data.encode("utf-8")) > 64:
        return None
    pieces = data.split(":")
    if not pieces or pieces[0] != "pub":
        return None
    action = pieces[1] if len(pieces) > 1 else ""
    try:
        values = tuple(int(value) for value in pieces[2:])
    except ValueError:
        return None
    if not values or values[0] == 0 or any(value < 0 or value > 2**63 - 1 for value in values):
        return None
    return action, values


async def _answer_stale(callback: CallbackQuery) -> None:
    await callback.answer("Кнопка устарела или относится к другому сообщению.", show_alert=True)


async def _refresh_callback_status(
    callback: CallbackQuery,
    store: Store,
    publication: PublicationState | None,
) -> None:
    message = callback.message
    if not isinstance(message, Message):
        return
    if publication is None:
        await message.answer("Состояние выпуска недоступно.")
        return
    await _send_bound_status(message, store, publication)


async def _refresh_republication_callback(
    target: Message,
    store: Store,
    publication: PublicationState,
) -> None:
    for _ in range(3):
        post = await build_weekly_post(publication.week_start, store)
        if not post.records and not post.occurrences:
            await target.answer("На эту неделю записей нет. Повторная отправка не началась.")
            return
        result = await store.refresh_republication(
            post, publication.id, publication.preview_revision
        )
        if result.outcome == "stale":
            continue
        updated = result.publication
        if updated is None:
            await target.answer("Не удалось обновить повторный выпуск. Откройте /republish.")
            return
        if updated.status == "prepared" and not await _send_preview(
            target, updated.snapshot, store
        ):
            return
        await _send_bound_status(target, store, updated)
        return
    await target.answer("Данные меняются. Повторите /republish и подтвердите новый предпросмотр.")


async def publication_callback_handler(
    callback: CallbackQuery,
    store: Store,
    settings: Settings,
    bot: Bot,
) -> None:
    """Проверить владельца, выпуск, ревизию попытки и исходное control-сообщение."""
    message = callback.message
    if not isinstance(message, Message) or not is_owner_private(
        callback.from_user.id, message.chat.type, settings.telegram_owner_id
    ):
        await _answer_stale(callback)
        return
    parsed = _parse_callback(callback.data or "")
    if parsed is None or settings.telegram_group_id is None:
        await _answer_stale(callback)
        return
    action, values = parsed
    if action in ("confirm", "republish") and len(values) == 2:
        publication_id, preview_revision = values
        publication = await store.get_publication(publication_id)
    elif action in ("resolve", "retry") and len(values) == (5 if action == "resolve" else 4):
        publication_id = values[0]
        preview_revision = values[-2]
        publication = await store.get_publication(publication_id)
    elif action == "continue" and len(values) == 2:
        publication_id, preview_revision = values
        publication = await store.get_publication(publication_id)
    else:
        await _answer_stale(callback)
        return

    if (
        publication is None
        or publication.group_id != settings.telegram_group_id
        or publication.preview_revision != preview_revision
        or publication.owner_chat_id != message.chat.id
        or publication.owner_message_id != message.message_id
    ):
        await _answer_stale(callback)
        return
    if action in ("confirm", "republish") and (
        (action == "republish") != (publication.source_publication_id is not None)
    ):
        await _answer_stale(callback)
        return

    await callback.answer()
    owner_chat_id = message.chat.id
    owner_message_id = message.message_id
    result: PublicationResult | None = None
    try:
        if action in ("confirm", "republish"):
            if publication.status != "prepared":
                await _answer_stale(callback)
                return
            result = await publish_week(
                publication.snapshot,
                publication.group_id,
                store,
                bot,
                confirmation=PublicationConfirmation(
                    publication_id=publication.id,
                    preview_revision=preview_revision,
                    owner_chat_id=owner_chat_id,
                    owner_message_id=owner_message_id,
                ),
            )
        elif action == "resolve" and len(values) == 5:
            part_index, delivered_value, preview_revision, attempt_revision = values[1:]
            if delivered_value not in (0, 1):
                await _answer_stale(callback)
                return
            await store.resolve_publication_part(
                publication_id,
                part_index,
                delivered_value == 1,
                expected_attempt_revision=attempt_revision,
                expected_preview_revision=preview_revision,
                owner_chat_id=owner_chat_id,
                owner_message_id=owner_message_id,
            )
            if delivered_value == 0:
                result = await retry_publication_part(
                    publication_id,
                    part_index,
                    publication.group_id,
                    store,
                    bot,
                    expected_attempt_revision=attempt_revision,
                    expected_preview_revision=preview_revision,
                    owner_chat_id=owner_chat_id,
                    owner_message_id=owner_message_id,
                )
        elif action == "retry" and len(values) == 4:
            part_index, preview_revision, attempt_revision = values[1:]
            result = await retry_publication_part(
                publication_id,
                part_index,
                publication.group_id,
                store,
                bot,
                expected_attempt_revision=attempt_revision,
                expected_preview_revision=preview_revision,
                owner_chat_id=owner_chat_id,
                owner_message_id=owner_message_id,
            )
        elif action == "continue":
            result = await continue_publication(
                publication_id,
                publication.group_id,
                store,
                bot,
                expected_preview_revision=preview_revision,
                owner_chat_id=owner_chat_id,
                owner_message_id=owner_message_id,
            )
    except ValueError:
        await _answer_stale(callback)
        return

    updated = await store.get_publication(publication_id)
    if result is not None and result.status == "stale":
        if publication.source_publication_id is not None:
            await _refresh_republication_callback(message, store, publication)
            return
        await message.answer("Данные изменились после предпросмотра. Проверьте новый выпуск.")
        await _prepare_preview(message, store, publication.group_id, publication.week_start)
        return
    if result is not None and result.status == "rejected":
        command = "/republish" if publication.source_publication_id is not None else "/publish"
        await message.answer(
            "Отправка не началась. Проверьте TELEGRAM_GROUP_ID, доступ бота к группе "
            f"или супергруппе и откройте актуальные кнопки через {command}."
        )
    await _refresh_callback_status(callback, store, updated)
