from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from pydantic import ValidationError

from dance_community_bot.access import is_owner_private
from dance_community_bot.bot import create_dispatcher
from dance_community_bot.cards import show_draft
from dance_community_bot.config import Settings
from dance_community_bot.models import CardActionName, EventData, SourceInput
from dance_community_bot.storage import Store

OWNER_ID = 222
TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[TelegramType]] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        self.requests.append(method)
        if isinstance(method, SendMessage):
            if not isinstance(method.chat_id, int):
                raise AssertionError("The test sends messages only to numeric chats")
            result: dict[str, object] | bool = {
                "message_id": 1,
                "date": 1793782800,
                "chat": {"id": method.chat_id, "type": "private"},
                "text": method.text,
            }
        elif isinstance(method, AnswerCallbackQuery):
            result = True
        else:
            raise AssertionError(f"Unexpected Telegram method: {method.__class__.__name__}")
        response = self.check_response(
            bot=bot,
            method=method,
            status_code=200,
            content=self.json_dumps({"ok": True, "result": result}),
        )
        assert response.result is not None
        return response.result

    async def stream_content(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        yield b""


@pytest.fixture
def client() -> Iterator[tuple[Bot, RecordingSession]]:
    session = RecordingSession()
    bot = Bot(token=TOKEN, session=session)
    yield bot, session
    asyncio.run(session.close())


@pytest.fixture
def app(tmp_path: Path) -> tuple[Dispatcher, Store]:
    settings = Settings(
        telegram_bot_token=TOKEN,
        telegram_owner_id=OWNER_ID,
        telegram_webhook_url="https://dance.example.org/telegram/webhook",
        telegram_webhook_secret="test_webhook_secret",
        database_path=tmp_path / "dance.sqlite3",
    )
    store = Store(settings.database_path)
    asyncio.run(store.initialize())
    return create_dispatcher(settings, store), store


def make_user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Пользователь")


def make_message_update(
    *,
    update_id: int,
    chat_type: str = "private",
    user_id: int | None = OWNER_ID,
    text: str = "материал",
) -> Update:
    chat_id = user_id if chat_type == "private" and user_id is not None else -100123
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=make_user(user_id) if user_id is not None else None,
            text=text,
        ),
    )


def make_callback_update(
    *,
    update_id: int,
    user_id: int = OWNER_ID,
    chat_type: str | None = "private",
) -> Update:
    message = (
        Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id if chat_type == "private" else -100123, type=chat_type),
        )
        if chat_type is not None
        else None
    )
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"callback-{update_id}",
            from_user=make_user(user_id),
            chat_instance="test-chat",
            data="test-action",
            message=message,
            inline_message_id="inline-message" if chat_type is None else None,
        ),
    )


def feed(dispatcher: Dispatcher, bot: Bot, update: Update) -> None:
    asyncio.run(dispatcher.feed_update(bot, update))


def message_texts(session: RecordingSession) -> list[str]:
    return [method.text for method in session.requests if isinstance(method, SendMessage)]


@pytest.mark.parametrize(
    ("user_id", "chat_type", "expected"),
    [
        (OWNER_ID, "private", True),
        (OWNER_ID + 1, "private", False),
        (OWNER_ID, "group", False),
        (OWNER_ID, "supergroup", False),
        (OWNER_ID, "channel", False),
        (None, "private", False),
    ],
)
def test_owner_private_helper_requires_owner_and_private_chat(
    user_id: int | None, chat_type: str, expected: bool
) -> None:
    assert is_owner_private(user_id, chat_type, OWNER_ID) is expected


@pytest.mark.parametrize("command", ["/help", "/start"])
def test_commands_work_without_prior_start_and_explain_current_limits(
    client: tuple[Bot, RecordingSession], app: tuple[Dispatcher, Store], command: str
) -> None:
    bot, session = client
    dispatcher, _ = app

    feed(dispatcher, bot, make_message_update(update_id=1, text=command))

    assert len(message_texts(session)) == 1
    assert message_texts(session)[0].strip()
    if command == "/help":
        assert "/events" in message_texts(session)[0]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/event", "Дата: 14.11.2026"),
        ("/class", "Первая дата: 10.11.2026"),
        ("/events", "Страница 1 из 1"),
        ("/schedule 09.11.2026", "Расписание"),
    ],
)
def test_intake_commands_are_available_to_owner(
    client: tuple[Bot, RecordingSession],
    app: tuple[Dispatcher, Store],
    command: str,
    expected: str,
) -> None:
    bot, session = client
    dispatcher, _ = app

    feed(dispatcher, bot, make_message_update(update_id=20, text=command))

    assert expected in "\n".join(message_texts(session))


def test_help_only_lists_implemented_input_routes(
    client: tuple[Bot, RecordingSession], app: tuple[Dispatcher, Store]
) -> None:
    bot, session = client
    dispatcher, _ = app

    feed(dispatcher, bot, make_message_update(update_id=21, text="/help"))

    text = "\n".join(message_texts(session))
    assert all(command in text for command in ("/event", "/class", "/events"))
    assert "ещё не реализ" not in text.lower()


def test_owner_malformed_material_is_saved_without_creating_catalog_record(
    client: tuple[Bot, RecordingSession],
    app: tuple[Dispatcher, Store],
) -> None:
    bot, session = client
    dispatcher, store = app

    feed(dispatcher, bot, make_message_update(update_id=2, text="Занятие в пятницу"))

    assert len(asyncio.run(store.list_sources())) == 1
    assert asyncio.run(store.list_drafts()) == []
    assert asyncio.run(store.list_records()) == []


@pytest.mark.parametrize(
    ("user_id", "chat_type"),
    [
        (OWNER_ID + 1, "private"),
        (OWNER_ID, "group"),
        (OWNER_ID, "supergroup"),
        (OWNER_ID, "channel"),
        (None, "private"),
    ],
)
@pytest.mark.parametrize("text", ["материал", "/help", "/start", "/schedule 09.11.2026"])
def test_disallowed_messages_are_ignored(
    client: tuple[Bot, RecordingSession],
    app: tuple[Dispatcher, Store],
    user_id: int | None,
    chat_type: str,
    text: str,
) -> None:
    bot, session = client
    dispatcher, store = app

    feed(
        dispatcher,
        bot,
        make_message_update(update_id=3, user_id=user_id, chat_type=chat_type, text=text),
    )

    assert message_texts(session) == []
    assert asyncio.run(store.list_sources()) == []


@pytest.mark.parametrize(
    ("user_id", "chat_type"),
    [
        (OWNER_ID + 1, "private"),
        (OWNER_ID, "group"),
        (OWNER_ID, None),
    ],
)
def test_disallowed_callbacks_do_not_reach_handlers(
    client: tuple[Bot, RecordingSession],
    app: tuple[Dispatcher, Store],
    user_id: int,
    chat_type: str | None,
) -> None:
    bot, _ = client
    dispatcher, _ = app
    handled: list[str] = []

    async def handler(callback: CallbackQuery) -> None:
        handled.append(callback.data or "")

    dispatcher.callback_query.register(handler)
    feed(dispatcher, bot, make_callback_update(update_id=4, user_id=user_id, chat_type=chat_type))

    assert handled == []


def test_owner_private_callback_reaches_handlers_after_the_access_barrier(
    client: tuple[Bot, RecordingSession],
    app: tuple[Dispatcher, Store],
) -> None:
    bot, session = client
    dispatcher, _ = app
    feed(dispatcher, bot, make_callback_update(update_id=5))

    assert any(isinstance(method, AnswerCallbackQuery) for method in session.requests)


@pytest.mark.parametrize(
    ("user_id", "chat_type"),
    [(OWNER_ID + 1, "private"), (OWNER_ID, "group"), (OWNER_ID, "supergroup")],
)
def test_all_bound_schedule_actions_require_owner_private_chat(
    client: tuple[Bot, RecordingSession],
    app: tuple[Dispatcher, Store],
    user_id: int,
    chat_type: str,
) -> None:
    bot, session = client
    dispatcher, store = app
    actions: list[CardActionName] = [
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

    async def scenario() -> None:
        for update_id, name in enumerate(actions, start=100):
            update = make_callback_update(update_id=update_id, user_id=user_id, chat_type=chat_type)
            callback = update.callback_query
            assert callback is not None and isinstance(callback.message, Message)
            action = await store.create_card_action(name)
            await store.bind_card_actions(
                callback.message.chat.id, callback.message.message_id, [action.token]
            )
            await dispatcher.feed_update(
                bot,
                update.model_copy(
                    update={
                        "callback_query": callback.model_copy(update={"data": f"a:{action.token}"})
                    }
                ),
            )
            stored = await store.get_card_action(action.token)
            assert stored is not None and not stored.completed

    asyncio.run(scenario())
    assert session.requests == []


def test_dispatcher_confirms_a_bound_card_action_for_the_owner(
    client: tuple[Bot, RecordingSession], app: tuple[Dispatcher, Store]
) -> None:
    bot, session = client
    dispatcher, store = app

    async def prepare_card() -> str:
        source = await store.save_source(
            SourceInput(
                chat_id=OWNER_ID,
                message_id=30,
                text="шаблон",
                received_at=datetime.now(UTC),
            )
        )
        draft = await store.save_draft(
            source.id,
            EventData(title="Popping Jam", event_type="Джем", start_date=date(2026, 11, 14)),
            poster_source_id=None,
        )
        card = Message(
            message_id=30,
            date=datetime.now(UTC),
            chat=Chat(id=OWNER_ID, type="private"),
            from_user=make_user(OWNER_ID),
        ).as_(bot)
        await show_draft(card, draft, store)
        sent = next(request for request in session.requests if isinstance(request, SendMessage))
        assert sent.reply_markup is not None
        button = sent.reply_markup.inline_keyboard[0][0]
        assert button.callback_data is not None
        return button.callback_data

    callback_data = asyncio.run(prepare_card())
    update = Update(
        update_id=31,
        callback_query=CallbackQuery(
            id="confirm-card",
            from_user=make_user(OWNER_ID),
            chat_instance="test-chat",
            data=callback_data,
            message=Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=OWNER_ID, type="private"),
            ),
        ),
    )

    feed(dispatcher, bot, update)
    feed(dispatcher, bot, update.model_copy(update={"update_id": 32}))

    record = asyncio.run(store.get_record(1))
    assert record is not None and record.status == "active" and record.revision == 1
    assert any(isinstance(method, AnswerCallbackQuery) for method in session.requests)


def test_dispatcher_stale_confirm_button_does_not_confirm_newer_draft(
    client: tuple[Bot, RecordingSession], app: tuple[Dispatcher, Store]
) -> None:
    bot, session = client
    dispatcher, store = app

    async def prepare_draft() -> tuple[int, str]:
        source = await store.save_source(
            SourceInput(
                chat_id=OWNER_ID,
                message_id=40,
                text="первая версия",
                received_at=datetime.now(UTC),
            )
        )
        draft = await store.save_draft(
            source.id,
            EventData(title="Первая версия", event_type="Джем", start_date=date(2026, 11, 14)),
            poster_source_id=None,
        )
        card = Message(
            message_id=40,
            date=datetime.now(UTC),
            chat=Chat(id=OWNER_ID, type="private"),
            from_user=make_user(OWNER_ID),
        ).as_(bot)
        await show_draft(card, draft, store)
        first_sent = next(
            request for request in session.requests if isinstance(request, SendMessage)
        )
        assert first_sent.reply_markup is not None
        confirm_button = first_sent.reply_markup.inline_keyboard[0][0]
        assert confirm_button.callback_data is not None
        newer_source = await store.save_source(
            SourceInput(
                chat_id=OWNER_ID,
                message_id=41,
                text="вторая версия",
                received_at=datetime.now(UTC),
            )
        )
        newer = await store.save_draft(
            newer_source.id,
            EventData(title="Вторая версия", event_type="Джем", start_date=date(2026, 11, 21)),
            poster_source_id=None,
            record_id=draft.record_id,
        )
        assert newer.version == draft.version + 1
        return draft.record_id, confirm_button.callback_data

    record_id, callback_data = asyncio.run(prepare_draft())
    update = Update(
        update_id=42,
        callback_query=CallbackQuery(
            id="stale-confirm",
            from_user=make_user(OWNER_ID),
            chat_instance="test-chat",
            data=callback_data,
            message=Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=OWNER_ID, type="private"),
            ),
        ),
    )

    feed(dispatcher, bot, update)

    drafts = asyncio.run(store.list_drafts())
    pending = [draft for draft in drafts if draft.status == "pending"]
    assert len(pending) == 1
    assert pending[0].record_id == record_id
    assert pending[0].version == 2
    assert pending[0].data.title == "Вторая версия"
    assert asyncio.run(store.get_record(record_id)) is None


def test_edited_message_passes_access_barrier_but_is_not_saved(
    client: tuple[Bot, RecordingSession], app: tuple[Dispatcher, Store]
) -> None:
    bot, session = client
    dispatcher, store = app
    update = Update(
        update_id=22,
        edited_message=Message(
            message_id=22,
            date=datetime.now(UTC),
            edit_date=int(datetime.now(UTC).timestamp()),
            chat=Chat(id=OWNER_ID, type="private"),
            from_user=make_user(OWNER_ID),
            text="Название: изменённое сообщение",
        ),
    )

    feed(dispatcher, bot, update)

    assert asyncio.run(store.list_sources()) == []
    assert message_texts(session) == []


def test_callback_without_sender_is_rejected_by_aiogram_schema() -> None:
    with pytest.raises(ValidationError):
        Update.model_validate(
            {
                "update_id": 6,
                "callback_query": {
                    "id": "callback-6",
                    "chat_instance": "test-chat",
                    "data": "test-action",
                    "inline_message_id": "inline-message",
                },
            }
        )
