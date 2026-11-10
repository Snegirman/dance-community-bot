from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, User
from telegram_transport import RecordingSession

from dance_community_bot.cards import _chunks, format_card, show_draft, show_record
from dance_community_bot.intake import handle_card_action, ingest_message
from dance_community_bot.models import CardBinding, EventData, SourceInput
from dance_community_bot.storage import Store

OWNER_ID = 222
TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
TEMPLATE = "Название: Джем\nДата: 14.11.2026\nТип: Джем"


def photo_message(bot: Bot, *, reply_id: int | None = None) -> Message:
    chat = Chat(id=OWNER_ID, type="private")
    return Message(
        message_id=2,
        date=NOW,
        chat=chat,
        from_user=User(id=OWNER_ID, is_bot=False, first_name="Владелец"),
        caption=None if reply_id is not None else TEMPLATE,
        photo=[PhotoSize(file_id="poster", file_unique_id="poster", width=20, height=20)],
        reply_to_message=(
            Message(message_id=reply_id, date=NOW, chat=chat) if reply_id is not None else None
        ),
    ).as_(bot)


def test_newer_edit_survives_reply_that_was_waiting_for_download(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(chat_id=OWNER_ID, message_id=1, received_at=NOW, text=TEMPLATE)
        )
        data = EventData(title="Старое название", event_type="Джем", start_date=date(2026, 11, 14))
        first = await store.save_draft(source.id, data, poster_source_id=None)
        await store.bind_card_message(
            CardBinding(
                chat_id=OWNER_ID,
                bot_message_id=1000,
                record_id=first.record_id,
                draft_id=first.id,
                version=first.version,
            )
        )

        class InterleavedDownload(RecordingSession):
            async def stream_content(
                self,
                url: str,
                headers: dict[str, str] | None = None,
                timeout: int = 30,
                chunk_size: int = 65536,
                raise_for_status: bool = True,
            ) -> AsyncGenerator[bytes]:
                correction = await store.save_source(
                    SourceInput(chat_id=OWNER_ID, message_id=3, received_at=NOW, text=TEMPLATE)
                )
                await store.save_draft(
                    correction.id,
                    data.model_copy(update={"title": "Более новая правка"}),
                    poster_source_id=None,
                    record_id=first.record_id,
                )
                yield self.poster_bytes

        session = InterleavedDownload(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            await ingest_message(photo_message(bot, reply_id=1000), store)
            pending = [draft for draft in await store.list_drafts() if draft.status == "pending"]
            assert len(pending) == 1
            assert pending[0].data.title == "Более новая правка"
            assert pending[0].poster_source_id is None
        finally:
            await session.close()

    asyncio.run(scenario())


def callback_for(bot: Bot, token: str, message_id: int) -> CallbackQuery:
    return CallbackQuery(
        id=f"click-{message_id}",
        from_user=User(id=OWNER_ID, is_bot=False, first_name="Владелец"),
        chat_instance="private-owner",
        data=f"a:{token}",
        message=Message(
            message_id=message_id, date=NOW, chat=Chat(id=OWNER_ID, type="private")
        ).as_(bot),
    ).as_(bot)


def test_callback_token_from_another_message_cannot_confirm(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(chat_id=OWNER_ID, message_id=1, received_at=NOW, text=TEMPLATE)
        )
        draft = await store.save_draft(
            source.id,
            EventData(title="Джем", event_type="Джем", start_date=date(2026, 11, 14)),
            poster_source_id=None,
        )
        action = await store.create_card_action(
            "confirm", record_id=draft.record_id, draft_id=draft.id, version=draft.version
        )
        await store.bind_card_actions(OWNER_ID, 1000, [action.token])
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            await handle_card_action(callback_for(bot, action.token, 9999), store)
            assert await store.list_records() == []
            assert (await store.get_draft(draft.id)) == draft
        finally:
            await session.close()

    asyncio.run(scenario())


def test_long_incoming_draft_binds_confirmation_to_last_delivered_chunk(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        description = "x" * 3950
        text = f"Название: Джем\nДата: 14.11.2026\nТип: Джем\nОписание: {description}"
        assert len(text) < 4096

        async def fail_after_first_chunk() -> bool:
            return len(session.messages) == 0

        session.before_send_check = fail_after_first_chunk
        incoming = Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=OWNER_ID, type="private"),
            from_user=User(id=OWNER_ID, is_bot=False, first_name="Владелец"),
            text=text,
        ).as_(bot)
        try:
            with pytest.raises(AssertionError, match="Проверка перед отправкой"):
                await ingest_message(incoming, store)

            drafts = await store.list_drafts()
            assert len(drafts) == 1
            preview = (
                f"Черновик №{drafts[0].record_id} · версия {drafts[0].version}\n\n"
                f"{format_card(drafts[0].data)}\n\nАфиша: не добавлена"
            )
            assert len(_chunks(preview)) == 2
            assert session.requests[0].reply_markup is None

            session.before_send_check = None
            request_start = len(session.requests)
            await show_draft(incoming, drafts[0], store)
            delivered = session.requests[request_start:]
            assert len(delivered) == 2
            assert delivered[0].reply_markup is None
            assert delivered[1].reply_markup is not None
            confirm = delivered[1].reply_markup.inline_keyboard[0][0]
            assert confirm.text == "Сохранить"
            assert confirm.callback_data is not None
            action = await store.get_card_action(confirm.callback_data.removeprefix("a:"))
            assert action is not None and action.bot_message_id == 100 + len(session.messages)

            await handle_card_action(callback_for(bot, action.token, action.bot_message_id), store)
            record = await store.get_record(drafts[0].record_id)
            assert record is not None and record.status == "active"
        finally:
            await session.close()

    asyncio.run(scenario())


def test_duplicate_pending_card_warns_and_links_to_the_other_draft(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            for message_id in (1, 2):
                await ingest_message(
                    Message(
                        message_id=message_id,
                        date=NOW,
                        chat=Chat(id=OWNER_ID, type="private"),
                        text=TEMPLATE,
                    ).as_(bot),
                    store,
                )
            drafts = await store.list_drafts()
            assert len(drafts) == 2
            card = session.requests[-1]
            assert isinstance(card, SendMessage)
            assert "похоже на событие" in card.text
            assert card.reply_markup is not None
            matching_actions = []
            for row in card.reply_markup.inline_keyboard:
                for button in row:
                    assert button.callback_data is not None
                    action = await store.get_card_action(button.callback_data.removeprefix("a:"))
                    assert action is not None
                    if action.action == "view":
                        matching_actions.append(action)
            assert len(matching_actions) == 1
            assert matching_actions[0].draft_id == drafts[0].id
            assert matching_actions[0].record_id == drafts[0].record_id
        finally:
            await session.close()

    asyncio.run(scenario())


def test_duplicate_warning_ignores_stale_drafts_and_cancelled_records(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        data = EventData(
            title="Джем",
            event_type="Джем",
            start_date=date(2099, 11, 14),
            start_time=time(18, 30),
        )
        try:
            cancelled_source = await store.save_source(
                SourceInput(chat_id=OWNER_ID, message_id=1, received_at=NOW, text=TEMPLATE)
            )
            cancelled_draft = await store.save_draft(
                cancelled_source.id, data, poster_source_id=None
            )
            cancelled_record = await store.confirm_draft(
                cancelled_draft.id, cancelled_draft.version
            )
            await store.cancel_record(cancelled_record.id, cancelled_record.revision)

            rejected_source = await store.save_source(
                SourceInput(chat_id=OWNER_ID, message_id=2, received_at=NOW, text=TEMPLATE)
            )
            rejected_draft = await store.save_draft(rejected_source.id, data, poster_source_id=None)
            await store.reject_draft(rejected_draft.id, rejected_draft.version)

            superseded_source = await store.save_source(
                SourceInput(chat_id=OWNER_ID, message_id=3, received_at=NOW, text=TEMPLATE)
            )
            superseded_draft = await store.save_draft(
                superseded_source.id, data, poster_source_id=None
            )
            replacement_source = await store.save_source(
                SourceInput(chat_id=OWNER_ID, message_id=4, received_at=NOW, text=TEMPLATE)
            )
            await store.save_draft(
                replacement_source.id,
                data.model_copy(update={"title": "Другой джем"}),
                poster_source_id=None,
                record_id=superseded_draft.record_id,
            )

            active_source = await store.save_source(
                SourceInput(chat_id=OWNER_ID, message_id=5, received_at=NOW, text=TEMPLATE)
            )
            active_draft = await store.save_draft(active_source.id, data, poster_source_id=None)
            active_record = await store.confirm_draft(active_draft.id, active_draft.version)
            pending_edit_source = await store.save_source(
                SourceInput(chat_id=OWNER_ID, message_id=6, received_at=NOW, text=TEMPLATE)
            )
            pending_edit = await store.save_draft(
                pending_edit_source.id,
                data,
                poster_source_id=None,
                record_id=active_record.id,
                base_revision=active_record.revision,
            )

            incoming_source = await store.save_source(
                SourceInput(chat_id=OWNER_ID, message_id=7, received_at=NOW, text=TEMPLATE)
            )
            incoming_draft = await store.save_draft(incoming_source.id, data, poster_source_id=None)
            message = Message(
                message_id=8,
                date=NOW,
                chat=Chat(id=OWNER_ID, type="private"),
                from_user=User(id=OWNER_ID, is_bot=False, first_name="Владелец"),
            ).as_(bot)

            await show_draft(message, incoming_draft, store)

            card = session.requests[-1]
            assert isinstance(card, SendMessage)
            assert card.text.lower().count("похоже на событие") == 1
            assert card.reply_markup is not None
            view_actions = []
            for row in card.reply_markup.inline_keyboard:
                for button in row:
                    if button.callback_data is None:
                        continue
                    action = await store.get_card_action(button.callback_data.removeprefix("a:"))
                    if action is not None and action.action == "view":
                        view_actions.append(action)
            assert len(view_actions) == 1
            assert view_actions[0].draft_id == pending_edit.id
            assert view_actions[0].record_id == pending_edit.record_id
        finally:
            await session.close()

    asyncio.run(scenario())


def test_reply_to_edit_prompt_after_restart_keeps_record_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(chat_id=OWNER_ID, message_id=1, received_at=NOW, text=TEMPLATE)
        )
        draft = await store.save_draft(
            source.id,
            EventData(title="Джем", event_type="Джем", start_date=date(2026, 11, 14)),
            poster_source_id=None,
        )
        await store.bind_card_message(
            CardBinding(
                chat_id=OWNER_ID,
                bot_message_id=1000,
                record_id=draft.record_id,
                draft_id=draft.id,
                version=draft.version,
            )
        )
        action = await store.create_card_action(
            "edit", record_id=draft.record_id, draft_id=draft.id, version=draft.version
        )
        await store.bind_card_actions(OWNER_ID, 1000, [action.token])
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            await handle_card_action(callback_for(bot, action.token, 1000), store)
            correction = Message(
                message_id=2,
                date=NOW,
                chat=Chat(id=OWNER_ID, type="private"),
                from_user=User(id=OWNER_ID, is_bot=False, first_name="Владелец"),
                text=TEMPLATE.replace("Название: Джем", "Название: Правка"),
                reply_to_message=Message(
                    message_id=101, date=NOW, chat=Chat(id=OWNER_ID, type="private")
                ),
            ).as_(bot)
            await ingest_message(correction, Store(store.path))
            pending = [item for item in await store.list_drafts() if item.status == "pending"]
            assert len(pending) == 1
            assert pending[0].record_id == draft.record_id
            assert pending[0].data.title == "Правка"
        finally:
            await session.close()

    asyncio.run(scenario())


def test_remove_confirmed_poster_preserves_original_until_confirmation(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(
                chat_id=OWNER_ID,
                message_id=1,
                received_at=NOW,
                text=TEMPLATE,
                telegram_file_id="poster",
                is_photo=True,
            )
        )
        poster_path = Path("posters") / f"{source.id}.jpg"
        (store.path.parent / poster_path).write_bytes(b"original")
        await store.set_source_poster_path(source.id, poster_path)
        draft = await store.save_draft(
            source.id,
            EventData(title="Джем", event_type="Джем", start_date=date(2026, 11, 14)),
            poster_source_id=source.id,
        )
        record = await store.confirm_draft(draft.id, draft.version)
        action = await store.create_card_action(
            "remove_poster", record_id=record.id, revision=record.revision, source_id=source.id
        )
        await store.bind_card_actions(OWNER_ID, 1000, [action.token])
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            await handle_card_action(callback_for(bot, action.token, 1000), store)
            pending = [item for item in await store.list_drafts() if item.status == "pending"]
            assert len(pending) == 1
            assert pending[0].poster_source_id is None
            assert await store.get_record(record.id) == record
            updated = await store.confirm_draft(pending[0].id, pending[0].version)
            assert updated.poster_source_id is None
            original = await store.get_source_by_id(source.id)
            assert original is not None and original.poster_path == poster_path
            assert (store.path.parent / poster_path).read_bytes() == b"original"
        finally:
            await session.close()

    asyncio.run(scenario())


def test_retry_source_without_draft_downloads_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(
                chat_id=OWNER_ID,
                message_id=1,
                received_at=NOW,
                telegram_file_id="poster",
                is_photo=True,
            )
        )
        await store.set_source_error(source.id, "Сбой загрузки")
        action = await store.create_card_action("retry_poster", source_id=source.id)
        await store.bind_card_actions(OWNER_ID, 1000, [action.token])
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            await handle_card_action(callback_for(bot, action.token, 1000), Store(store.path))
            restored = await store.get_source_by_id(source.id)
            assert restored is not None and restored.poster_path is not None
            assert (store.path.parent / restored.poster_path).read_bytes() == session.poster_bytes
            assert await store.list_records() == []
            assert await store.list_drafts() == []
        finally:
            await session.close()

    asyncio.run(scenario())


def test_correcting_failed_caption_by_reply_keeps_saved_poster(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            invalid = photo_message(bot).model_copy(update={"caption": "Название: Джем"})
            await ingest_message(invalid, store)
            source = await store.get_source(OWNER_ID, 2)
            assert source is not None and source.poster_path is not None
            assert await store.list_drafts() == []
            correction = Message(
                message_id=3,
                date=NOW,
                chat=Chat(id=OWNER_ID, type="private"),
                from_user=User(id=OWNER_ID, is_bot=False, first_name="Владелец"),
                text=TEMPLATE,
                reply_to_message=Message(
                    message_id=101, date=NOW, chat=Chat(id=OWNER_ID, type="private")
                ),
            ).as_(bot)
            await ingest_message(correction, Store(store.path))
            drafts = await store.list_drafts()
            assert len(drafts) == 1
            assert drafts[0].poster_source_id == source.id
        finally:
            await session.close()

    asyncio.run(scenario())


def test_long_emoji_card_preserves_text_in_telegram_sized_messages(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(chat_id=OWNER_ID, message_id=1, received_at=NOW, text=TEMPLATE)
        )
        description = "💃" * 4000 + "<все условия сохранены>"
        draft = await store.save_draft(
            source.id,
            EventData(
                title="Джем",
                event_type="Джем",
                start_date=date(2026, 11, 14),
                description=description,
            ),
            poster_source_id=None,
        )
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            await show_draft(photo_message(bot), draft, store)
            assert description in "".join(session.messages)
            assert all(len(text.encode("utf-16-le")) // 2 <= 4096 for text in session.messages)
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "description",
    [
        "я" * 4095 + "💃" * 2000 + "<все условия сохранены>",
        "я" * 4086 + "\n" + "💃" * 3000,
        "я" * 4086 + "\n\n" + "💃" * 3000,
    ],
    ids=["mixed_text", "newline_at_boundary", "blank_line_at_boundary"],
)
def test_mixed_russian_and_emoji_card_splits_at_utf16_boundary(
    tmp_path: Path, description: str
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(chat_id=OWNER_ID, message_id=1, received_at=NOW, text=TEMPLATE)
        )
        draft = await store.save_draft(
            source.id,
            EventData(
                title="Джем",
                event_type="Джем",
                start_date=date(2099, 11, 14),
                description=description,
            ),
            poster_source_id=None,
        )
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            await show_draft(photo_message(bot), draft, store)
            expected = (
                f"Черновик №{draft.record_id} · версия {draft.version}\n\n"
                f"{format_card(draft.data)}\n\n"
                "Афиша: не добавлена"
            )
            assert "".join(session.messages) == expected
            assert all(len(text.encode("utf-16-le")) // 2 <= 4096 for text in session.messages)
            assert all(text.strip() for text in session.messages)
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("has_poster", [False, True])
def test_cancelled_record_card_reports_current_status_without_edit_buttons(
    tmp_path: Path, has_poster: bool
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(
                chat_id=OWNER_ID,
                message_id=1,
                received_at=NOW,
                text=TEMPLATE,
                telegram_file_id="poster" if has_poster else None,
                is_photo=has_poster,
            )
        )
        if has_poster:
            poster_path = Path("posters/cancelled.jpg")
            poster_file = store.path.parent / poster_path
            poster_file.write_bytes(b"poster")
            await store.set_source_poster_path(source.id, poster_path)
        draft = await store.save_draft(
            source.id,
            EventData(title="Джем", event_type="Джем", start_date=date(2026, 11, 14)),
            poster_source_id=source.id if has_poster else None,
        )
        record = await store.confirm_draft(draft.id, draft.version)
        cancelled = await store.cancel_record(record.id, record.revision)
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            await show_record(photo_message(bot), cancelled, store)
            assert "отменена" in "\n".join(session.messages).lower()
            assert all(
                request.reply_markup is None
                for request in session.requests
                if isinstance(request, SendMessage)
            )
        finally:
            await session.close()

    asyncio.run(scenario())


def test_source_only_media_failure_resumes_template_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(
                chat_id=OWNER_ID,
                message_id=2,
                received_at=NOW,
                text=TEMPLATE,
                telegram_file_id="poster",
                is_photo=True,
            )
        )
        original = store.path.parent / "posters" / f"{source.id}.jpg"
        original.write_bytes(b"original-downloaded-before-restart")
        await store.set_source_error(source.id, "Сбой фиксации пути")
        restarted = Store(store.path)
        await restarted.initialize()
        session = RecordingSession(OWNER_ID)
        bot = Bot(token=TOKEN, session=session)
        try:
            await ingest_message(photo_message(bot), restarted)
            drafts = await restarted.list_drafts()
            assert len(drafts) == 1
            assert drafts[0].poster_source_id == source.id
            assert drafts[0].data.title == "Джем"
            assert original.read_bytes() == b"original-downloaded-before-restart"
        finally:
            await session.close()

    asyncio.run(scenario())
