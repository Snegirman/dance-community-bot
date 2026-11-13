from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, ForceReply, Message, Update, User
from telegram_transport import RecordingSession

from dance_community_bot.bot import create_dispatcher
from dance_community_bot.config import Settings
from dance_community_bot.storage import Store

OWNER_ID = 111
TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
NOW = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)


def make_settings(database_path: Path, initial_time: time | None = time(9, 0)) -> Settings:
    return Settings(
        telegram_bot_token=TOKEN,
        telegram_owner_id=OWNER_ID,
        telegram_webhook_url="https://dance.example.org/telegram/webhook",
        telegram_webhook_secret="test_webhook_secret",
        database_path=database_path,
        weekly_publication_time=initial_time,
    )


def private_message(
    bot: Bot,
    message_id: int,
    text: str,
    *,
    reply_to_message_id: int | None = None,
    edited: bool = False,
    user_id: int = OWNER_ID,
    chat_id: int = OWNER_ID,
    chat_type: str = "private",
) -> Message:
    reply = (
        Message(
            message_id=reply_to_message_id,
            date=NOW,
            chat=Chat(id=chat_id, type=chat_type),
            from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
            text="Введите время",
        )
        if reply_to_message_id is not None
        else None
    )
    message = Message(
        message_id=message_id,
        date=NOW,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="Владелец"),
        text=text,
        reply_to_message=reply,
        edit_date=int(NOW.timestamp()) if edited else None,
    )
    return message.as_(bot)


def callback_update(
    bot: Bot,
    *,
    update_id: int,
    message_id: int,
    callback_data: str,
    user_id: int = OWNER_ID,
    chat_id: int = OWNER_ID,
    chat_type: str = "private",
) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"settings-{update_id}",
            from_user=User(id=user_id, is_bot=False, first_name="Владелец"),
            chat_instance="settings-test",
            message=Message(
                message_id=message_id,
                date=NOW,
                chat=Chat(id=chat_id, type=chat_type),
                text="Настройки автопубликации",
            ).as_(bot),
            data=callback_data,
        ),
    )


def sent_messages(session: RecordingSession) -> list[SendMessage]:
    return [request for request in session.requests if isinstance(request, SendMessage)]


def telegram_message_id(session: RecordingSession, request: SendMessage) -> int:
    return 100 + sent_messages(session).index(request) + 1


def button_data(request: SendMessage, label: str) -> str:
    assert request.reply_markup is not None
    button = next(
        button
        for row in request.reply_markup.inline_keyboard
        for button in row
        if button.text == label
    )
    assert button.callback_data is not None
    return button.callback_data


async def open_time_prompt(
    dispatcher: Dispatcher, bot: Bot, session: RecordingSession, *, offset: int
) -> int:
    settings_message = sent_messages(session)[-1]
    settings_message_id = telegram_message_id(session, settings_message)
    await dispatcher.feed_update(
        bot,
        callback_update(
            bot,
            update_id=offset,
            message_id=settings_message_id,
            callback_data=button_data(settings_message, "Изменить время"),
        ),
    )
    prompt = sent_messages(session)[-1]
    assert isinstance(prompt.reply_markup, ForceReply)
    return telegram_message_id(session, prompt)


def test_settings_shows_database_time_and_buttons(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(10, 30))
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=private_message(bot, 1, "/settings"),
                ),
            )

            response = sent_messages(session)[-1]
            assert "10:30" in response.text
            assert button_data(response, "Изменить время")
            assert button_data(response, "Отключить автопубликацию")
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_valid_time_reply_is_saved_and_status_reads_database_value(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize(time(9, 0))
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(database), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 1, "/settings")),
            )
            prompt_id = await open_time_prompt(dispatcher, bot, session, offset=2)
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=3,
                    message=private_message(bot, 2, "13:45", reply_to_message_id=prompt_id),
                ),
            )
            setting = await store.get_weekly_publication_setting()
            assert setting.weekly_publication_time == time(13, 45)
            assert setting.revision == 1

            await dispatcher.feed_update(
                bot,
                Update(update_id=4, message=private_message(bot, 3, "/status")),
            )
            assert "13:45" in sent_messages(session)[-1].text
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_invalid_time_does_not_change_setting_and_prompt_accepts_correction(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 1, "/settings")),
            )
            prompt_id = await open_time_prompt(dispatcher, bot, session, offset=2)
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=3,
                    message=private_message(bot, 2, "9:00", reply_to_message_id=prompt_id),
                ),
            )
            unchanged = await store.get_weekly_publication_setting()
            assert unchanged.weekly_publication_time == time(9, 0)
            assert unchanged.revision == 0

            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=4,
                    message=private_message(bot, 3, "10:15", reply_to_message_id=prompt_id),
                ),
            )
            changed = await store.get_weekly_publication_setting()
            assert changed.weekly_publication_time == time(10, 15)
            assert changed.revision == 1
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_same_time_reply_consumes_prompt_without_revision_change(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 1, "/settings")),
            )
            prompt_id = await open_time_prompt(dispatcher, bot, session, offset=2)
            same_answer = Update(
                update_id=3,
                message=private_message(bot, 2, "09:00", reply_to_message_id=prompt_id),
            )
            await dispatcher.feed_update(bot, same_answer)
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=4,
                    message=private_message(bot, 3, "11:30", reply_to_message_id=prompt_id),
                ),
            )

            setting = await store.get_weekly_publication_setting()
            assert setting.weekly_publication_time == time(9, 0)
            assert setting.revision == 0
            assert await store.list_sources() == []
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_duplicate_old_reply_and_edited_reply_cannot_revert_setting_or_enter_intake(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 1, "/settings")),
            )
            prompt_id = await open_time_prompt(dispatcher, bot, session, offset=2)
            valid_reply = Update(
                update_id=3,
                message=private_message(bot, 2, "10:15", reply_to_message_id=prompt_id),
            )
            await dispatcher.feed_update(bot, valid_reply)
            await dispatcher.feed_update(bot, valid_reply)
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=4,
                    edited_message=private_message(
                        bot,
                        2,
                        "08:00",
                        reply_to_message_id=prompt_id,
                        edited=True,
                    ),
                ),
            )
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=5,
                    message=private_message(bot, 3, "07:30", reply_to_message_id=prompt_id),
                ),
            )

            setting = await store.get_weekly_publication_setting()
            assert setting.weekly_publication_time == time(10, 15)
            assert setting.revision == 1
            assert await store.list_sources() == []
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_pending_prompt_and_setting_survive_store_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize(time(10, 30))
        first_session = RecordingSession(OWNER_ID)
        first_bot = Bot(token=TOKEN, session=first_session)
        first_dispatcher = create_dispatcher(make_settings(database), store)
        await first_dispatcher.feed_update(
            first_bot,
            Update(update_id=1, message=private_message(first_bot, 1, "/settings")),
        )
        prompt_id = await open_time_prompt(first_dispatcher, first_bot, first_session, offset=2)
        await first_bot.session.close()

        reopened = Store(database)
        await reopened.initialize(time(14, 0))
        second_session = RecordingSession(OWNER_ID)
        second_bot = Bot(token=TOKEN, session=second_session)
        second_dispatcher = create_dispatcher(make_settings(database), reopened)
        try:
            await second_dispatcher.feed_update(
                second_bot,
                Update(
                    update_id=3,
                    message=private_message(second_bot, 2, "11:30", reply_to_message_id=prompt_id),
                ),
            )
            setting = await reopened.get_weekly_publication_setting()
            assert setting.weekly_publication_time == time(11, 30)
            assert setting.revision == 1
        finally:
            await second_bot.session.close()

    asyncio.run(scenario())


def test_disable_is_explicit_and_does_not_fall_back_to_environment(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize(time(9, 0))
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(database), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 1, "/settings")),
            )
            settings_message = sent_messages(session)[-1]
            await dispatcher.feed_update(
                bot,
                callback_update(
                    bot,
                    update_id=2,
                    message_id=telegram_message_id(session, settings_message),
                    callback_data=button_data(settings_message, "Отключить автопубликацию"),
                ),
            )
            disabled = await store.get_weekly_publication_setting()
            assert disabled.weekly_publication_time is None
            assert disabled.revision == 1

            reopened = Store(database)
            await reopened.initialize(time(14, 0))
            persisted = await reopened.get_weekly_publication_setting()
            assert persisted.weekly_publication_time is None
            assert persisted.revision == 1
        finally:
            await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("user_id", "chat_id", "chat_type"),
    [(OWNER_ID + 1, OWNER_ID + 1, "private"), (OWNER_ID, -100123, "supergroup")],
)
def test_settings_command_requires_owner_in_private_chat(
    tmp_path: Path, user_id: int, chat_id: int, chat_type: str
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=private_message(
                        bot,
                        1,
                        "/settings",
                        user_id=user_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                    ),
                ),
            )
            assert sent_messages(session) == []
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_stale_settings_callback_does_not_change_time(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 1, "/settings")),
            )
            old_settings_message = sent_messages(session)[-1]
            old_callback_data = button_data(old_settings_message, "Отключить автопубликацию")
            old_message_id = telegram_message_id(session, old_settings_message)
            prompt_id = await open_time_prompt(dispatcher, bot, session, offset=2)
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=3,
                    message=private_message(bot, 2, "10:00", reply_to_message_id=prompt_id),
                ),
            )
            await dispatcher.feed_update(
                bot,
                callback_update(
                    bot,
                    update_id=4,
                    message_id=old_message_id,
                    callback_data=old_callback_data,
                ),
            )

            setting = await store.get_weekly_publication_setting()
            assert setting.weekly_publication_time == time(10, 0)
            assert setting.revision == 1
        finally:
            await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("user_id", "chat_id", "chat_type"),
    [(OWNER_ID + 1, OWNER_ID + 1, "private"), (OWNER_ID, -100123, "supergroup")],
)
def test_owner_callback_requires_owner_and_private_chat(
    tmp_path: Path, user_id: int, chat_id: int, chat_type: str
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 1, "/settings")),
            )
            settings_message = sent_messages(session)[-1]
            callback_data = button_data(settings_message, "Отключить автопубликацию")
            message_id = telegram_message_id(session, settings_message)
            await dispatcher.feed_update(
                bot,
                callback_update(
                    bot,
                    update_id=2,
                    message_id=message_id,
                    callback_data=callback_data,
                    user_id=user_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                ),
            )
            setting = await store.get_weekly_publication_setting()
            assert setting.weekly_publication_time == time(9, 0)
            assert setting.revision == 0
            assert len(sent_messages(session)) == 1
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_active_settings_prompt_does_not_capture_event_card_reply(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 1, "/settings")),
            )
            await open_time_prompt(dispatcher, bot, session, offset=2)
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=3,
                    message=private_message(
                        bot,
                        2,
                        "Название: Джем\nДата: 14.11.2026\nТип: Джем",
                        reply_to_message_id=777,
                    ),
                ),
            )

            assert len(await store.list_sources()) == 1
            setting = await store.get_weekly_publication_setting()
            assert setting.weekly_publication_time == time(9, 0)
            assert setting.revision == 0
        finally:
            await bot.session.close()

    asyncio.run(scenario())
