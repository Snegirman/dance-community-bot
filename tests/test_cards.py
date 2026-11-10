from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, User
from telegram_transport import RecordingSession

from dance_community_bot.cards import (
    _card_warnings,
    format_card,
    show_draft,
    show_events,
    show_record,
    show_source,
)
from dance_community_bot.models import EventData, SourceInput
from dance_community_bot.storage import Store

TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
CHAT_ID = 222


def test_card_past_date_warning_uses_template_warning() -> None:
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    data = EventData(
        title="Jam",
        event_type="Джем",
        start_date=today - timedelta(days=2),
    )

    assert _card_warnings(data, []) == ["Предупреждение: Поле «Дата» события уже прошло."]


def test_card_keeps_multiline_optional_fields_without_truncating() -> None:
    description = "Первая строка\n" + ("длинный текст " * 500).rstrip()
    data = EventData(
        title="Popping Jam",
        event_type="Джем",
        start_date=date(2026, 11, 14),
        price="До 12 ноября — 500 ₽\nНа входе — 700 ₽",
        description=description,
    )

    card = format_card(data)

    assert "Название: Popping Jam" in card
    assert "Тип: Джем" in card
    assert "Время: не указано" in card
    assert "Место: не указано" in card
    assert "До 12 ноября — 500 ₽\nНа входе — 700 ₽" in card
    assert description in card


def test_card_week_label_handles_date_max() -> None:
    data = EventData(
        title="Последнее событие",
        event_type="Встреча",
        start_date=date.max,
    )

    card = format_card(data)

    assert "31.12.9999" in card
    assert "Неделя" in card


def test_duplicate_warning_links_existing_card_and_skips_record_being_edited(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        duplicate_data = EventData(
            title="Popping Jam",
            event_type="Джем",
            start_date=date(2026, 11, 14),
            start_time=time(18, 30),
        )
        source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=1,
                text="existing",
                received_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            )
        )
        existing_draft = await store.save_draft(source.id, duplicate_data, poster_source_id=None)
        existing = await store.confirm_draft(existing_draft.id, existing_draft.version)
        duplicate_source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=2,
                text="duplicate",
                received_at=datetime(2026, 11, 7, 12, 1, tzinfo=UTC),
            )
        )
        duplicate = await store.save_draft(
            duplicate_source.id, duplicate_data, poster_source_id=None
        )
        message = Message(
            message_id=3,
            date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        ).as_(bot)

        await show_draft(message, duplicate, store)

        first_card = session.requests[0]
        assert isinstance(first_card, SendMessage)
        assert "похоже на событие" in first_card.text.lower()
        assert first_card.reply_markup is not None
        duplicate_link = next(
            button
            for row in first_card.reply_markup.inline_keyboard
            for button in row
            if button.text == f"Открыть №{existing.id}"
        )
        assert duplicate_link.callback_data is not None
        duplicate_action = await store.get_card_action(duplicate_link.callback_data[2:])
        assert duplicate_action is not None and duplicate_action.action == "view"
        assert duplicate_action.record_id == existing.id

        edit_source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=4,
                text="edit",
                received_at=datetime(2026, 11, 7, 12, 2, tzinfo=UTC),
            )
        )
        edit = await store.save_draft(
            edit_source.id,
            duplicate_data,
            poster_source_id=None,
            record_id=existing.id,
            base_revision=existing.revision,
        )
        edit_message = message.model_copy(update={"message_id": 5})
        await show_draft(edit_message, edit, store)

        second_card = session.requests[1]
        assert isinstance(second_card, SendMessage)
        assert f"похоже на событие в карточке №{existing.id}." not in second_card.text.lower()
        assert f"похоже на событие в карточке №{duplicate.record_id}." in second_card.text.lower()
        assert second_card.reply_markup is not None
        edit_actions = [
            await store.get_card_action(button.callback_data[2:])
            for row in second_card.reply_markup.inline_keyboard
            for button in row
            if button.callback_data is not None
        ]
        assert not any(
            action is not None and action.action == "view" and action.record_id == existing.id
            for action in edit_actions
        )
        assert any(
            action is not None and action.action == "view" and action.draft_id == duplicate.id
            for action in edit_actions
        )

        await store.reject_draft(duplicate.id, duplicate.version)
        changed_edit_source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=6,
                text="changed edit",
                received_at=datetime(2026, 11, 7, 12, 3, tzinfo=UTC),
            )
        )
        await store.save_draft(
            changed_edit_source.id,
            duplicate_data.model_copy(update={"title": "Changed title"}),
            poster_source_id=None,
            record_id=existing.id,
            base_revision=existing.revision,
        )
        incoming_source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=7,
                text="incoming duplicate",
                received_at=datetime(2026, 11, 7, 12, 4, tzinfo=UTC),
            )
        )
        incoming = await store.save_draft(incoming_source.id, duplicate_data, poster_source_id=None)
        await show_draft(message.model_copy(update={"message_id": 8}), incoming, store)

        third_card = session.requests[2]
        assert isinstance(third_card, SendMessage)
        assert f"похоже на событие в карточке №{existing.id}." in third_card.text.lower()
        assert (
            f"похоже на событие в карточке №{duplicate.record_id}." not in third_card.text.lower()
        )
        assert third_card.reply_markup is not None
        active_links = []
        for row in third_card.reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data is None:
                    continue
                action = await store.get_card_action(button.callback_data.removeprefix("a:"))
                if action is not None and action.action == "view":
                    active_links.append(action)
        assert len(active_links) == 1
        assert active_links[0].record_id == existing.id
        assert active_links[0].draft_id is None
        await bot.session.close()

    asyncio.run(scenario())


def test_show_draft_persists_reply_binding_and_callback_intents(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=17,
                text="карточка",
                received_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            )
        )
        draft = await store.save_draft(
            source.id,
            EventData(title="Popping Jam", event_type="Джем", start_date=date(2026, 11, 14)),
            poster_source_id=None,
        )
        message = Message(
            message_id=17,
            date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        ).as_(bot)

        await show_draft(message, draft, store)

        sent = session.requests[0]
        assert isinstance(sent, SendMessage)
        callback_data = sent.reply_markup.inline_keyboard[0][0].callback_data
        assert callback_data is not None
        action = await store.get_card_action(callback_data[2:])
        binding = await store.get_card_binding(CHAT_ID, 101)
        assert action is not None
        assert action.action == "confirm"
        assert action.draft_id == draft.id
        assert action.chat_id == CHAT_ID
        assert action.bot_message_id == 101
        assert binding is not None
        assert binding.record_id == draft.record_id
        assert binding.version == draft.version

        await bot.session.close()

    asyncio.run(scenario())


def test_events_list_paginates_records_instead_of_dropping_them(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        for index in range(6):
            source = await store.save_source(
                SourceInput(
                    chat_id=CHAT_ID,
                    message_id=index + 1,
                    text=f"source {index + 1}",
                    received_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
                )
            )
            draft = await store.save_draft(
                source.id,
                EventData(
                    title=f"Event {index + 1}",
                    event_type="Встреча",
                    start_date=date(2026, 11, 14 + index),
                ),
                poster_source_id=None,
            )
            await store.confirm_draft(draft.id, draft.version)
        message = Message(
            message_id=20,
            date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        ).as_(bot)

        await show_events(message, store)

        first_page = session.messages[0]
        assert "Страница 1 из 2" in first_page
        assert all(f"Event {index}" in first_page for index in range(1, 6))
        assert "Event 6" not in first_page
        sent = session.requests[0]
        assert isinstance(sent, SendMessage)
        assert sent.reply_markup is not None
        actions = []
        for row in sent.reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data is None or not button.callback_data.startswith("a:"):
                    continue
                action = await store.get_card_action(button.callback_data[2:])
                assert action is not None
                actions.append(action)
        assert sum(action.action == "view" for action in actions) == 5
        next_page = next(action for action in actions if action.action == "events_page")
        assert next_page.page == 1
        assert all(action.chat_id == CHAT_ID and action.bot_message_id == 101 for action in actions)

        await bot.session.close()

    asyncio.run(scenario())


def test_source_only_photo_card_has_retry_and_remove_actions(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=17,
                received_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
                telegram_file_id="poster-file-id",
                is_photo=True,
            )
        )
        await store.set_source_error(source.id, "ошибка загрузки")
        message = Message(
            message_id=18,
            date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        ).as_(bot)

        stored_source = await store.get_source_by_id(source.id)
        assert stored_source is not None
        await show_source(message, stored_source, store)

        sent = session.requests[0]
        assert isinstance(sent, SendMessage)
        assert sent.reply_markup is not None
        actions = [
            await store.get_card_action(button.callback_data[2:])
            for row in sent.reply_markup.inline_keyboard
            for button in row
            if button.callback_data is not None
        ]
        assert {action.action for action in actions if action is not None} == {
            "retry_poster",
            "remove_poster",
        }
        assert all(action.source_id == source.id for action in actions if action)

        await bot.session.close()

    asyncio.run(scenario())


def test_source_error_card_keeps_the_replied_card_binding(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        original = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=17,
                text="карточка",
                received_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            )
        )
        draft = await store.save_draft(
            original.id,
            EventData(title="Popping Jam", event_type="Джем", start_date=date(2026, 11, 14)),
            poster_source_id=None,
        )
        draft_message = Message(
            message_id=17,
            date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        ).as_(bot)
        await show_draft(draft_message, draft, store)
        source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=18,
                received_at=datetime(2026, 11, 7, 12, 1, tzinfo=UTC),
                telegram_file_id="failed-photo",
                is_photo=True,
                reply_to_message_id=101,
            )
        )
        await store.set_source_error(source.id, "шаблон неполон")
        source_message = Message(
            message_id=18,
            date=datetime(2026, 11, 7, 12, 1, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        ).as_(bot)

        await show_source(source_message, source, store)

        binding = await store.get_card_binding(CHAT_ID, 102)
        assert binding is not None
        assert binding.record_id == draft.record_id
        assert binding.draft_id == draft.id
        assert binding.version == draft.version
        assert binding.source_id == source.id
        await bot.session.close()

    asyncio.run(scenario())


def test_confirmed_record_has_explicit_edit_and_poster_removal_actions(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=17,
                text="карточка",
                received_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
                telegram_file_id="poster-file",
            )
        )
        poster_path = Path("posters") / "card.jpg"
        (store.path.parent / poster_path).write_bytes(b"poster")
        await store.set_source_poster_path(source.id, poster_path)
        draft = await store.save_draft(
            source.id,
            EventData(title="Popping Jam", event_type="Джем", start_date=date(2026, 11, 14)),
            poster_source_id=source.id,
        )
        record = await store.confirm_draft(draft.id, draft.version)
        message = Message(
            message_id=17,
            date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        ).as_(bot)

        await show_record(message, record, store)

        sent = session.requests[0]
        assert isinstance(sent, SendMessage)
        buttons = [
            button
            for row in sent.reply_markup.inline_keyboard
            for button in row
            if button.callback_data is not None
        ]
        actions = [await store.get_card_action(button.callback_data[2:]) for button in buttons]
        assert {action.action for action in actions if action is not None} == {
            "cancel",
            "edit",
            "remove_poster",
        }
        assert all(
            action.record_id == record.id and action.revision == record.revision
            for action in actions
            if action
        )
        removal = next(action for action in actions if action and action.action == "remove_poster")
        assert removal.source_id == source.id

        await bot.session.close()

    asyncio.run(scenario())
