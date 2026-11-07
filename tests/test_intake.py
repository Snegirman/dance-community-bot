from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Document, Message, PhotoSize, User
from telegram_transport import RecordingSession

from dance_community_bot.cards import show_record
from dance_community_bot.intake import handle_card_action, ingest_message
from dance_community_bot.models import ClassData, EventData, Source
from dance_community_bot.storage import Store

TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
CHAT_ID = 222
EVENT_TEMPLATE = "Название: Popping Jam\nДата: 14.11.2026\nТип: Джем"
CLASS_TEMPLATE = "Название: Popping\nПервая дата: 10.11.2026\nВремя: 19:00"


def make_message(
    bot: Bot,
    *,
    text: str | None = None,
    photo: bool = False,
    message_id: int = 17,
    media_group_id: str | None = None,
    document: bool = False,
    reply_to: Message | None = None,
    edited: bool = False,
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        text=text if not photo else None,
        caption=text if photo else None,
        photo=(
            [
                PhotoSize(
                    file_id="poster-file-id", file_unique_id="poster-unique-id", width=20, height=20
                )
            ]
            if photo
            else None
        ),
        document=(
            Document(
                file_id="document-file-id",
                file_unique_id="document-unique-id",
                file_name="poster.pdf",
                mime_type="application/pdf",
                file_size=100,
            )
            if document
            else None
        ),
        media_group_id=media_group_id,
        reply_to_message=reply_to,
        edit_date=int(datetime(2026, 11, 7, 12, 1, tzinfo=UTC).timestamp()) if edited else None,
    ).as_(bot)


def card_callback(bot: Bot, token: str, message_id: int) -> CallbackQuery:
    return CallbackQuery(
        id=f"callback-{message_id}-{token[:6]}",
        from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        chat_instance="test-chat",
        data=f"a:{token}",
        message=Message(
            message_id=message_id,
            date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
        ).as_(bot),
    ).as_(bot)


async def token_for_button(store: Store, message: Message, label: str) -> str:
    if message.reply_markup is None:
        raise AssertionError("Card action keyboard is missing")
    for row in message.reply_markup.inline_keyboard:
        for button in row:
            if button.text != label or button.callback_data is None:
                continue
            action = await store.get_card_action(button.callback_data[2:])
            assert action is not None
            return action.token
    raise AssertionError(f"Button {label!r} was not sent")


def test_text_template_is_saved_before_card_is_shown(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()

        async def check_saved_before_send() -> bool:
            return await store.get_source(CHAT_ID, 17) is not None and bool(
                await store.list_drafts()
            )

        session.before_send_check = check_saved_before_send

        await ingest_message(make_message(bot, text=EVENT_TEMPLATE), store)

        source = await store.get_source(CHAT_ID, 17)
        drafts = await store.list_drafts()
        assert source is not None
        assert source.text == EVENT_TEMPLATE
        assert source.status == "processed"
        assert len(drafts) == 1
        assert drafts[0].status == "pending"
        assert drafts[0].data.title == "Popping Jam"
        assert await store.list_records() == []
        assert "Popping Jam" in "\n".join(session.messages)

        await bot.session.close()

    asyncio.run(scenario())


def test_send_message_requires_successful_pre_send_check() -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        check_calls = 0

        async def check_before_send() -> bool:
            nonlocal check_calls
            check_calls += 1
            return check_calls == 2

        session.before_send_check = check_before_send

        with pytest.raises(AssertionError):
            await bot.send_message(CHAT_ID, "Первое сообщение")

        assert session.requests == []
        assert session.messages == []

        message = await bot.send_message(CHAT_ID, "Повторная отправка")

        assert check_calls == 2
        assert len(session.requests) == 1
        assert isinstance(session.requests[0], SendMessage)
        assert session.messages == ["Повторная отправка"]
        assert message.text == "Повторная отправка"
        await bot.session.close()

    asyncio.run(scenario())


def test_invalid_template_keeps_source_without_creating_record(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()

        await ingest_message(make_message(bot, text="Занятие в пятницу"), store)

        source = await store.get_source(CHAT_ID, 17)
        assert source is not None
        assert source.status == "failed"
        assert "сохран" in (source.error_reason or "").lower()
        assert await store.list_drafts() == []
        assert await store.list_records() == []
        await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("template", "class_template"),
    [(EVENT_TEMPLATE, False), (CLASS_TEMPLATE, True)],
)
def test_date_marker_selects_event_or_class_template(
    tmp_path: Path, template: str, class_template: bool
) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()

        await ingest_message(make_message(bot, text=template), store)

        drafts = await store.list_drafts()
        assert len(drafts) == 1
        assert isinstance(drafts[0].data, ClassData) is class_template
        assert isinstance(drafts[0].data, EventData) is not class_template
        await bot.session.close()

    asyncio.run(scenario())


def test_conflicting_date_markers_and_reply_type_change_are_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        conflicting = "Название: Ошибка\nДата: 14.11.2026\nПервая дата: 15.11.2026\nТип: Джем"

        await ingest_message(make_message(bot, text=conflicting), store)

        conflict_source = await store.get_source(CHAT_ID, 17)
        assert conflict_source is not None and conflict_source.status == "failed"
        assert await store.list_drafts() == []
        await ingest_message(make_message(bot, text=EVENT_TEMPLATE, message_id=18), store)
        bound_card_id = 102
        binding = await store.get_card_binding(CHAT_ID, bound_card_id)
        assert binding is not None and binding.draft_id is not None
        replied_card = Message(
            message_id=bound_card_id,
            date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
        )

        await ingest_message(
            make_message(bot, text=CLASS_TEMPLATE, message_id=19, reply_to=replied_card), store
        )

        source = await store.get_source(CHAT_ID, 19)
        pending = [draft for draft in await store.list_drafts() if draft.status == "pending"]
        assert source is not None and source.status == "failed"
        assert len(pending) == 1
        assert isinstance(pending[0].data, EventData)
        assert pending[0].data.title == "Popping Jam"
        await bot.session.close()

    asyncio.run(scenario())


def test_photo_without_template_is_saved_as_visible_source_only(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()

        await ingest_message(make_message(bot, photo=True), store)

        source = await store.get_source(CHAT_ID, 17)
        assert source is not None
        assert source.is_photo
        assert source.status == "failed"
        assert source.attachment_status == "saved"
        assert source.poster_path is not None
        assert await store.list_drafts() == []
        assert await store.list_records() == []
        get_file_requests = [
            request for request in session.requests if request.__class__.__name__ == "GetFile"
        ]
        assert len(get_file_requests) == 1
        await bot.session.close()

    asyncio.run(scenario())


def test_album_and_document_are_saved_but_not_downloaded_as_posters(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)

        await ingest_message(make_message(bot, photo=True, media_group_id="album-1"), store)
        album = await store.get_source(CHAT_ID, 17)
        assert album is not None
        assert album.is_photo and album.status == "failed"
        assert album.poster_path is None
        assert "альбом" in (album.error_reason or "").lower()

        await ingest_message(
            make_message(bot, text=EVENT_TEMPLATE, message_id=18, document=True), store
        )
        document = await store.get_source(CHAT_ID, 18)
        assert document is not None
        assert not document.is_photo and document.status == "failed"
        assert "документ" in (document.error_reason or "").lower()
        assert not any(request.__class__.__name__ == "GetFile" for request in session.requests)
        assert await store.list_drafts() == []
        await bot.session.close()

    asyncio.run(scenario())


def test_caption_validation_failure_preserves_downloaded_poster_and_source(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()

        await ingest_message(make_message(bot, text="Название: неполно", photo=True), store)

        source = await store.get_source(CHAT_ID, 17)
        assert source is not None
        assert source.status == "failed"
        assert source.poster_path is not None
        assert (store.path.parent / source.poster_path).read_bytes() == session.poster_bytes
        assert await store.list_drafts() == []
        assert await store.list_records() == []
        await bot.session.close()

    asyncio.run(scenario())


def test_media_file_survives_commit_failure_and_source_replay_finishes_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        original_set_path = store.set_source_poster_path
        attempts = 0

        async def fail_first_commit(source_id: int, poster_path: Path) -> Source:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated metadata commit failure")
            return await original_set_path(source_id, poster_path)

        monkeypatch.setattr(store, "set_source_poster_path", fail_first_commit)
        message = make_message(bot, text=EVENT_TEMPLATE, photo=True)

        await ingest_message(message, store)

        draft = (await store.list_drafts())[0]
        assert draft.poster_source_id is not None
        source = await store.get_source_by_id(draft.poster_source_id)
        assert source is not None
        assert source.attachment_status == "failed"
        assert source.poster_path is None
        final_file = store.path.parent / "posters" / f"{source.id}.jpg"
        assert final_file.read_bytes() == session.poster_bytes

        restarted = Store(store.path)
        await restarted.initialize()
        await ingest_message(message, restarted)

        recovered = await restarted.get_source_by_id(source.id)
        recovered_draft = await restarted.get_draft(draft.id)
        assert recovered is not None
        assert recovered.attachment_status == "saved"
        assert recovered.poster_path == Path("posters") / f"{source.id}.jpg"
        assert recovered_draft is not None and recovered_draft.status == "pending"
        assert recovered_draft.poster_source_id == source.id
        get_file_requests = [
            request for request in session.requests if request.__class__.__name__ == "GetFile"
        ]
        assert len(get_file_requests) == 1
        await bot.session.close()

    asyncio.run(scenario())


def test_unfinished_poster_blocks_confirmation_then_can_be_retried_and_removed(
    tmp_path: Path,
) -> None:
    class FailsFirstDownload(RecordingSession):
        def __init__(self) -> None:
            super().__init__(CHAT_ID)
            self.fail_next_download = True

        async def stream_content(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            timeout: int = 30,
            chunk_size: int = 65536,
            raise_for_status: bool = True,
        ) -> AsyncGenerator[bytes]:
            if self.fail_next_download:
                self.fail_next_download = False
                raise OSError("temporary download failure")
            async for chunk in super().stream_content(url, headers, timeout, chunk_size):
                yield chunk

    async def scenario() -> None:
        session = FailsFirstDownload()
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await ingest_message(make_message(bot, text=EVENT_TEMPLATE, photo=True), store)
        draft = (await store.list_drafts())[0]
        assert draft.poster_source_id is not None
        source = await store.get_source_by_id(draft.poster_source_id)
        assert source is not None and source.attachment_status == "failed"
        with pytest.raises(ValueError, match="афиша ещё не сохранена"):
            await store.confirm_draft(draft.id, draft.version)

        failed_card = next(
            request for request in session.requests if isinstance(request, SendMessage)
        )
        assert failed_card.reply_markup is not None
        retry_token = await token_for_button(
            store,
            Message(
                message_id=101,
                date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
                chat=Chat(id=CHAT_ID, type="private"),
                from_user=User(id=bot.id, is_bot=True, first_name="Dance bot"),
                reply_markup=failed_card.reply_markup,
            ),
            "Повторить загрузку афиши",
        )
        await handle_card_action(card_callback(bot, retry_token, 101), store)

        sent_cards = [request for request in session.requests if isinstance(request, SendMessage)]
        retry_card = sent_cards[1]
        if retry_card.reply_markup is None:
            raise AssertionError("Retry response did not show a draft card")
        remove_token = await token_for_button(
            store,
            Message(
                message_id=102,
                date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
                chat=Chat(id=CHAT_ID, type="private"),
                from_user=User(id=bot.id, is_bot=True, first_name="Dance bot"),
                reply_markup=retry_card.reply_markup,
            ),
            "Убрать афишу",
        )
        await handle_card_action(card_callback(bot, remove_token, 102), store)

        pending = [draft for draft in await store.list_drafts() if draft.status == "pending"]
        assert len(pending) == 1
        assert pending[0].poster_source_id is None
        assert source.poster_path is None
        stored_source = await store.get_source_by_id(source.id)
        assert stored_source is not None
        assert stored_source.attachment_status == "saved"
        assert stored_source.poster_path is not None
        assert (store.path.parent / stored_source.poster_path).is_file()
        record = await store.confirm_draft(pending[0].id, pending[0].version)
        assert record.status == "active" and record.poster_source_id is None
        await bot.session.close()

    asyncio.run(scenario())


def test_duplicate_delivery_reuses_the_existing_draft_and_binding(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        message = make_message(bot, text=EVENT_TEMPLATE)

        await ingest_message(message, store)
        await ingest_message(message, store)

        assert len(await store.list_drafts()) == 1
        assert len(await store.list_sources()) == 1
        assert len(session.messages) == 2
        await bot.session.close()

    asyncio.run(scenario())


def test_edited_telegram_message_does_not_change_catalog_or_sources(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()

        await ingest_message(make_message(bot, text=EVENT_TEMPLATE, edited=True), store)

        assert await store.list_sources() == []
        assert await store.list_drafts() == []
        assert await store.list_records() == []
        assert session.messages == []
        await bot.session.close()

    asyncio.run(scenario())


def test_text_edit_reply_keeps_confirmed_poster_until_new_version_is_confirmed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await ingest_message(make_message(bot, text=EVENT_TEMPLATE, photo=True), store)
        draft = (await store.list_drafts())[0]
        record = await store.confirm_draft(draft.id, draft.version)
        card_message = Message(
            message_id=20,
            date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        ).as_(bot)
        await show_record(card_message, record, store)
        reply = session.requests[-1]
        assert reply.__class__.__name__ == "SendMessage"
        bound_card = Message(
            message_id=102,
            date=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=bot.id, is_bot=True, first_name="Dance bot"),
        )
        replacement = EVENT_TEMPLATE.replace("Popping Jam", "Animation Jam")

        await ingest_message(
            make_message(bot, text=replacement, message_id=18, reply_to=bound_card),
            store,
        )

        active = await store.get_record(record.id)
        drafts = [draft for draft in await store.list_drafts() if draft.status == "pending"]
        assert active is not None and active.data.title == "Popping Jam"
        assert active.poster_source_id == record.poster_source_id
        assert len(drafts) == 1
        assert drafts[0].data.title == "Animation Jam"
        assert drafts[0].poster_source_id == record.poster_source_id
        await bot.session.close()

    asyncio.run(scenario())


def test_caption_template_downloads_and_saves_original_poster(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        message = make_message(bot, text=EVENT_TEMPLATE, photo=True)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()

        await ingest_message(message, store)

        source = await store.get_source(CHAT_ID, 17)
        draft = (await store.list_drafts())[0]
        assert source is not None
        assert source.poster_path is not None
        assert (store.path.parent / source.poster_path).read_bytes() == session.poster_bytes
        assert draft.poster_source_id == source.id

        await bot.session.close()

    asyncio.run(scenario())
