from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

from aiogram import Bot
from aiogram.methods import GetChat, SendMessage, SendPhoto, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from telegram_transport import RecordingSession

from dance_community_bot.bot import create_dispatcher
from dance_community_bot.config import Settings
from dance_community_bot.models import EventData, SourceInput
from dance_community_bot.storage import Store

TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
OWNER_ID = 111
GROUP_ID = -100123
NOW = datetime(2026, 11, 9, 9, tzinfo=UTC)


def make_settings(database_path: Path, group_id: int | None = GROUP_ID) -> Settings:
    return Settings(
        telegram_bot_token=TOKEN,
        telegram_owner_id=OWNER_ID,
        telegram_webhook_url="https://dance.example.org/telegram/webhook",
        telegram_webhook_secret="test_webhook_secret",
        database_path=database_path,
        telegram_group_id=group_id,
    )


def private_message(bot: Bot, message_id: int, text: str) -> Message:
    return Message(
        message_id=message_id,
        date=NOW,
        chat=Chat(id=OWNER_ID, type="private"),
        from_user=User(id=OWNER_ID, is_bot=False, first_name="Владелец"),
        text=text,
    ).as_(bot)


async def add_event(store: Store) -> None:
    source = await store.save_source(
        SourceInput(
            chat_id=OWNER_ID,
            message_id=1,
            text="Джэм",
            received_at=NOW,
        )
    )
    draft = await store.save_draft(
        source.id,
        EventData(title="Джэм", event_type="Джэм", start_date=date(2026, 11, 10)),
        poster_source_id=None,
    )
    await store.confirm_draft(draft.id, draft.version)


def _publish_confirmation(session: RecordingSession) -> SendMessage:
    return next(
        request
        for request in session.requests
        if isinstance(request, SendMessage)
        and request.reply_markup is not None
        and any(
            button.callback_data is not None and button.callback_data.startswith("pub:confirm:")
            for row in request.reply_markup.inline_keyboard
            for button in row
        )
    )


def _message_id(session: RecordingSession, request: SendMessage) -> int:
    messages = [item for item in session.requests if isinstance(item, (SendMessage, SendPhoto))]
    return 100 + messages.index(request) + 1


def _callback_update(
    bot: Bot,
    *,
    message_id: int,
    callback_data: str,
    user_id: int = OWNER_ID,
) -> Update:
    return Update(
        update_id=1000 + message_id,
        callback_query=CallbackQuery(
            id=f"publication-{message_id}",
            from_user=User(id=user_id, is_bot=False, first_name="Владелец"),
            chat_instance="publication-test",
            message=Message(
                message_id=message_id,
                date=NOW,
                chat=Chat(id=OWNER_ID, type="private"),
                text="Предпросмотр публикации",
            ).as_(bot),
            data=callback_data,
        ),
    )


def test_publish_requires_confirmation_and_binds_callback_to_its_message(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_event(store)
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=private_message(bot, 50, "/publish 10.11.2026"),
                ),
            )

            group_sends = [
                request
                for request in session.requests
                if isinstance(request, (SendMessage, SendPhoto)) and request.chat_id == GROUP_ID
            ]
            confirmation = _publish_confirmation(session)
            button = next(
                button
                for row in confirmation.reply_markup.inline_keyboard
                for button in row
                if button.callback_data is not None
                and button.callback_data.startswith("pub:confirm:")
            )
            assert group_sends == []
            assert button.callback_data is not None
            assert button.callback_data.split(":")[-1] == "2"

            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, confirmation),
                    callback_data=button.callback_data,
                ),
            )

            publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
            assert publication is not None
            assert publication.status == "delivered"
            assert publication.owner_message_id != _message_id(session, confirmation)
            group_sends = [
                request
                for request in session.requests
                if isinstance(request, (SendMessage, SendPhoto)) and request.chat_id == GROUP_ID
            ]
            assert len(group_sends) == 1
            assert any(isinstance(request, GetChat) for request in session.requests)
            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, confirmation),
                    callback_data=button.callback_data,
                ),
            )
            assert (
                len(
                    [
                        request
                        for request in session.requests
                        if isinstance(request, (SendMessage, SendPhoto))
                        and request.chat_id == GROUP_ID
                    ]
                )
                == 1
            )
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_old_confirmation_cannot_send_to_reconfigured_group(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_event(store)
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=private_message(bot, 50, "/publish 10.11.2026"),
                ),
            )
            confirmation = _publish_confirmation(session)
            button = next(
                button
                for row in confirmation.reply_markup.inline_keyboard
                for button in row
                if button.callback_data is not None
                and button.callback_data.startswith("pub:confirm:")
            )
            assert button.callback_data is not None

            reconfigured = create_dispatcher(make_settings(store.path, GROUP_ID - 1), store)
            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, confirmation),
                    callback_data=button.callback_data,
                    user_id=OWNER_ID + 1,
                ),
            )
            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, confirmation) + 1,
                    callback_data=button.callback_data,
                ),
            )
            await reconfigured.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, confirmation),
                    callback_data=button.callback_data,
                ),
            )

            assert not any(
                isinstance(request, (SendMessage, SendPhoto))
                and request.chat_id in (GROUP_ID, GROUP_ID - 1)
                for request in session.requests
            )
            publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
            assert publication is not None
            assert publication.status == "prepared"
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_publish_empty_week_only_notifies_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=private_message(bot, 50, "/publish 17.11.2026"),
                ),
            )

            assert not any(isinstance(request, GetChat) for request in session.requests)
            assert not any(
                isinstance(request, (SendMessage, SendPhoto)) and request.chat_id == GROUP_ID
                for request in session.requests
            )
            assert session.messages == ["На эту неделю записей нет."]
            assert await store.get_publication_for_week(GROUP_ID, date(2026, 11, 16)) is None
        finally:
            await bot.session.close()

    asyncio.run(scenario())


class LoseGroupPhotoResponsesSession(RecordingSession):
    def __init__(self) -> None:
        super().__init__(OWNER_ID)
        self.group_photo_attempts = 0

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        result = await super().make_request(bot, method, timeout)
        if isinstance(method, SendPhoto) and method.chat_id == GROUP_ID:
            self.group_photo_attempts += 1
            if self.group_photo_attempts <= 2:
                raise TimeoutError("possible group delivery")
        return result


def test_unknown_photo_controls_retry_only_once_and_reject_stale_or_outsider_callbacks(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(
                chat_id=OWNER_ID,
                message_id=3,
                text="Афиша",
                received_at=NOW,
                telegram_file_id="telegram-file-id",
                is_photo=True,
            )
        )
        poster_path = Path("posters/3.jpg")
        (store.path.parent / poster_path).parent.mkdir(parents=True, exist_ok=True)
        (store.path.parent / poster_path).write_bytes(b"poster")
        await store.set_source_poster_path(source.id, poster_path)
        draft = await store.save_draft(
            source.id,
            EventData(
                title="Афишный джэм",
                event_type="Джэм",
                start_date=date(2026, 11, 10),
            ),
            poster_source_id=source.id,
        )
        await store.confirm_draft(draft.id, draft.version)
        session = LoseGroupPhotoResponsesSession()
        bot = Bot(token=TOKEN, session=session)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=private_message(bot, 50, "/publish 10.11.2026"),
                ),
            )
            confirmation = _publish_confirmation(session)
            confirm_button = next(
                button
                for row in confirmation.reply_markup.inline_keyboard
                for button in row
                if button.callback_data is not None
                and button.callback_data.startswith("pub:confirm:")
            )
            assert confirm_button.callback_data is not None
            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, confirmation),
                    callback_data=confirm_button.callback_data,
                ),
            )

            first_unknown_controls = next(
                request
                for request in reversed(session.requests)
                if isinstance(request, SendMessage)
                and request.reply_markup is not None
                and any(
                    button.callback_data is not None
                    and button.callback_data.startswith("pub:resolve:")
                    for row in request.reply_markup.inline_keyboard
                    for button in row
                )
            )
            repeat_button = next(
                button
                for row in first_unknown_controls.reply_markup.inline_keyboard
                for button in row
                if button.callback_data is not None
                and button.callback_data.startswith("pub:resolve:")
                and button.callback_data.split(":")[4] == "0"
            )
            assert repeat_button.callback_data is not None
            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, first_unknown_controls),
                    callback_data=repeat_button.callback_data,
                ),
            )
            assert session.group_photo_attempts == 2

            latest_unknown_controls = next(
                request
                for request in reversed(session.requests)
                if isinstance(request, SendMessage)
                and request.reply_markup is not None
                and any(
                    button.callback_data is not None
                    and button.callback_data.startswith("pub:resolve:")
                    for row in request.reply_markup.inline_keyboard
                    for button in row
                )
            )
            latest_button = next(
                button
                for row in latest_unknown_controls.reply_markup.inline_keyboard
                for button in row
                if button.callback_data is not None
                and button.callback_data.startswith("pub:resolve:")
                and button.callback_data.split(":")[4] == "0"
            )
            assert latest_button.callback_data is not None
            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, latest_unknown_controls),
                    callback_data=latest_button.callback_data,
                    user_id=OWNER_ID + 1,
                ),
            )
            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, first_unknown_controls),
                    callback_data=repeat_button.callback_data,
                ),
            )

            assert session.group_photo_attempts == 2
            publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
            assert publication is not None
            unknown = next(part for part in publication.parts if part.status == "unknown")
            assert unknown.attempt_revision == 2
        finally:
            await bot.session.close()

    asyncio.run(scenario())
