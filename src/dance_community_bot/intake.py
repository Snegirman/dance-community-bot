"""Приём структурированных карточек и их владельческих действий."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal
from uuid import uuid4

from aiogram.types import CallbackQuery, Message

from .cards import show_draft, show_events, show_record, show_source
from .models import (
    CardAction,
    CardBinding,
    CardData,
    ClassData,
    Draft,
    EventData,
    Source,
    SourceInput,
)
from .schedule_handlers import handle_schedule_action, handle_schedule_reply
from .storage import Store
from .templates import TemplateError, parse_class_template, parse_event_template

_POSTER_ERROR = "Не удалось сохранить афишу. Можно повторить загрузку или убрать её из черновика."


class _EditContext:
    def __init__(
        self,
        data: CardData,
        record_id: int,
        base_revision: int | None,
        poster_source_id: int | None,
        expected_draft_id: int | None = None,
    ) -> None:
        self.data = data
        self.record_id = record_id
        self.base_revision = base_revision
        self.poster_source_id = poster_source_id
        self.expected_draft_id = expected_draft_id


def _message_text(message: Message) -> str | None:
    return message.text if message.text is not None else message.caption


def _source_input(message: Message) -> SourceInput:
    photo_id = message.photo[-1].file_id if message.photo else None
    document_id = message.document.file_id if message.document is not None else None
    media_file_id = photo_id or document_id
    reply_to_message_id = (
        message.reply_to_message.message_id if message.reply_to_message is not None else None
    )
    return SourceInput(
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=_message_text(message),
        received_at=message.date,
        telegram_file_id=media_file_id,
        is_photo=bool(message.photo),
        media_group_id=message.media_group_id,
        reply_to_message_id=reply_to_message_id,
    )


def _field_markers(text: str) -> tuple[bool, bool]:
    labels = {
        line.partition(":")[0].strip().casefold()
        for line in text.splitlines()
        if ":" in line and not line.startswith((" ", "\t"))
    }
    return "дата" in labels, "первая дата" in labels


def _template_text(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip().casefold() in {"/event", "/class"}:
        return "\n".join(lines[1:])
    return text


def _parse_template(text: str, expected: Literal["event", "class"] | None) -> CardData:
    template = _template_text(text)
    has_event_date, has_first_date = _field_markers(template)
    if has_event_date and has_first_date:
        raise TemplateError(["Укажите «Дата» или «Первая дата», но не оба поля."])
    if expected == "event" and has_first_date:
        raise TemplateError(["Для этой карточки используйте поле «Дата». "])
    if expected == "class" and has_event_date:
        raise TemplateError(["Для этой карточки используйте поле «Первая дата». "])

    if expected == "event" or (expected is None and has_event_date):
        return parse_event_template(template)
    if expected == "class" or (expected is None and has_first_date):
        return parse_class_template(template)
    raise TemplateError(["Выберите тип карточки: /event или /class, затем отправьте шаблон."])


async def _resolve_edit_context(
    chat_id: int,
    reply_to_message_id: int | None,
    store: Store,
) -> tuple[_EditContext | None, bool]:
    if reply_to_message_id is None:
        return None, False
    binding = await store.get_card_binding(chat_id, reply_to_message_id)
    return await _context_from_binding(binding, store)


async def _context_from_binding(
    binding: CardBinding | None, store: Store
) -> tuple[_EditContext | None, bool]:
    if binding is None or binding.record_id is None:
        return None, False

    if binding.draft_id is not None:
        draft = await store.get_draft(binding.draft_id)
        if (
            draft is None
            or draft.version != binding.version
            or draft.record_id != binding.record_id
        ):
            return None, True
        if draft.status == "pending":
            return (
                _EditContext(
                    draft.data,
                    draft.record_id,
                    draft.base_revision,
                    draft.poster_source_id,
                    draft.id,
                ),
                False,
            )
        if draft.status != "confirmed":
            return None, True
        record = await store.get_record(draft.record_id)
        expected_revision = (draft.base_revision or 0) + 1
        if record is None or record.status != "active" or record.revision != expected_revision:
            return None, True
        return _EditContext(record.data, record.id, record.revision, record.poster_source_id), False

    record = await store.get_record(binding.record_id)
    if record is None or record.status != "active" or binding.revision != record.revision:
        return None, True
    return _EditContext(record.data, record.id, record.revision, record.poster_source_id), False


async def _download_poster(message: Message, source: Source, store: Store) -> bool:
    if source.telegram_file_id is None or not source.is_photo:
        return False
    if source.attachment_status == "saved" and source.poster_path is not None:
        if await asyncio.to_thread((store.path.parent / source.poster_path).is_file):
            return True
    if source.attachment_status == "removed":
        return False

    source = await store.retry_source(source.id)
    poster_directory = store.path.parent / "posters"
    poster_path = Path("posters") / f"{source.id}.jpg"
    final_path = store.path.parent / poster_path
    temporary_path = poster_directory / f".{source.id}.{uuid4().hex}.jpg.tmp"
    try:
        if not await asyncio.to_thread(final_path.is_file):
            telegram_file = await message.bot.get_file(source.telegram_file_id)
            if telegram_file.file_path is None:
                raise ValueError("Telegram не вернул путь к афише.")
            await message.bot.download_file(telegram_file.file_path, temporary_path)
            await asyncio.to_thread(temporary_path.replace, final_path)
        await store.set_source_poster_path(source.id, poster_path)
        return True
    except Exception:
        try:
            await asyncio.to_thread(temporary_path.unlink, missing_ok=True)
        except OSError:
            pass
        try:
            await store.set_source_error(source.id, _POSTER_ERROR)
        except Exception:
            pass
        return False


async def _save_draft(
    message: Message,
    source: Source,
    data: CardData,
    store: Store,
    context: _EditContext | None,
    poster_source_id: int | None,
) -> Draft | None:
    try:
        draft = await store.save_draft(
            source.id,
            data,
            poster_source_id=poster_source_id,
            record_id=context.record_id if context is not None else None,
            base_revision=context.base_revision if context is not None else None,
            expected_draft_id=context.expected_draft_id if context is not None else None,
        )
    except ValueError:
        await store.set_source_error(
            source.id,
            "Карточка изменилась или стала недоступна. Откройте /events; "
            "затем отправьте шаблон ещё раз.",
        )
        await message.answer(
            "Карточка изменилась или стала недоступна. Откройте /events и отправьте шаблон ещё раз."
        )
        return None
    return draft


async def _show_source_error(message: Message, source: Source, store: Store) -> None:
    await show_source(message, source, store)


async def _repeat_source(message: Message, source: Source, store: Store) -> None:
    drafts = [draft for draft in await store.list_drafts() if draft.source_id == source.id]
    if drafts:
        draft = drafts[-1]
        if (
            source.attachment_status == "failed"
            and draft.status == "pending"
            and draft.poster_source_id == source.id
        ):
            if await _download_poster(message, source, store):
                await store.mark_source_processed(source.id)
                draft = await store.get_draft(draft.id) or draft
            else:
                draft = await store.get_draft(draft.id) or draft
            await show_draft(message, draft, store)
            return
        if draft.status == "pending":
            await show_draft(message, draft, store)
            return
        record = await store.get_record(draft.record_id)
        if record is not None:
            await show_record(message, record, store)
            return
        if draft.status in {"rejected", "superseded"}:
            await message.answer(
                "Это сообщение уже обработано; откройте /events для текущего состояния."
            )
            return

    if source.status == "failed" and source.attachment_status != "failed":
        await _show_source_error(message, source, store)
        return
    if source.attachment_status == "failed" and source.telegram_file_id is not None:
        await _process_source(message, source, store, resolve_reply=True)
        return

    await _process_source(message, source, store, resolve_reply=True)


async def _process_source(
    message: Message,
    source: Source,
    store: Store,
    *,
    resolve_reply: bool,
) -> None:
    text = source.text or ""
    context: _EditContext | None = None
    inherited_poster: Source | None = None
    if resolve_reply:
        context, stale = await _resolve_edit_context(
            source.chat_id,
            source.reply_to_message_id,
            store,
        )
        if stale:
            await store.set_source_error(source.id, "Карточка устарела. Откройте /events.")
            await _show_source_error(
                message, await store.get_source_by_id(source.id) or source, store
            )
            return

        if source.reply_to_message_id is not None:
            binding = await store.get_card_binding(source.chat_id, source.reply_to_message_id)
            if binding is not None and binding.source_id is not None:
                candidate = await store.get_source_by_id(binding.source_id)
                if (
                    candidate is not None
                    and candidate.is_photo
                    and candidate.attachment_status != "removed"
                ):
                    inherited_poster = candidate

    photo_present = source.is_photo
    if source.media_group_id is not None:
        reason = "Альбомы не поддерживаются. Отправьте одну фотографию ответом на карточку."
        await store.set_source_error(source.id, reason)
        await _show_source_error(message, await store.get_source_by_id(source.id) or source, store)
        return
    if source.telegram_file_id is not None and not source.is_photo:
        reason = "Документы не поддерживаются как афиша. Отправьте одну фотографию."
        await store.set_source_error(source.id, reason)
        await _show_source_error(message, await store.get_source_by_id(source.id) or source, store)
        return

    if (
        context is not None
        and isinstance(context.data, ClassData)
        and context.base_revision is not None
        and source.reply_to_message_id is not None
    ):
        await store.bind_card_message(
            CardBinding(
                chat_id=source.chat_id,
                bot_message_id=source.reply_to_message_id,
                schedule_action="change_series",
                schedule_series_id=context.record_id,
                schedule_revision=context.base_revision,
            )
        )
        await _handle_schedule_source(message, source, store)
        return

    data: CardData | None = None
    parse_error: str | None = None
    if text.strip():
        expected: Literal["event", "class"] | None = None
        if context is not None:
            expected = "event" if isinstance(context.data, EventData) else "class"
        try:
            data = _parse_template(text, expected)
        except TemplateError as error:
            parse_error = str(error)
    elif context is not None and photo_present:
        data = context.data

    poster_saved = True
    if photo_present:
        poster_saved = await _download_poster(
            message,
            await store.get_source_by_id(source.id) or source,
            store,
        )
    elif inherited_poster is not None:
        poster_saved = await _download_poster(message, inherited_poster, store)

    if parse_error is not None:
        reason = f"{parse_error}\nИсточник сохранён; отправьте исправленный шаблон."
        await store.set_source_error(source.id, reason)
        await _show_source_error(message, await store.get_source_by_id(source.id) or source, store)
        return
    if data is None:
        reason = (
            "Фото без шаблона сохранено. Отправьте шаблон текста или добавьте фото "
            "ответом на карточку."
            if photo_present
            else "Отправьте полный шаблон /event или /class. Источник сохранён в /events."
        )
        await store.set_source_error(source.id, reason)
        await _show_source_error(message, await store.get_source_by_id(source.id) or source, store)
        return

    poster_source_id = context.poster_source_id if context is not None else None
    if inherited_poster is not None:
        poster_source_id = inherited_poster.id
    if photo_present:
        poster_source_id = source.id
    draft = await _save_draft(message, source, data, store, context, poster_source_id)
    if draft is None:
        return
    if not poster_saved:
        await store.set_source_error(source.id, _POSTER_ERROR)
    elif inherited_poster is not None:
        await store.mark_source_processed(inherited_poster.id)
    await show_draft(message, await store.get_draft(draft.id) or draft, store)


async def ingest_message(message: Message, store: Store) -> None:
    """Сохранить исходник до проверки и показать результат обработки."""
    if message.edit_date is not None:
        return

    source_input = _source_input(message)
    existing = await store.get_source(source_input.chat_id, source_input.message_id)
    if existing is not None:
        if await _handle_schedule_source(message, existing, store):
            return
        await _repeat_source(message, existing, store)
        return

    source = await store.save_source(source_input)
    if await _handle_schedule_source(message, source, store):
        return
    if message.media_group_id is None:
        await _process_source(message, source, store, resolve_reply=True)
    else:
        await _process_source(message, source, store, resolve_reply=False)


async def _handle_schedule_source(message: Message, source: Source, store: Store) -> bool:
    if source.media_group_id is not None or (
        source.telegram_file_id is not None and not source.is_photo
    ):
        return False
    if source.reply_to_message_id is None:
        return False
    binding = await store.get_card_binding(source.chat_id, source.reply_to_message_id)
    if binding is None or binding.schedule_action is None:
        return False
    poster = source if source.is_photo else None
    if poster is None and binding.schedule_action == "change_series" and binding.source_id:
        poster = await store.get_source_by_id(binding.source_id)
    poster_source_id: int | None = None
    if poster is not None and poster.is_photo and poster.attachment_status != "removed":
        if not await _download_poster(message, poster, store):
            await store.set_source_error(source.id, _POSTER_ERROR)
            await show_source(message, await store.get_source_by_id(source.id) or source, store)
            return True
        poster_source_id = poster.id
    handled = await handle_schedule_reply(message, source, store, poster_source_id=poster_source_id)
    if poster_source_id is not None and poster_source_id != source.id:
        processed = await store.get_source_by_id(source.id)
        if processed is not None and processed.status == "processed":
            await store.mark_source_processed(poster_source_id)
    return handled


async def handle_card_action(callback: CallbackQuery, store: Store) -> None:
    """Проверить сохранённое намерение и выполнить только его версию карточки."""
    message = callback.message
    data = callback.data or ""
    token = data[2:] if data.startswith("a:") else ""
    action = await store.get_card_action(token) if token else None
    if not isinstance(message, Message):
        await callback.answer("Устаревшая кнопка.", show_alert=True)
        return
    if (
        action is None
        or action.chat_id != message.chat.id
        or action.bot_message_id != message.message_id
    ):
        await callback.answer("Кнопка неизвестна или устарела.", show_alert=True)
        return
    if action.completed:
        if action.record_id is not None:
            record = await store.get_record(action.record_id)
            if record is not None:
                await show_record(message, record, store)
        await callback.answer((action.outcome or "Это действие уже обработано.")[:180])
        return

    try:
        if action.action.startswith("schedule_"):
            outcome = await handle_schedule_action(message, action, store)
            await store.complete_card_action(action.token, outcome)
        else:
            outcome = await _apply_card_action(message, action, store)
    except ValueError as error:
        outcome = str(error)
        await message.answer(outcome)
    else:
        if action.action in {"confirm", "reject", "cancel", "remove_poster"}:
            await store.complete_card_action(action.token, outcome)
    await callback.answer(outcome[:180])


async def _apply_card_action(message: Message, action: CardAction, store: Store) -> str:
    if action.action == "view":
        if action.draft_id is not None:
            draft = await store.get_draft(action.draft_id)
            if draft is not None and draft.status == "pending":
                await show_draft(message, draft, store)
                return "Черновик открыт."
        if action.record_id is not None:
            record = await store.get_record(action.record_id)
            if record is not None:
                await show_record(message, record, store)
                return "Карточка открыта."
        if action.source_id is not None:
            source = await store.get_source_by_id(action.source_id)
            if source is not None:
                await show_source(message, source, store)
                return "Исходное сообщение открыто."
        raise ValueError("Карточка изменилась. Откройте /events.")

    if action.action == "confirm":
        if action.draft_id is None or action.version is None:
            raise ValueError("Кнопка подтверждения повреждена.")
        record = await store.confirm_draft(action.draft_id, action.version)
        await show_record(message, record, store)
        return "Карточка сохранена."

    if action.action == "reject":
        if action.draft_id is None or action.version is None:
            raise ValueError("Кнопка отклонения повреждена.")
        await store.reject_draft(action.draft_id, action.version)
        await message.answer("Черновик отклонён; подтверждённая версия не менялась.")
        return "Черновик отклонён."

    if action.action == "cancel":
        if action.record_id is None or action.revision is None:
            raise ValueError("Кнопка отмены повреждена.")
        record = await store.cancel_record(action.record_id, action.revision)
        await message.answer(f"Запись №{record.id} отменена.")
        return "Запись отменена."

    if action.action == "edit":
        binding = CardBinding(
            chat_id=message.chat.id,
            bot_message_id=0,
            record_id=action.record_id,
            draft_id=action.draft_id,
            version=action.version,
            revision=action.revision,
        )
        context, stale = await _context_from_binding(binding, store)
        if context is None or stale:
            raise ValueError("Карточка устарела. Откройте /events.")
        prompt = await message.answer(
            "Отправьте полный шаблон ответом на это сообщение. "
            "Пропущенные необязательные поля очистятся; афиша сохранится."
        )
        await store.bind_card_message(
            binding.model_copy(update={"bot_message_id": prompt.message_id})
        )
        return "Ожидаю полный шаблон ответом на приглашение."

    if action.action == "retry_poster":
        if action.source_id is None:
            raise ValueError("Кнопка загрузки афиши повреждена.")
        source = await store.get_source_by_id(action.source_id)
        if source is None or not source.is_photo or source.attachment_status == "removed":
            raise ValueError("Афиша недоступна для повторной загрузки.")
        if action.draft_id is None:
            if any(
                source.id in (draft.source_id, draft.poster_source_id)
                for draft in await store.list_drafts()
            ):
                raise ValueError("Источник уже связан с карточкой. Откройте /events.")
            if await _handle_schedule_source(message, source, store):
                return "Состояние вложения обновлено."
            await _process_source(message, source, store, resolve_reply=True)
            return "Состояние вложения обновлено."
        draft = await store.get_draft(action.draft_id)
        if (
            draft is None
            or draft.version != action.version
            or draft.status != "pending"
            or draft.poster_source_id != source.id
        ):
            raise ValueError("Черновик устарел. Откройте /events.")
        if await _download_poster(message, source, store):
            await store.mark_source_processed(source.id)
            current_draft = await store.get_draft(draft.id) or draft
            await show_draft(message, current_draft, store)
            return "Афиша сохранена."
        current_draft = await store.get_draft(draft.id) or draft
        await show_draft(message, current_draft, store)
        return _POSTER_ERROR

    if action.action == "remove_poster":
        if action.record_id is None:
            if action.source_id is None:
                raise ValueError("Кнопка удаления афиши повреждена.")
            source = await store.get_source_by_id(action.source_id)
            if source is None or not source.is_photo:
                raise ValueError("Неизвестная афиша.")
            source = await store.remove_source_poster(source.id)
            await show_source(message, source, store)
            return "Незавершённое вложение убрано; исходник сохранён."

        previous_source = await store.get_source(message.chat.id, message.message_id)
        if previous_source is not None:
            await _repeat_source(message, previous_source, store)
            return "Действие уже сохранено."
        context, stale = await _context_from_binding(
            CardBinding(
                chat_id=message.chat.id,
                bot_message_id=message.message_id,
                record_id=action.record_id,
                draft_id=action.draft_id,
                version=action.version,
                revision=action.revision,
            ),
            store,
        )
        if context is None or stale or context.poster_source_id is None:
            raise ValueError("Карточка изменилась. Откройте /events.")
        action_source = await store.save_source(
            SourceInput(
                chat_id=message.chat.id,
                message_id=message.message_id,
                text="Владелец явно убрал афишу.",
                received_at=message.date,
            )
        )
        updated = await store.save_draft(
            action_source.id,
            context.data,
            poster_source_id=None,
            record_id=context.record_id,
            base_revision=context.base_revision,
            expected_draft_id=context.expected_draft_id,
        )
        await show_draft(message, updated, store)
        return "Создан черновик без афиши. Подтвердите его, чтобы применить изменение."

    if action.action == "events_page":
        await show_events(message, store, page=action.page or 0)
        return "Страница открыта."

    raise ValueError("Неизвестное действие карточки.")
