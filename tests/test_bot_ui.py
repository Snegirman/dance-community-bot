from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)
from pydantic import SecretStr
from telegram_transport import RecordingSession

from dance_community_bot import bot as bot_module
from dance_community_bot.bot import create_dispatcher
from dance_community_bot.config import Settings
from dance_community_bot.storage import Store
from dance_community_bot.templates import parse_class_template, parse_event_template

OWNER_ID = 222
TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"


@pytest.fixture
def client() -> tuple[Bot, RecordingSession]:
    session = RecordingSession(OWNER_ID)
    return Bot(token=TOKEN, session=session), session


@pytest.fixture
def app(tmp_path: Path) -> tuple[Dispatcher, Store]:
    settings = Settings(
        telegram_bot_token=SecretStr(TOKEN),
        telegram_owner_id=OWNER_ID,
        telegram_webhook_url="https://dance.example.org/telegram/webhook",
        telegram_webhook_secret=SecretStr("test_webhook_secret"),
        database_path=tmp_path / "dance.sqlite3",
        telegram_group_id=None,
        weekly_publication_time=None,
        _env_file=None,
    )
    store = Store(settings.database_path)
    asyncio.run(store.initialize())
    return create_dispatcher(settings, store), store


def make_message_update(
    update_id: int,
    text: str,
    *,
    user_id: int = OWNER_ID,
    chat_type: str = "private",
) -> Update:
    chat_id = user_id if chat_type == "private" else -100123
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=User(id=user_id, is_bot=False, first_name="Владелец"),
            text=text,
        ),
    )


def make_callback_update(
    update_id: int,
    data: str,
    *,
    user_id: int = OWNER_ID,
    chat_type: str = "private",
) -> Update:
    chat_id = user_id if chat_type == "private" else -100123
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
    )
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"callback-{update_id}",
            from_user=User(id=user_id, is_bot=False, first_name="Владелец"),
            chat_instance="test-chat",
            data=data,
            message=message,
        ),
    )


def feed(dispatcher: Dispatcher, bot: Bot, update: Update) -> None:
    asyncio.run(dispatcher.feed_update(bot, update))


def _utf16_slice(value: str, offset: int, length: int) -> str:
    encoded = value.encode("utf-16-le")
    return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")


def test_start_and_help_show_persistent_schedule_keyboard(
    client: tuple[Bot, RecordingSession], app: tuple[Dispatcher, Store]
) -> None:
    bot, session = client
    dispatcher, _ = app

    feed(dispatcher, bot, make_message_update(1, "/start"))
    feed(dispatcher, bot, make_message_update(2, "/help"))

    replies = [method for method in session.requests if isinstance(method, SendMessage)]
    assert len(replies) == 2
    for reply in replies:
        assert isinstance(reply.reply_markup, ReplyKeyboardMarkup)
        assert reply.reply_markup.is_persistent is True
        assert reply.reply_markup.resize_keyboard is True
        assert [button.text for row in reply.reply_markup.keyboard for button in row] == [
            "Расписание"
        ]


def test_schedule_menu_shows_current_and_next_moscow_weeks_and_is_read_only(
    client: tuple[Bot, RecordingSession],
    app: tuple[Dispatcher, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, session = client
    dispatcher, store = app

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            value = cls(2026, 11, 27, 12, 0, tzinfo=UTC)
            return value.astimezone(tz) if tz is not None else value

    monkeypatch.setattr(bot_module, "datetime", FrozenDateTime)
    feed(dispatcher, bot, make_message_update(3, "Расписание"))

    menu = next(method for method in session.requests if isinstance(method, SendMessage))
    assert menu.reply_markup is not None
    labels = [button.text for row in menu.reply_markup.inline_keyboard for button in row]
    assert labels == [
        "Эта неделя · 23.11.2026 — 29.11.2026",
        "Следующая неделя · 30.11.2026 — 06.12.2026",
    ]
    callbacks = [
        button.callback_data for row in menu.reply_markup.inline_keyboard for button in row
    ]
    assert callbacks == ["schedule-week:2026-11-23", "schedule-week:2026-11-30"]
    assert asyncio.run(store.list_sources()) == []


def test_schedule_keyboard_reply_bypasses_pending_settings_time_prompt(
    client: tuple[Bot, RecordingSession], app: tuple[Dispatcher, Store]
) -> None:
    bot, session = client
    dispatcher, store = app
    setting = asyncio.run(store.get_weekly_publication_setting())
    prompt_id = 50
    assert asyncio.run(store.bind_weekly_publication_prompt(OWNER_ID, prompt_id, setting.revision))
    prompt = Message(
        message_id=prompt_id,
        date=datetime.now(UTC),
        chat=Chat(id=OWNER_ID, type="private"),
        from_user=User(id=TOKEN.split(":", maxsplit=1)[0], is_bot=True, first_name="Бот"),
        text="Введите время автопубликации",
    )
    update = make_message_update(8, "Расписание")
    assert update.message is not None
    update = update.model_copy(
        update={"message": update.message.model_copy(update={"reply_to_message": prompt})}
    )

    feed(dispatcher, bot, update)

    assert any(
        isinstance(request, SendMessage)
        and request.reply_markup is not None
        and hasattr(request.reply_markup, "inline_keyboard")
        for request in session.requests
    )
    assert asyncio.run(store.list_sources()) == []
    assert asyncio.run(store.get_weekly_publication_setting()) == setting


def test_schedule_callback_shows_selected_week_and_navigation(
    client: tuple[Bot, RecordingSession], app: tuple[Dispatcher, Store]
) -> None:
    bot, session = client
    dispatcher, _ = app

    feed(dispatcher, bot, make_callback_update(4, "schedule-week:2026-11-23"))

    messages = [method for method in session.requests if isinstance(method, SendMessage)]
    assert messages[0].text == "Расписание · 23.11.2026 — 29.11.2026"
    navigation = messages[-1]
    assert navigation.reply_markup is not None
    assert [
        button.callback_data for row in navigation.reply_markup.inline_keyboard for button in row
    ] == ["schedule-week:2026-11-16", "schedule-week:2026-11-30"]


@pytest.mark.parametrize(
    "data", ["schedule-week:wrong", "schedule-week:0001-01-01", "schedule-week:9999-99-99"]
)
def test_malformed_or_extreme_schedule_callbacks_are_rejected(
    client: tuple[Bot, RecordingSession],
    app: tuple[Dispatcher, Store],
    data: str,
) -> None:
    bot, session = client
    dispatcher, _ = app

    feed(dispatcher, bot, make_callback_update(5, data))

    answers = [method for method in session.requests if isinstance(method, AnswerCallbackQuery)]
    assert len(answers) == 1
    assert answers[0].show_alert is True
    assert not any(isinstance(method, SendMessage) for method in session.requests)


@pytest.mark.parametrize(("user_id", "chat_type"), [(OWNER_ID + 1, "private"), (OWNER_ID, "group")])
def test_schedule_callbacks_are_limited_to_owner_private_chat(
    client: tuple[Bot, RecordingSession],
    app: tuple[Dispatcher, Store],
    user_id: int,
    chat_type: str,
) -> None:
    bot, session = client
    dispatcher, _ = app

    feed(
        dispatcher,
        bot,
        make_callback_update(
            6,
            "schedule-week:2026-11-23",
            user_id=user_id,
            chat_type=chat_type,
        ),
    )

    assert session.requests == []


@pytest.mark.parametrize(
    ("command", "expected_pre", "parser"),
    [
        ("/event", "Дата: 14.11.2026", parse_event_template),
        ("/class", "Первая дата: 10.11.2026", parse_class_template),
    ],
)
def test_copy_templates_are_preformatted_and_accepted_by_parser(
    client: tuple[Bot, RecordingSession],
    app: tuple[Dispatcher, Store],
    command: str,
    expected_pre: str,
    parser: Callable[[str], object],
) -> None:
    bot, session = client
    dispatcher, _ = app

    feed(dispatcher, bot, make_message_update(7, command))

    reply = next(method for method in session.requests if isinstance(method, SendMessage))
    pre_entities = [entity for entity in reply.entities or [] if entity.type == "pre"]
    assert len(pre_entities) == 1
    template = _utf16_slice(reply.text, pre_entities[0].offset, pre_entities[0].length)
    assert expected_pre in template
    assert "Обязательные поля" in reply.text
    assert all(
        "обязательн" in line.casefold() or "необязательн" in line.casefold()
        for line in reply.text.splitlines()
        if "поля" in line.casefold()
    )
    parser(template)
