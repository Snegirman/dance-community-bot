from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, tzinfo
from html import escape
from pathlib import Path
from typing import Literal

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from telegram_transport import RecordingSession

from dance_community_bot import publication_handlers
from dance_community_bot import publishing as publishing_module
from dance_community_bot.bot import create_dispatcher
from dance_community_bot.config import Settings
from dance_community_bot.models import EventData, PhotoPostPart, Record, SourceInput, TextPostPart
from dance_community_bot.publishing import publish_week
from dance_community_bot.storage import Store
from dance_community_bot.weekly import build_weekly_post

TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
OWNER_ID = 111
GROUP_ID = -100123
NOW = datetime(2026, 11, 13, 9, tzinfo=UTC)
WEEK_START = date(2026, 11, 9)


def make_settings(database_path: Path, *, group_id: int | None = GROUP_ID) -> Settings:
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


def _message_id(session: RecordingSession, request: SendMessage | SendPhoto) -> int:
    messages = [item for item in session.requests if isinstance(item, (SendMessage, SendPhoto))]
    return 100 + messages.index(request) + 1


def _callback_update(bot: Bot, *, message_id: int, callback_data: str) -> Update:
    return Update(
        update_id=1000 + message_id,
        callback_query=CallbackQuery(
            id=f"republish-{message_id}",
            from_user=User(id=OWNER_ID, is_bot=False, first_name="Владелец"),
            chat_instance="republish-test",
            message=Message(
                message_id=message_id,
                date=NOW,
                chat=Chat(id=OWNER_ID, type="private"),
                text="Предпросмотр повтора",
            ).as_(bot),
            data=callback_data,
        ),
    )


def _republish_confirmation(
    session: RecordingSession,
) -> tuple[SendMessage, str] | None:
    confirmations: list[tuple[SendMessage, str]] = []
    for request in session.requests:
        if not isinstance(request, SendMessage) or request.reply_markup is None:
            continue
        for row in request.reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data is not None and button.callback_data.startswith(
                    "pub:republish:"
                ):
                    confirmations.append((request, button.callback_data))
    return confirmations[-1] if confirmations else None


async def add_photo_event(store: Store) -> Record:
    source = await store.save_source(
        SourceInput(
            chat_id=OWNER_ID,
            message_id=1,
            text="Исходная афиша",
            received_at=NOW,
            telegram_file_id="original-photo-file-id",
            is_photo=True,
        )
    )
    poster_path = Path("posters/original.jpg")
    (store.path.parent / poster_path).parent.mkdir(parents=True, exist_ok=True)
    (store.path.parent / poster_path).write_bytes(b"original poster")
    await store.set_source_poster_path(source.id, poster_path)
    draft = await store.save_draft(
        source.id,
        EventData(
            title="Исходный джэм",
            event_type="Джэм",
            start_date=date(2026, 11, 10),
        ),
        poster_source_id=source.id,
    )
    return await store.confirm_draft(draft.id, draft.version)


async def edit_photo_event(
    store: Store,
    record: Record,
    *,
    message_id: int = 2,
    title: str = "Обновлённый джэм",
    file_id: str = "updated-photo-file-id",
) -> Record:
    source = await store.save_source(
        SourceInput(
            chat_id=OWNER_ID,
            message_id=message_id,
            text="Обновлённая афиша",
            received_at=NOW,
            telegram_file_id=file_id,
            is_photo=True,
        )
    )
    poster_path = Path(f"posters/{message_id}.jpg")
    (store.path.parent / poster_path).write_bytes(f"poster {message_id}".encode())
    await store.set_source_poster_path(source.id, poster_path)
    draft = await store.save_draft(
        source.id,
        EventData(
            title=title,
            event_type="Джэм",
            start_date=date(2026, 11, 10),
        ),
        poster_source_id=source.id,
        record_id=record.id,
        base_revision=record.revision,
    )
    return await store.confirm_draft(draft.id, draft.version)


async def add_text_event(
    store: Store,
    *,
    message_id: int,
    title: str,
    event_date: date,
) -> Record:
    source = await store.save_source(
        SourceInput(
            chat_id=OWNER_ID,
            message_id=message_id,
            text=title,
            received_at=NOW,
        )
    )
    draft = await store.save_draft(
        source.id,
        EventData(title=title, event_type="Джэм", start_date=event_date),
        poster_source_id=None,
    )
    return await store.confirm_draft(draft.id, draft.version)


def test_republish_sends_updated_catalog_after_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(publication_handlers, "datetime", FrozenDatetime)

    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        record = await add_photo_event(store)
        original_post = await build_weekly_post(WEEK_START, store)
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        original_result = await publish_week(original_post, GROUP_ID, store, bot)
        assert original_result.status == "delivered"
        original = await store.get_publication_for_week(GROUP_ID, WEEK_START)
        assert original is not None
        original_parts = original.parts
        await edit_photo_event(store, record)

        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 50, "/republish")),
            )

            updated_post = await build_weekly_post(WEEK_START, store)
            assert updated_post != original_post
            confirmation = _republish_confirmation(session)
            assert confirmation is not None, "Для повтора не показана кнопка подтверждения"
            confirmation_message, callback_data = confirmation
            private_content = "\n".join(
                request.text if isinstance(request, SendMessage) else request.caption or ""
                for request in session.requests
                if isinstance(request, SendMessage | SendPhoto) and request.chat_id == OWNER_ID
            )
            assert "Обновлённый джэм" in private_content
            assert "Исходный джэм" not in private_content
            assert "повтор" in confirmation_message.text.casefold()
            assert sum(
                isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
                for request in session.requests
            ) == len(original_post.parts)

            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, confirmation_message),
                    callback_data=callback_data,
                ),
            )

            group_messages = [
                request
                for request in session.requests
                if isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
            ]
            repeated_messages = group_messages[len(original_post.parts) :]
            assert len(repeated_messages) == len(updated_post.parts)
            for part, request in zip(updated_post.parts, repeated_messages, strict=True):
                if isinstance(part, TextPostPart):
                    assert isinstance(request, SendMessage)
                    assert request.text == escape(part.text)
                else:
                    assert isinstance(part, PhotoPostPart)
                    assert isinstance(request, SendPhoto)
                    source = await store.get_source_by_id(part.poster_source_id)
                    assert source is not None
                    assert request.photo == source.telegram_file_id
                    assert request.caption == escape(part.caption)
                    assert "Обновлённый джэм" in request.caption

            original = await store.get_publication_for_week(GROUP_ID, WEEK_START)
            assert original is not None
            assert original.snapshot == original_post
            assert original.status == "delivered"
            assert original.parts == original_parts
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_republish_refreshes_same_preview_when_catalog_changes_before_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(publication_handlers, "datetime", FrozenDatetime)

    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        record = await add_photo_event(store)
        original_post = await build_weekly_post(WEEK_START, store)
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        original_result = await publish_week(original_post, GROUP_ID, store, bot)
        assert original_result.status == "delivered"
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 50, "/republish")),
            )
            first_confirmation = _republish_confirmation(session)
            assert first_confirmation is not None
            first_message, first_callback = first_confirmation
            first_repeat = await store.get_republication_for_request(OWNER_ID, 50)
            assert first_repeat is not None

            await edit_photo_event(
                store,
                record,
                message_id=3,
                title="Самый новый джэм",
                file_id="newest-photo-file-id",
            )
            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, first_message),
                    callback_data=first_callback,
                ),
            )

            latest_confirmation = _republish_confirmation(session)
            assert latest_confirmation is not None
            latest_message, latest_callback = latest_confirmation
            assert latest_callback != first_callback
            preview_text = "\n".join(
                request.text if isinstance(request, SendMessage) else request.caption or ""
                for request in session.requests
                if isinstance(request, SendMessage | SendPhoto) and request.chat_id == OWNER_ID
            )
            assert "Самый новый джэм" in preview_text
            assert sum(
                isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
                for request in session.requests
            ) == len(original_post.parts)

            refreshed_repeat = await store.get_republication_for_request(OWNER_ID, 50)
            assert refreshed_repeat is not None
            assert refreshed_repeat.id == first_repeat.id
            assert refreshed_repeat.snapshot != first_repeat.snapshot
            assert refreshed_repeat.preview_revision > first_repeat.preview_revision
            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, first_message),
                    callback_data=first_callback,
                ),
            )
            assert sum(
                isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
                for request in session.requests
            ) == len(original_post.parts)

            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, latest_message),
                    callback_data=latest_callback,
                ),
            )
            group_photo = [
                request
                for request in session.requests
                if isinstance(request, SendPhoto) and request.chat_id == GROUP_ID
            ]
            assert group_photo[-1].photo == "newest-photo-file-id"
            assert "Самый новый джэм" in (group_photo[-1].caption or "")
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_republish_request_binding_survives_week_boundary_and_new_requests_can_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenDatetime(datetime):
        current = NOW

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return (
                cls.current.astimezone(tz) if tz is not None else cls.current.replace(tzinfo=None)
            )

    monkeypatch.setattr(publication_handlers, "datetime", FrozenDatetime)

    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(chat_id=OWNER_ID, message_id=1, text="Событие", received_at=NOW)
        )
        draft = await store.save_draft(
            source.id,
            EventData(title="Текущий джэм", event_type="Джэм", start_date=date(2026, 11, 10)),
            poster_source_id=None,
        )
        await store.confirm_draft(draft.id, draft.version)
        original_post = await build_weekly_post(WEEK_START, store)
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        original_result = await publish_week(original_post, GROUP_ID, store, bot)
        assert original_result.status == "delivered"
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:

            async def start_and_confirm(update_id: int, request_id: int) -> int:
                await dispatcher.feed_update(
                    bot,
                    Update(
                        update_id=update_id,
                        message=private_message(bot, request_id, "/republish"),
                    ),
                )
                confirmation = _republish_confirmation(session)
                assert confirmation is not None
                confirmation_message, callback_data = confirmation
                publication = await store.get_republication_for_request(OWNER_ID, request_id)
                assert publication is not None
                await dispatcher.feed_update(
                    bot,
                    _callback_update(
                        bot,
                        message_id=_message_id(session, confirmation_message),
                        callback_data=callback_data,
                    ),
                )
                completed = await store.get_republication_for_request(OWNER_ID, request_id)
                assert completed is not None and completed.status == "delivered"
                return publication.id

            first_repeat_id = await start_and_confirm(1, 50)
            sends_after_first_repeat = sum(
                isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
                for request in session.requests
            )

            await dispatcher.feed_update(
                bot,
                Update(update_id=2, message=private_message(bot, 50, "/republish")),
            )
            same_request = await store.get_republication_for_request(OWNER_ID, 50)
            assert same_request is not None and same_request.id == first_repeat_id
            assert same_request.status == "delivered"
            assert (
                sum(
                    isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
                    for request in session.requests
                )
                == sends_after_first_repeat
            )

            second_repeat_id = await start_and_confirm(3, 51)
            assert second_repeat_id != first_repeat_id
            FrozenDatetime.current = datetime(2026, 11, 15, 21, 0, tzinfo=UTC)
            await dispatcher.feed_update(
                bot,
                Update(update_id=4, message=private_message(bot, 50, "/republish")),
            )
            after_boundary = await store.get_republication_for_request(OWNER_ID, 50)
            assert after_boundary is not None and after_boundary.id == first_repeat_id
            assert after_boundary.week_start == WEEK_START
            await dispatcher.feed_update(
                bot,
                Update(update_id=5, message=private_message(bot, 52, "/republish")),
            )
            assert await store.get_publication_for_week(GROUP_ID, date(2026, 11, 16)) is None
            assert await store.get_republication_for_request(OWNER_ID, 52) is None
            assert sum(
                isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
                for request in session.requests
            ) == 3 * len(original_post.parts)
        finally:
            await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["missing_group", "missing_original", "prepared_original"])
def test_republish_requires_group_and_delivered_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(publication_handlers, "datetime", FrozenDatetime)

    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_text_event(
            store,
            message_id=1,
            title="Событие недели",
            event_date=date(2026, 11, 10),
        )
        post = await build_weekly_post(WEEK_START, store)
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        if case == "missing_original":
            original = None
        elif case == "prepared_original":
            prepared = await store.prepare_publication(post, GROUP_ID, OWNER_ID, 40)
            original = prepared.publication
            assert original is not None and original.status == "prepared"
        else:
            result = await publish_week(post, GROUP_ID, store, bot)
            assert result.status == "delivered"
            original = await store.get_publication_for_week(GROUP_ID, WEEK_START)
            assert original is not None and original.status == "delivered"

        group_id = None if case == "missing_group" else GROUP_ID
        dispatcher = create_dispatcher(make_settings(store.path, group_id=group_id), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 50, "/republish")),
            )
            assert await store.get_republication_for_request(OWNER_ID, 50) is None
            if case == "missing_group":
                assert any(
                    isinstance(request, SendMessage)
                    and request.chat_id == OWNER_ID
                    and "TELEGRAM_GROUP_ID" in request.text
                    for request in session.requests
                )
            elif case == "missing_original":
                assert original is None
                assert await store.get_publication_for_week(GROUP_ID, WEEK_START) is None
            else:
                current_original = await store.get_publication_for_week(GROUP_ID, WEEK_START)
                assert current_original is not None and current_original.status == "prepared"
                assert current_original.id == original.id
            assert (
                not any(
                    isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
                    for request in session.requests
                )
                or case == "missing_group"
            )
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_republish_rejects_outsider_group_chat_and_changed_target_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(publication_handlers, "datetime", FrozenDatetime)

    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_text_event(
            store,
            message_id=1,
            title="Событие недели",
            event_date=date(2026, 11, 10),
        )
        post = await build_weekly_post(WEEK_START, store)
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        original = await publish_week(post, GROUP_ID, store, bot)
        assert original.status == "delivered"
        dispatcher = create_dispatcher(make_settings(store.path), store)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 50, "/republish")),
            )
            confirmation = _republish_confirmation(session)
            assert confirmation is not None
            control_message, callback_data = confirmation
            prepared = await store.get_republication_for_request(OWNER_ID, 50)
            assert prepared is not None and prepared.status == "prepared"

            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=2,
                    message=Message(
                        message_id=51,
                        date=NOW,
                        chat=Chat(id=OWNER_ID + 1, type="private"),
                        from_user=User(id=OWNER_ID + 1, is_bot=False, first_name="Гость"),
                        text="/republish",
                    ).as_(bot),
                ),
            )
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=3,
                    message=Message(
                        message_id=52,
                        date=NOW,
                        chat=Chat(id=GROUP_ID, type="supergroup"),
                        from_user=User(id=OWNER_ID, is_bot=False, first_name="Владелец"),
                        text="/republish",
                    ).as_(bot),
                ),
            )
            assert await store.get_republication_for_request(OWNER_ID + 1, 51) is None
            assert await store.get_republication_for_request(GROUP_ID, 52) is None

            changed_target_dispatcher = create_dispatcher(
                make_settings(store.path, group_id=GROUP_ID - 1), store
            )
            await changed_target_dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, control_message),
                    callback_data=callback_data,
                ),
            )
            unchanged = await store.get_republication_for_request(OWNER_ID, 50)
            assert unchanged is not None and unchanged.id == prepared.id
            assert unchanged.status == "prepared"
            assert sum(
                isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
                for request in session.requests
            ) == len(post.parts)
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_new_republish_request_recovers_pending_repeat_when_catalog_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(publication_handlers, "datetime", FrozenDatetime)

    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        poster_record = await add_photo_event(store)
        text_record = await add_text_event(
            store,
            message_id=4,
            title="Вторая афиша без картинки",
            event_date=date(2026, 11, 11),
        )
        original_post = await build_weekly_post(WEEK_START, store)
        assert len(original_post.parts) >= 3
        photo_index = next(
            index
            for index, part in enumerate(original_post.parts)
            if isinstance(part, PhotoPostPart)
        )
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        original_result = await publish_week(original_post, GROUP_ID, store, bot)
        assert original_result.status == "delivered"
        assert original_result.publication_id is not None

        original_before_repeat = await store.get_publication(original_result.publication_id)
        assert original_before_repeat is not None
        original_send_part = publishing_module._send_part
        lose_photo_response = True

        async def send_with_lost_photo_response(
            send_bot: Bot,
            group_id: int,
            part: TextPostPart | PhotoPostPart,
            prepared_photo: publishing_module.PreparedPhoto | None = None,
        ) -> tuple[Literal["sent", "failed", "unknown"], int | None]:
            nonlocal lose_photo_response
            outcome, message_id = await original_send_part(send_bot, group_id, part, prepared_photo)
            if lose_photo_response and isinstance(part, PhotoPostPart) and outcome == "sent":
                lose_photo_response = False
                return "unknown", None
            return outcome, message_id

        monkeypatch.setattr(publishing_module, "_send_part", send_with_lost_photo_response)
        dispatcher = create_dispatcher(make_settings(store.path), store)
        await dispatcher.feed_update(
            bot,
            Update(update_id=1, message=private_message(bot, 50, "/republish")),
        )
        confirmation = _republish_confirmation(session)
        assert confirmation is not None
        confirmation_message, callback_data = confirmation
        await dispatcher.feed_update(
            bot,
            _callback_update(
                bot,
                message_id=_message_id(session, confirmation_message),
                callback_data=callback_data,
            ),
        )
        original_after_repeat = await store.get_publication_for_week(GROUP_ID, WEEK_START)
        repeat = await store.get_republication_for_request(OWNER_ID, 50)
        assert original_after_repeat is not None
        assert repeat is not None and repeat.status == "unknown"
        assert original_after_repeat.parts == original_before_repeat.parts
        assert [part.status for part in repeat.parts] == [
            "sent" if index < photo_index else "unknown" if index == photo_index else "prepared"
            for index in range(len(repeat.parts))
        ]
        await store.cancel_record(poster_record.id, poster_record.revision)
        await store.cancel_record(text_record.id, text_record.revision)
        empty_post = await build_weekly_post(WEEK_START, store)
        assert not empty_post.records and not empty_post.occurrences

        reopened = Store(store.path)
        await reopened.initialize()
        await reopened.recover_publications_on_startup()
        dispatcher = create_dispatcher(make_settings(reopened.path), reopened)
        try:
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 60, "/republish")),
            )
            recovered = await reopened.get_republication_for_request(OWNER_ID, 60)
            assert recovered is not None and recovered.id == repeat.id
            assert recovered.status == "unknown"
            recovery_text = "\n".join(
                request.text
                for request in session.requests
                if isinstance(request, SendMessage) and request.chat_id == OWNER_ID
            )
            assert "доставка неизвестна" in recovery_text
            control_message = next(
                request
                for request in reversed(session.requests)
                if isinstance(request, SendMessage)
                and request.chat_id == OWNER_ID
                and request.reply_markup is not None
                and any(
                    button.callback_data is not None
                    and button.callback_data.startswith("pub:resolve:")
                    for row in request.reply_markup.inline_keyboard
                    for button in row
                )
            )
            resolved_button = next(
                button
                for row in control_message.reply_markup.inline_keyboard
                for button in row
                if button.text.endswith("доставлено")
            )
            assert resolved_button.callback_data is not None
            sent_before_recovery = sum(
                isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
                for request in session.requests
            )
            assert sent_before_recovery == len(original_post.parts) + photo_index + 1
            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, control_message),
                    callback_data=resolved_button.callback_data,
                ),
            )

            continue_message = next(
                request
                for request in reversed(session.requests)
                if isinstance(request, SendMessage)
                and request.chat_id == OWNER_ID
                and request.reply_markup is not None
                and any(
                    button.callback_data is not None
                    and button.callback_data.startswith("pub:continue:")
                    for row in request.reply_markup.inline_keyboard
                    for button in row
                )
            )
            continue_button = next(
                button
                for row in continue_message.reply_markup.inline_keyboard
                for button in row
                if button.callback_data is not None
                and button.callback_data.startswith("pub:continue:")
            )
            await dispatcher.feed_update(
                bot,
                _callback_update(
                    bot,
                    message_id=_message_id(session, continue_message),
                    callback_data=continue_button.callback_data,
                ),
            )
            completed = await reopened.get_republication_for_request(OWNER_ID, 60)
            assert completed is not None and completed.id == repeat.id
            assert completed.status == "delivered"
            assert completed.parts[0].message_id is not None
            assert sum(
                isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
                for request in session.requests
            ) == 2 * len(original_post.parts)

            await dispatcher.feed_update(
                bot,
                Update(update_id=2, message=private_message(bot, 60, "/republish")),
            )
            redelivered = await reopened.get_republication_for_request(OWNER_ID, 60)
            assert redelivered is not None and redelivered.id == repeat.id
            assert redelivered.status == "delivered"
            await dispatcher.feed_update(
                bot,
                Update(update_id=3, message=private_message(bot, 61, "/republish")),
            )
            assert await reopened.get_republication_for_request(OWNER_ID, 61) is None
            assert sum(
                isinstance(request, SendMessage | SendPhoto) and request.chat_id == GROUP_ID
                for request in session.requests
            ) == 2 * len(original_post.parts)
        finally:
            await bot.session.close()

    asyncio.run(scenario())
