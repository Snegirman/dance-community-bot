from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from pydantic import ValidationError

from dance_community_bot.access import is_owner_private
from dance_community_bot.bot import create_dispatcher

OWNER_ID = 222
TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[SendMessage] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        request = method
        if not isinstance(request, SendMessage):
            raise AssertionError(f"Unexpected Telegram method: {request.__class__.__name__}")
        if not isinstance(request.chat_id, int):
            raise AssertionError("The test sends messages only to numeric chats")
        self.requests.append(request)
        response = self.check_response(
            bot=bot,
            method=method,
            status_code=200,
            content=self.json_dumps(
                {
                    "ok": True,
                    "result": {
                        "message_id": 1,
                        "date": 1793782800,
                        "chat": {"id": request.chat_id, "type": "private"},
                        "text": request.text,
                    },
                }
            ),
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
    return [request.text for request in session.requests if isinstance(request, SendMessage)]


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
    client: tuple[Bot, RecordingSession], command: str
) -> None:
    bot, session = client
    dispatcher = create_dispatcher(OWNER_ID)

    feed(dispatcher, bot, make_message_update(update_id=1, text=command))

    assert len(message_texts(session)) == 1
    assert "ещё не реализ" in message_texts(session)[0].lower()


def test_owner_material_is_rejected_without_claiming_it_was_saved(
    client: tuple[Bot, RecordingSession],
) -> None:
    bot, session = client
    dispatcher = create_dispatcher(OWNER_ID)

    feed(dispatcher, bot, make_message_update(update_id=2, text="Занятие в пятницу"))

    assert "не сохран" in message_texts(session)[0].lower()


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
@pytest.mark.parametrize("text", ["материал", "/help", "/start"])
def test_disallowed_messages_are_ignored(
    client: tuple[Bot, RecordingSession], user_id: int | None, chat_type: str, text: str
) -> None:
    bot, session = client
    dispatcher = create_dispatcher(OWNER_ID)

    feed(
        dispatcher,
        bot,
        make_message_update(update_id=3, user_id=user_id, chat_type=chat_type, text=text),
    )

    assert message_texts(session) == []


@pytest.mark.parametrize(
    ("user_id", "chat_type"),
    [
        (OWNER_ID + 1, "private"),
        (OWNER_ID, "group"),
        (OWNER_ID, None),
    ],
)
def test_disallowed_callbacks_do_not_reach_handlers(
    client: tuple[Bot, RecordingSession], user_id: int, chat_type: str | None
) -> None:
    bot, _ = client
    dispatcher = create_dispatcher(OWNER_ID)
    handled: list[str] = []

    async def handler(callback: CallbackQuery) -> None:
        handled.append(callback.data or "")

    dispatcher.callback_query.register(handler)
    feed(dispatcher, bot, make_callback_update(update_id=4, user_id=user_id, chat_type=chat_type))

    assert handled == []


def test_owner_private_callback_reaches_handlers_after_the_access_barrier(
    client: tuple[Bot, RecordingSession],
) -> None:
    bot, _ = client
    dispatcher = create_dispatcher(OWNER_ID)
    handled: list[str] = []

    async def handler(callback: CallbackQuery) -> None:
        handled.append(callback.data or "")

    dispatcher.callback_query.register(handler)
    feed(dispatcher, bot, make_callback_update(update_id=5))

    assert handled == ["test-action"]


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
