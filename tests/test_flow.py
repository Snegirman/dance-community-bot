from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetFile, SendMessage, SendPhoto, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    PhotoSize,
    Update,
    User,
)
from telegram_transport import RecordingSession

from dance_community_bot import bot as bot_module
from dance_community_bot import publication_handlers
from dance_community_bot.bot import create_dispatcher
from dance_community_bot.config import Settings
from dance_community_bot.models import CardData, Draft
from dance_community_bot.scheduler import check_publication
from dance_community_bot.storage import Store

TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
OWNER_ID = 222
GROUP_ID = -100123
NOW = datetime(2026, 11, 25, 9, tzinfo=UTC)
EVENT_TEMPLATE = """Название: Popping Jam
Дата: 27.11.2026
Тип: Джем
Время: 18:30
Место: Студия, адрес
Стоимость: До 26 ноября — 500 ₽; на входе — 700 ₽
Описание: Свободные выходы"""


def settings(database_path: Path, *, group_id: int | None = GROUP_ID) -> Settings:
    return Settings(
        telegram_bot_token=TOKEN,
        telegram_owner_id=OWNER_ID,
        telegram_webhook_url="https://dance.example.org/telegram/webhook",
        telegram_webhook_secret="test_webhook_secret",
        database_path=database_path,
        telegram_group_id=group_id,
    )


def quiet_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    async def wait_for_shutdown(current_settings: Settings, store: Store, bot: Bot) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(bot_module, "run_scheduler", wait_for_shutdown)


def next_session(
    chat_id: int = OWNER_ID,
    previous: RecordingSession | None = None,
) -> RecordingSession:
    session = RecordingSession(chat_id)
    if previous is not None:
        session.requests = previous.requests
        session.messages = previous.messages
        session.photos = previous.photos
        session.poster_bytes = previous.poster_bytes
    return session


async def start_app(
    database_path: Path,
    current_settings: Settings,
    session: RecordingSession,
    store: Store | None = None,
) -> tuple[Store, Dispatcher, Bot]:
    current_store = store or Store(database_path)
    dispatcher = create_dispatcher(current_settings, current_store)
    bot = Bot(token=TOKEN, session=session)
    await dispatcher.emit_startup(bot=bot)
    return current_store, dispatcher, bot


async def stop_app(dispatcher: Dispatcher, bot: Bot) -> None:
    await dispatcher.emit_shutdown(bot=bot)
    await bot.session.close()


def message_update(
    bot: Bot,
    *,
    update_id: int,
    message_id: int,
    text: str | None = None,
    caption: str | None = None,
    photo_file_id: str | None = None,
    reply_to: Message | None = None,
    user_id: int = OWNER_ID,
    chat_id: int = OWNER_ID,
    chat_type: str = "private",
) -> Update:
    photo = (
        [PhotoSize(file_id=photo_file_id, file_unique_id=photo_file_id, width=640, height=640)]
        if photo_file_id is not None
        else []
    )
    message = Message(
        message_id=message_id,
        date=NOW,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="Владелец"),
        text=text,
        caption=caption,
        photo=photo,
        reply_to_message=reply_to,
    ).as_(bot)
    return Update(update_id=update_id, message=message)


def sent_requests(session: RecordingSession) -> list[SendMessage | SendPhoto]:
    return [
        request for request in session.requests if isinstance(request, (SendMessage, SendPhoto))
    ]


def bound_message_id(
    session: RecordingSession,
    request: SendMessage | SendPhoto,
) -> int:
    return 101 + next(
        index for index, candidate in enumerate(sent_requests(session)) if candidate is request
    )


def button_request(
    session: RecordingSession,
    label: str,
    *,
    after: int = 0,
) -> SendMessage:
    for request in reversed(sent_requests(session)[after:]):
        if not isinstance(request, SendMessage) or request.reply_markup is None:
            continue
        if any(
            button.text == label for row in request.reply_markup.inline_keyboard for button in row
        ):
            return request
    raise AssertionError(f"Не найдено сообщение с кнопкой «{label}».")


def button_data(request: SendMessage, label: str) -> str:
    assert request.reply_markup is not None
    for row in request.reply_markup.inline_keyboard:
        for button in row:
            if button.text == label:
                assert button.callback_data is not None
                return button.callback_data
    raise AssertionError(f"Не найдена кнопка «{label}».")


def chat_send_requests(
    session: RecordingSession,
    chat_id: int,
) -> list[SendMessage | SendPhoto]:
    return [request for request in sent_requests(session) if request.chat_id == chat_id]


async def click_button(
    dispatcher: Dispatcher,
    bot: Bot,
    session: RecordingSession,
    request: SendMessage,
    label: str,
    *,
    update_id: int,
    user_id: int = OWNER_ID,
    chat_id: int = OWNER_ID,
    chat_type: str = "private",
) -> None:
    callback_message = Message(
        message_id=bound_message_id(session, request),
        date=NOW,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=0, is_bot=True, first_name="Бот"),
        text=request.text,
        reply_markup=request.reply_markup,
    ).as_(bot)
    callback = CallbackQuery(
        id=f"callback-{update_id}",
        from_user=User(id=user_id, is_bot=False, first_name="Пользователь"),
        chat_instance="flow-test",
        message=callback_message,
        data=button_data(request, label),
    ).as_(bot)
    await dispatcher.feed_update(bot, Update(update_id=update_id, callback_query=callback))


class FailDraftOnceStore(Store):
    def __init__(self, path: Path, *, require_downloaded_poster: bool) -> None:
        super().__init__(path)
        self.fail_next_draft = True
        self.require_downloaded_poster = require_downloaded_poster

    async def save_draft(
        self,
        source_id: int,
        data: CardData,
        poster_source_id: int | None,
        record_id: int | None = None,
        base_revision: int | None = None,
        expected_draft_id: int | None = None,
    ) -> Draft:
        if self.fail_next_draft:
            self.fail_next_draft = False
            if self.require_downloaded_poster:
                source = await self.get_source_by_id(source_id)
                assert source is not None and source.poster_path is not None
                assert (self.path.parent / source.poster_path).read_bytes() == b"poster-bytes"
            raise RuntimeError("simulated draft-save failure")
        return await super().save_draft(
            source_id,
            data,
            poster_source_id,
            record_id,
            base_revision,
            expected_draft_id,
        )


class FailGetFileOnceSession(RecordingSession):
    def __init__(self, chat_id: int, previous: RecordingSession | None = None) -> None:
        super().__init__(chat_id)
        self.fail_get_file = previous is None
        self.failed_file_requests: list[GetFile] = (
            previous.failed_file_requests if isinstance(previous, FailGetFileOnceSession) else []
        )
        if previous is not None:
            self.requests = previous.requests
            self.messages = previous.messages
            self.photos = previous.photos
            self.poster_bytes = previous.poster_bytes

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        if self.fail_get_file and isinstance(method, GetFile):
            self.fail_get_file = False
            self.failed_file_requests.append(method)
            raise TelegramBadRequest(method=method, message="file temporarily unavailable")
        return await super().make_request(bot, method, timeout)


class GroupSendFaultSession(RecordingSession):
    def __init__(
        self,
        chat_id: int,
        *,
        failure: str,
        fail_attempt: int,
        previous: GroupSendFaultSession | None = None,
    ) -> None:
        super().__init__(chat_id)
        self.failure = failure
        self.fail_attempt = fail_attempt
        self.group_attempts: list[SendMessage] = (
            previous.group_attempts if previous is not None else []
        )
        if previous is not None:
            self.requests = previous.requests
            self.messages = previous.messages
            self.photos = previous.photos
            self.poster_bytes = previous.poster_bytes

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        is_group_message = isinstance(method, SendMessage) and method.chat_id == GROUP_ID
        if is_group_message:
            attempt = len(self.group_attempts) + 1
            self.group_attempts.append(method)
            if attempt == self.fail_attempt and self.failure == "partial":
                raise TelegramBadRequest(method=method, message="explicit test refusal")
            response = await super().make_request(bot, method, timeout)
            if attempt == self.fail_attempt and self.failure == "unknown":
                raise TimeoutError("test lost the Telegram response")
            return response
        return await super().make_request(bot, method, timeout)


def test_event_poster_edit_stale_button_and_restart_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        quiet_scheduler(monkeypatch)
        database_path = tmp_path / "dance.sqlite3"
        current_settings = settings(database_path)
        session = next_session()
        store, dispatcher, bot = await start_app(database_path, current_settings, session)

        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=1,
                message_id=1,
                caption=EVENT_TEMPLATE,
                photo_file_id="poster-file-id",
            ),
        )
        source = await store.get_source(OWNER_ID, 1)
        assert source is not None
        assert source.text == EVENT_TEMPLATE
        assert source.status == "processed"
        assert source.attachment_status == "saved"
        assert source.poster_path is not None
        assert (database_path.parent / source.poster_path).read_bytes() == b"poster-bytes"
        drafts = await store.list_drafts()
        assert len(drafts) == 1 and drafts[0].status == "pending"
        original_confirmation = button_request(session, "Сохранить")

        await click_button(
            dispatcher,
            bot,
            session,
            original_confirmation,
            "Исправить",
            update_id=2,
        )
        prompt = sent_requests(session)[-1]
        assert isinstance(prompt, SendMessage)
        edited_template = EVENT_TEMPLATE.replace("Popping Jam", "Popping Jam — редакция")
        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=3,
                message_id=2,
                text=edited_template,
                reply_to=Message(
                    message_id=bound_message_id(session, prompt),
                    date=NOW,
                    chat=Chat(id=OWNER_ID, type="private"),
                    from_user=User(id=0, is_bot=True, first_name="Бот"),
                    text=prompt.text,
                ),
            ),
        )
        drafts = await store.list_drafts()
        assert [draft.status for draft in drafts] == ["superseded", "pending"]
        new_confirmation = button_request(session, "Сохранить")
        await click_button(
            dispatcher,
            bot,
            session,
            original_confirmation,
            "Сохранить",
            update_id=4,
        )
        assert await store.list_records() == []
        assert (await store.get_draft(drafts[0].id)).status == "superseded"

        await click_button(
            dispatcher,
            bot,
            session,
            new_confirmation,
            "Сохранить",
            update_id=5,
        )
        record = (await store.list_records())[0]
        assert record.status == "active"
        assert record.data.title == "Popping Jam — редакция"
        assert record.poster_source_id == source.id
        await click_button(
            dispatcher,
            bot,
            session,
            new_confirmation,
            "Сохранить",
            update_id=6,
        )
        assert await store.list_records() == [record]
        await stop_app(dispatcher, bot)

        restarted_session = next_session(previous=session)
        restarted_store, restarted_dispatcher, restarted_bot = await start_app(
            database_path, current_settings, restarted_session
        )
        await restarted_dispatcher.feed_update(
            restarted_bot,
            message_update(
                restarted_bot,
                update_id=7,
                message_id=3,
                text="/events",
            ),
        )
        view = button_request(restarted_session, f"Открыть №{record.id}")
        view_offset = len(sent_requests(restarted_session))
        await click_button(
            restarted_dispatcher,
            restarted_bot,
            restarted_session,
            view,
            f"Открыть №{record.id}",
            update_id=8,
        )
        view_requests = sent_requests(restarted_session)[view_offset:]
        displayed = [request.text for request in view_requests if isinstance(request, SendMessage)]
        assert any("Popping Jam — редакция" in text for text in displayed)
        assert any("До 26 ноября — 500 ₽; на входе — 700 ₽" in text for text in displayed)
        assert any("Афиша: добавлена" in text for text in displayed)
        restored_record = await restarted_store.get_record(record.id)
        assert restored_record is not None and restored_record.poster_source_id == source.id
        assert (database_path.parent / source.poster_path).read_bytes() == b"poster-bytes"
        assert len(await restarted_store.list_sources()) == 2
        preview_offset = len(sent_requests(restarted_session))
        await restarted_dispatcher.feed_update(
            restarted_bot,
            message_update(
                restarted_bot,
                update_id=9,
                message_id=4,
                text="/preview 27.11.2026",
            ),
        )
        assert any(
            isinstance(request, SendPhoto)
            and request.caption is not None
            and "Popping Jam — редакция" in request.caption
            for request in sent_requests(restarted_session)[preview_offset:]
        )
        await stop_app(restarted_dispatcher, restarted_bot)

    asyncio.run(scenario())


@pytest.mark.parametrize("has_poster", [False, True])
def test_source_is_reprocessed_after_draft_save_failure_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_poster: bool,
) -> None:
    async def scenario() -> None:
        quiet_scheduler(monkeypatch)
        database_path = tmp_path / "dance.sqlite3"
        current_settings = settings(database_path)
        session = next_session()
        failing_store = FailDraftOnceStore(
            database_path,
            require_downloaded_poster=has_poster,
        )
        store, dispatcher, bot = await start_app(
            database_path, current_settings, session, failing_store
        )
        incoming = message_update(
            bot,
            update_id=1,
            message_id=1,
            text=None if has_poster else EVENT_TEMPLATE,
            caption=EVENT_TEMPLATE if has_poster else None,
            photo_file_id="poster-file-id" if has_poster else None,
        )
        with pytest.raises(RuntimeError, match="simulated draft-save failure"):
            await dispatcher.feed_update(bot, incoming)
        source = await store.get_source(OWNER_ID, 1)
        assert source is not None
        assert await store.list_drafts() == []
        if has_poster:
            assert source.attachment_status == "saved"
            assert source.poster_path is not None
            assert (database_path.parent / source.poster_path).read_bytes() == b"poster-bytes"
        await stop_app(dispatcher, bot)

        restarted_session = next_session(previous=session)
        restarted_store, restarted_dispatcher, restarted_bot = await start_app(
            database_path, current_settings, restarted_session
        )
        replay = message_update(
            restarted_bot,
            update_id=2,
            message_id=1,
            text=None if has_poster else EVENT_TEMPLATE,
            caption=EVENT_TEMPLATE if has_poster else None,
            photo_file_id="poster-file-id" if has_poster else None,
        )
        await restarted_dispatcher.feed_update(restarted_bot, replay)
        sources = await restarted_store.list_sources()
        drafts = await restarted_store.list_drafts()
        assert len(sources) == len(drafts) == 1
        assert drafts[0].status == "pending"
        assert sources[0].status == "processed"
        if has_poster:
            assert sources[0].attachment_status == "saved"
            assert sources[0].poster_path is not None
            assert (database_path.parent / sources[0].poster_path).read_bytes() == b"poster-bytes"
            assert drafts[0].poster_source_id == sources[0].id
        await stop_app(restarted_dispatcher, restarted_bot)

    asyncio.run(scenario())


def test_invalid_template_and_duplicate_content_keep_distinct_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        quiet_scheduler(monkeypatch)
        database_path = tmp_path / "dance.sqlite3"
        session = next_session()
        store, dispatcher, bot = await start_app(database_path, settings(database_path), session)

        invalid = "Название: Неполная афиша\nДата: 28.11.2026"
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=1, message_id=1, text=invalid),
        )
        invalid_source = await store.get_source(OWNER_ID, 1)
        assert invalid_source is not None
        assert invalid_source.text == invalid
        assert invalid_source.status == "failed"
        assert invalid_source.error_reason is not None
        assert await store.list_drafts() == []

        for update_id, message_id in ((2, 2), (3, 3)):
            await dispatcher.feed_update(
                bot,
                message_update(
                    bot,
                    update_id=update_id,
                    message_id=message_id,
                    text=EVENT_TEMPLATE,
                ),
            )
        sources = await store.list_sources()
        drafts = await store.list_drafts()
        assert [source.message_id for source in sources] == [1, 2, 3]
        assert len(drafts) == 2
        assert drafts[0].data == drafts[1].data
        assert drafts[0].source_id != drafts[1].source_id
        assert any("похоже на событие" in request.text for request in sent_requests(session))

        source_count = len(sources)
        draft_count = len(drafts)
        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=4,
                message_id=2,
                text=EVENT_TEMPLATE,
            ),
        )
        assert len(await store.list_sources()) == source_count
        assert len(await store.list_drafts()) == draft_count
        assert await store.list_records() == []
        await stop_app(dispatcher, bot)

    asyncio.run(scenario())


def test_unavailable_poster_can_be_retried_from_bound_draft_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        quiet_scheduler(monkeypatch)
        database_path = tmp_path / "dance.sqlite3"
        current_settings = settings(database_path)
        first_session = FailGetFileOnceSession(OWNER_ID)
        store, dispatcher, bot = await start_app(database_path, current_settings, first_session)
        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=1,
                message_id=1,
                caption=EVENT_TEMPLATE,
                photo_file_id="unavailable-poster-file-id",
            ),
        )
        source = await store.get_source(OWNER_ID, 1)
        drafts = await store.list_drafts()
        assert source is not None
        assert source.attachment_status == "failed"
        assert source.status == "failed"
        assert source.error_reason is not None
        assert len(drafts) == 1 and drafts[0].status == "pending"
        assert drafts[0].poster_source_id == source.id
        retry_message = button_request(first_session, "Повторить загрузку афиши")
        await click_button(
            dispatcher,
            bot,
            first_session,
            button_request(first_session, "Сохранить"),
            "Сохранить",
            update_id=2,
        )
        assert await store.list_records() == []
        await stop_app(dispatcher, bot)

        retry_session = FailGetFileOnceSession(OWNER_ID, previous=first_session)
        restarted_store, restarted_dispatcher, restarted_bot = await start_app(
            database_path, current_settings, retry_session
        )
        await click_button(
            restarted_dispatcher,
            restarted_bot,
            retry_session,
            retry_message,
            "Повторить загрузку афиши",
            update_id=3,
        )
        retried_source = await restarted_store.get_source_by_id(source.id)
        retried_drafts = await restarted_store.list_drafts()
        assert retried_source is not None
        assert retried_source.attachment_status == "saved"
        assert retried_source.status == "processed"
        assert retried_source.poster_path is not None
        assert (database_path.parent / retried_source.poster_path).read_bytes() == b"poster-bytes"
        assert len(retried_drafts) == 1 and retried_drafts[0].id == drafts[0].id
        assert len(retry_session.failed_file_requests) == 1
        assert sum(isinstance(request, GetFile) for request in retry_session.requests) == 1
        await click_button(
            restarted_dispatcher,
            restarted_bot,
            retry_session,
            button_request(retry_session, "Сохранить"),
            "Сохранить",
            update_id=4,
        )
        records = await restarted_store.list_records()
        assert len(records) == 1 and records[0].poster_source_id == source.id
        await stop_app(restarted_dispatcher, restarted_bot)

    asyncio.run(scenario())


def test_cancel_record_supersedes_open_edit_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        quiet_scheduler(monkeypatch)
        database_path = tmp_path / "dance.sqlite3"
        session = next_session()
        store, dispatcher, bot = await start_app(database_path, settings(database_path), session)
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=1, message_id=1, text=EVENT_TEMPLATE),
        )
        original_draft_message = button_request(session, "Сохранить")
        await click_button(
            dispatcher,
            bot,
            session,
            original_draft_message,
            "Сохранить",
            update_id=2,
        )
        cancel_message = button_request(session, "Отменить")
        record = (await store.list_records())[0]

        await click_button(
            dispatcher,
            bot,
            session,
            cancel_message,
            "Исправить",
            update_id=3,
        )
        prompt = sent_requests(session)[-1]
        assert isinstance(prompt, SendMessage)
        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=4,
                message_id=2,
                text=EVENT_TEMPLATE.replace("Popping Jam", "Future Popping Jam"),
                reply_to=Message(
                    message_id=bound_message_id(session, prompt),
                    date=NOW,
                    chat=Chat(id=OWNER_ID, type="private"),
                    from_user=User(id=0, is_bot=True, first_name="Бот"),
                    text=prompt.text,
                ),
            ),
        )
        open_edit = button_request(session, "Сохранить")
        pending_edit = (await store.list_drafts())[-1]
        assert pending_edit.status == "pending"

        await click_button(
            dispatcher,
            bot,
            session,
            cancel_message,
            "Отменить",
            update_id=5,
        )
        cancelled = await store.get_record(record.id)
        assert cancelled is not None and cancelled.status == "cancelled"
        assert (await store.get_draft(pending_edit.id)).status == "superseded"

        await click_button(
            dispatcher,
            bot,
            session,
            open_edit,
            "Сохранить",
            update_id=6,
        )
        assert (await store.get_record(record.id)).status == "cancelled"
        assert len(await store.list_records()) == 1
        await stop_app(dispatcher, bot)

    asyncio.run(scenario())


def test_transferred_series_publication_change_notification_and_publish_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        quiet_scheduler(monkeypatch)
        database_path = tmp_path / "dance.sqlite3"
        current_settings = settings(database_path)
        session = next_session()
        store, dispatcher, bot = await start_app(database_path, current_settings, session)

        class_template = """Название: Popping
Первая дата: 24.11.2026
Время: 19:00
Место: Зал A
Стоимость: 600 ₽"""
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=1, message_id=1, text=class_template),
        )
        class_confirmation = button_request(session, "Сохранить")
        await click_button(
            dispatcher,
            bot,
            session,
            class_confirmation,
            "Сохранить",
            update_id=2,
        )
        series = (await store.list_records())[0]

        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=3,
                message_id=2,
                text="/schedule 23.11.2026",
            ),
        )
        occurrence = next(
            request
            for request in reversed(sent_requests(session))
            if isinstance(request, SendMessage)
            and request.reply_markup is not None
            and any(
                button.text == "Перенести"
                for row in request.reply_markup.inline_keyboard
                for button in row
            )
        )
        await click_button(
            dispatcher,
            bot,
            session,
            occurrence,
            "Перенести",
            update_id=4,
        )
        transfer_prompt = sent_requests(session)[-1]
        assert isinstance(transfer_prompt, SendMessage)
        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=5,
                message_id=3,
                text="Дата: 30.11.2026\nВремя: 20:00\nМесто: Зал B",
                reply_to=Message(
                    message_id=bound_message_id(session, transfer_prompt),
                    date=NOW,
                    chat=Chat(id=OWNER_ID, type="private"),
                    from_user=User(id=0, is_bot=True, first_name="Бот"),
                    text=transfer_prompt.text,
                ),
            ),
        )
        transfer_preview = button_request(session, "Подтвердить")
        assert "30.11.2026 в 20:00" in transfer_preview.text
        await click_button(
            dispatcher,
            bot,
            session,
            transfer_preview,
            "Подтвердить",
            update_id=6,
        )
        _, exceptions = await store.get_schedule()
        moved = next(item for item in exceptions if item.series_id == series.id)
        assert (moved.original_date, moved.new_date, moved.new_time, moved.place) == (
            date(2026, 11, 24),
            date(2026, 11, 30),
            time(20),
            "Зал B",
        )

        original_week_offset = len(sent_requests(session))
        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=7,
                message_id=4,
                text="/preview 23.11.2026",
            ),
        )
        original_week_messages = sent_requests(session)[original_week_offset:]
        assert original_week_messages
        assert any(
            isinstance(request, SendMessage) and request.text == "На эту неделю записей нет."
            for request in original_week_messages
        )
        preview_offset = len(sent_requests(session))
        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=8,
                message_id=5,
                text="/publish 30.11.2026",
            ),
        )
        publish_preview = button_request(session, "Опубликовать")
        assert any(
            isinstance(request, SendMessage) and "Popping" in request.text
            for request in sent_requests(session)[preview_offset:]
        )
        assert "30.11.2026" in publish_preview.text
        assert chat_send_requests(session, GROUP_ID) == []
        await click_button(
            dispatcher,
            bot,
            session,
            publish_preview,
            "Опубликовать",
            update_id=9,
        )
        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 30))
        assert publication is not None and publication.status == "delivered"
        transferred = [
            occurrence
            for occurrence in publication.snapshot.occurrences
            if occurrence.original_date == date(2026, 11, 24)
        ]
        assert len(transferred) == 1
        assert transferred[0].actual_date == date(2026, 11, 30)
        assert transferred[0].place == "Зал B"
        group_send_count = len(chat_send_requests(session, GROUP_ID))
        assert group_send_count > 0

        event_template = "Название: Встреча\nДата: 02.12.2026\nТип: Практика"
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=10, message_id=6, text=event_template),
        )
        event_confirmation = button_request(session, "Сохранить")
        await click_button(
            dispatcher,
            bot,
            session,
            event_confirmation,
            "Сохранить",
            update_id=11,
        )
        notification_before_delivery = await store.list_pending_publication_notifications()
        assert len(notification_before_delivery) == 1
        assert "выпуск" in notification_before_delivery[0].text.casefold()
        await stop_app(dispatcher, bot)
        session = next_session(previous=session)
        store, dispatcher, bot = await start_app(database_path, current_settings, session)
        notice_offset = len(session.requests)
        await check_publication(NOW, current_settings, store, bot)
        assert len(await store.list_pending_publication_notifications()) == 0
        owner_notices = [
            request.text
            for request in session.requests[notice_offset:]
            if isinstance(request, SendMessage) and request.chat_id == OWNER_ID
        ]
        assert any("устарел" in text.casefold() for text in owner_notices)
        assert any("Встреча" in text for text in owner_notices)
        notice_count = len(chat_send_requests(session, OWNER_ID))
        await check_publication(NOW, current_settings, store, bot)
        assert len(chat_send_requests(session, OWNER_ID)) == notice_count

        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=12,
                message_id=7,
                text="/publish 30.11.2026",
            ),
        )
        assert len(chat_send_requests(session, GROUP_ID)) == group_send_count
        unchanged = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 30))
        assert unchanged is not None and unchanged.status == "delivered"
        assert unchanged.snapshot == publication.snapshot
        await stop_app(dispatcher, bot)

    asyncio.run(scenario())


def test_republish_is_an_explicit_idempotent_action_for_current_week(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            frozen = datetime(2026, 11, 25, 10, tzinfo=ZoneInfo("Europe/Moscow"))
            return frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)

    async def scenario() -> None:
        quiet_scheduler(monkeypatch)
        monkeypatch.setattr(publication_handlers, "datetime", FrozenDateTime)
        database_path = tmp_path / "dance.sqlite3"
        current_settings = settings(database_path)
        session = next_session()
        store, dispatcher, bot = await start_app(database_path, current_settings, session)
        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=1,
                message_id=1,
                text="Название: Текущий джем\nДата: 27.11.2026\nТип: Джем",
            ),
        )
        confirmation = button_request(session, "Сохранить")
        await click_button(
            dispatcher,
            bot,
            session,
            confirmation,
            "Сохранить",
            update_id=2,
        )
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=3, message_id=2, text="/publish 23.11.2026"),
        )
        first_preview = button_request(session, "Опубликовать")
        await click_button(
            dispatcher,
            bot,
            session,
            first_preview,
            "Опубликовать",
            update_id=4,
        )
        original = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 23))
        assert original is not None and original.status == "delivered"
        initial_group_sends = len(chat_send_requests(session, GROUP_ID))

        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=5, message_id=3, text="/publish 23.11.2026"),
        )
        assert len(chat_send_requests(session, GROUP_ID)) == initial_group_sends
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=6, message_id=4, text="/republish"),
        )
        repeat_preview = button_request(session, "Повторить актуальный выпуск")
        repeat_callback = button_data(repeat_preview, "Повторить актуальный выпуск")
        await click_button(
            dispatcher,
            bot,
            session,
            repeat_preview,
            "Повторить актуальный выпуск",
            update_id=7,
        )
        after_confirmation = len(chat_send_requests(session, GROUP_ID))
        assert after_confirmation == initial_group_sends + len(original.parts)
        repeated = await store.get_republication_for_request(OWNER_ID, 4)
        assert repeated is not None and repeated.status == "delivered"
        assert repeated.source_publication_id == original.id
        assert await store.get_pending_republication(original.id) is None

        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=8, message_id=5, text="/republish"),
        )
        assert len(chat_send_requests(session, GROUP_ID)) == after_confirmation
        await dispatcher.feed_update(
            bot,
            Update(
                update_id=9,
                callback_query=CallbackQuery(
                    id="repeat-old-button",
                    from_user=User(id=OWNER_ID, is_bot=False, first_name="Владелец"),
                    chat_instance="flow-test",
                    message=Message(
                        message_id=bound_message_id(session, repeat_preview),
                        date=NOW,
                        chat=Chat(id=OWNER_ID, type="private"),
                        from_user=User(id=0, is_bot=True, first_name="Бот"),
                        text=repeat_preview.text,
                        reply_markup=repeat_preview.reply_markup,
                    ).as_(bot),
                    data=repeat_callback,
                ).as_(bot),
            ),
        )
        assert len(chat_send_requests(session, GROUP_ID)) == after_confirmation
        await stop_app(dispatcher, bot)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["partial", "unknown"])
def test_interrupted_multipart_publication_recovers_without_resending_successful_parts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    async def scenario() -> None:
        quiet_scheduler(monkeypatch)
        database_path = tmp_path / "dance.sqlite3"
        current_settings = settings(database_path)
        session = GroupSendFaultSession(
            OWNER_ID,
            failure=failure,
            fail_attempt=2,
        )
        store, dispatcher, bot = await start_app(database_path, current_settings, session)
        for index in (1, 2):
            template = (
                f"Название: Большой джем {index}\nДата: {26 + index}.11.2026\nТип: Джем\n"
                f"Описание: {'x' * 2500}"
            )
            await dispatcher.feed_update(
                bot,
                message_update(bot, update_id=index * 2 - 1, message_id=index, text=template),
            )
            draft = button_request(session, "Сохранить")
            await click_button(dispatcher, bot, session, draft, "Сохранить", update_id=index * 2)
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=5, message_id=3, text="/publish 23.11.2026"),
        )
        preview = button_request(session, "Опубликовать")
        await click_button(dispatcher, bot, session, preview, "Опубликовать", update_id=6)
        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 23))
        assert publication is not None
        assert len(publication.parts) == 2
        assert publication.status == ("partial" if failure == "partial" else "unknown")
        assert publication.parts[0].status == "sent"
        assert publication.parts[0].message_id is not None
        first_message_id = publication.parts[0].message_id
        assert publication.parts[1].status == ("failed" if failure == "partial" else "unknown")
        initial_group_count = len(chat_send_requests(session, GROUP_ID))
        assert initial_group_count == (1 if failure == "partial" else 2)
        await stop_app(dispatcher, bot)

        recovered_session = GroupSendFaultSession(
            OWNER_ID,
            failure=failure,
            fail_attempt=2,
            previous=session,
        )
        recovered_store, recovered_dispatcher, recovered_bot = await start_app(
            database_path,
            current_settings,
            recovered_session,
        )
        await recovered_dispatcher.feed_update(
            recovered_bot,
            message_update(
                recovered_bot,
                update_id=7,
                message_id=4,
                text="/publish 23.11.2026",
            ),
        )
        recovery_status = button_request(
            recovered_session,
            "Повторить только часть 2" if failure == "partial" else "Часть 2: доставлено",
        )
        await click_button(
            recovered_dispatcher,
            recovered_bot,
            recovered_session,
            recovery_status,
            "Повторить только часть 2" if failure == "partial" else "Часть 2: доставлено",
            update_id=8,
        )
        recovered = await recovered_store.get_publication(publication.id)
        assert recovered is not None
        assert recovered.status == "delivered"
        assert recovered.parts[0].status == "sent"
        assert recovered.parts[0].message_id == first_message_id
        assert recovered.snapshot == publication.snapshot
        group_messages = chat_send_requests(recovered_session, GROUP_ID)
        assert len(group_messages) == 2
        assert len(recovered_session.group_attempts) == (3 if failure == "partial" else 2)
        assert recovered.parts[1].status == "sent"
        assert group_messages[0].text != group_messages[1].text
        if failure == "unknown":
            assert recovered.parts[1].message_id is None
        attempts_after_recovery = len(recovered_session.group_attempts)
        await click_button(
            recovered_dispatcher,
            recovered_bot,
            recovered_session,
            recovery_status,
            "Повторить только часть 2" if failure == "partial" else "Часть 2: доставлено",
            update_id=9,
        )
        await recovered_dispatcher.feed_update(
            recovered_bot,
            message_update(recovered_bot, update_id=10, message_id=5, text="/publish 23.11.2026"),
        )
        assert len(recovered_session.group_attempts) == attempts_after_recovery
        assert len(chat_send_requests(recovered_session, GROUP_ID)) == 2
        await stop_app(recovered_dispatcher, recovered_bot)

    asyncio.run(scenario())


def test_settings_commands_work_before_start_and_preserve_changes_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        quiet_scheduler(monkeypatch)
        database_path = tmp_path / "dance.sqlite3"
        current_settings = settings(database_path).model_copy(
            update={"weekly_publication_time": time(9, 0)}
        )
        session = next_session()
        store, dispatcher, bot = await start_app(database_path, current_settings, session)

        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=1, message_id=1, text="/settings"),
        )
        initial_panel = button_request(session, "Изменить время")
        assert "09:00" in initial_panel.text
        await click_button(
            dispatcher,
            bot,
            session,
            initial_panel,
            "Изменить время",
            update_id=2,
        )
        prompt = sent_requests(session)[-1]
        assert isinstance(prompt, SendMessage) and prompt.reply_markup is not None

        await dispatcher.feed_update(
            bot,
            message_update(
                bot,
                update_id=3,
                message_id=2,
                text="08:45",
                reply_to=Message(
                    message_id=bound_message_id(session, prompt),
                    date=NOW,
                    chat=Chat(id=OWNER_ID, type="private"),
                    from_user=User(id=0, is_bot=True, first_name="Бот"),
                    text=prompt.text,
                ),
            ),
        )
        changed = await store.get_weekly_publication_setting()
        assert changed.weekly_publication_time == time(8, 45)
        assert changed.revision == 1
        assert await store.list_sources() == []
        assert await store.list_drafts() == []
        await stop_app(dispatcher, bot)

        session = next_session(previous=session)
        store, dispatcher, bot = await start_app(database_path, current_settings, session)
        assert await store.get_weekly_publication_setting() == changed

        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=4, message_id=3, text="/settings"),
        )
        assert "08:45" in button_request(session, "Изменить время").text
        await click_button(
            dispatcher,
            bot,
            session,
            button_request(session, "Отключить автопубликацию"),
            "Отключить автопубликацию",
            update_id=5,
        )
        disabled = await store.get_weekly_publication_setting()
        assert disabled.weekly_publication_time is None
        assert disabled.revision == 2
        assert await store.list_sources() == []
        assert await store.list_drafts() == []
        await stop_app(dispatcher, bot)

        session = next_session(previous=session)
        store, dispatcher, bot = await start_app(database_path, current_settings, session)
        assert await store.get_weekly_publication_setting() == disabled
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=6, message_id=4, text="/settings"),
        )
        assert "автопубликация отключена" in button_request(session, "Изменить время").text
        await stop_app(dispatcher, bot)

    asyncio.run(scenario())


def test_stranger_and_owner_group_cannot_mutate_through_messages_or_bound_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        from dance_community_bot.cards import show_record
        from dance_community_bot.schedule_handlers import show_schedule_week
        from dance_community_bot.settings_handlers import settings_command_handler

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:
                frozen = datetime(2026, 11, 25, 10, tzinfo=ZoneInfo("Europe/Moscow"))
                return frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)

        quiet_scheduler(monkeypatch)
        monkeypatch.setattr(publication_handlers, "datetime", FrozenDateTime)
        database_path = tmp_path / "dance.sqlite3"
        current_settings = settings(database_path).model_copy(
            update={"weekly_publication_time": time(9, 0)}
        )
        session = next_session()
        store, dispatcher, bot = await start_app(database_path, current_settings, session)

        for update_id, message_id, template in (
            (1, 1, EVENT_TEMPLATE),
            (
                3,
                2,
                EVENT_TEMPLATE.replace("27.11.2026", "03.12.2026").replace(
                    "Popping Jam", "December Jam"
                ),
            ),
            (
                5,
                3,
                "Название: Popping\nПервая дата: 24.11.2026\nВремя: 19:00\nМесто: Зал A",
            ),
        ):
            await dispatcher.feed_update(
                bot,
                message_update(bot, update_id=update_id, message_id=message_id, text=template),
            )
            await click_button(
                dispatcher,
                bot,
                session,
                button_request(session, "Сохранить"),
                "Сохранить",
                update_id=update_id + 1,
            )

        records = await store.list_records()
        first_event = next(record for record in records if record.data.title == "Popping Jam")
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=7, message_id=4, text="/events"),
        )
        private_cancel = next(
            request
            for request in reversed(sent_requests(session))
            if isinstance(request, SendMessage)
            and request.chat_id == OWNER_ID
            and "Название: Popping Jam" in request.text
            and request.reply_markup is not None
            and any(
                button.text == "Отменить"
                for row in request.reply_markup.inline_keyboard
                for button in row
            )
        )
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=8, message_id=5, text="/schedule 23.11.2026"),
        )
        private_transfer = next(
            request
            for request in reversed(sent_requests(session))
            if isinstance(request, SendMessage)
            and request.chat_id == OWNER_ID
            and "Проведение · 24.11.2026" in request.text
            and request.reply_markup is not None
            and any(
                button.text == "Перенести"
                for row in request.reply_markup.inline_keyboard
                for button in row
            )
        )
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=9, message_id=6, text="/settings"),
        )
        private_settings = button_request(session, "Отключить автопубликацию")
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=10, message_id=7, text="/publish 23.11.2026"),
        )
        first_week_preview = button_request(session, "Опубликовать")
        await click_button(
            dispatcher,
            bot,
            session,
            first_week_preview,
            "Опубликовать",
            update_id=11,
        )
        delivered_publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 23))
        assert delivered_publication is not None and delivered_publication.status == "delivered"
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=12, message_id=8, text="/publish 30.11.2026"),
        )
        private_publication = button_request(session, "Опубликовать")
        await dispatcher.feed_update(
            bot,
            message_update(bot, update_id=13, message_id=9, text="/republish"),
        )
        private_republication = button_request(session, "Повторить актуальный выпуск")
        republished_state = await store.get_republication_for_request(OWNER_ID, 9)
        assert republished_state is not None and republished_state.status == "prepared"

        # Посторонний не может подтвердить настоящие привязанные выпуски.
        before_private_callback_db = database_path.read_bytes()
        before_private_callback_requests = tuple(session.requests)
        await click_button(
            dispatcher,
            bot,
            session,
            private_publication,
            "Опубликовать",
            update_id=14,
            user_id=333,
        )
        await click_button(
            dispatcher,
            bot,
            session,
            private_republication,
            "Повторить актуальный выпуск",
            update_id=15,
            user_id=333,
        )
        assert database_path.read_bytes() == before_private_callback_db
        assert len(session.requests) == len(before_private_callback_requests)
        assert all(
            current is previous
            for current, previous in zip(
                session.requests, before_private_callback_requests, strict=True
            )
        )

        group_publication_state = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 30))
        assert group_publication_state is not None and group_publication_state.status == "prepared"

        def ui_target(message_id: int, *, chat_id: int, chat_type: str) -> Message:
            return Message(
                message_id=message_id,
                date=NOW,
                chat=Chat(id=chat_id, type=chat_type),
                from_user=User(id=OWNER_ID, is_bot=False, first_name="Владелец"),
                text="Подготовка связанного интерфейса",
            ).as_(bot)

        group_target = ui_target(80, chat_id=GROUP_ID, chat_type="supergroup")
        await show_record(group_target, first_event, store)
        await show_schedule_week(group_target, store, date(2026, 11, 23))
        await settings_command_handler(group_target, store)
        await publication_handlers._send_bound_status(group_target, store, group_publication_state)
        await publication_handlers._send_bound_status(group_target, store, republished_state)

        def bound_request(label: str, chat_id: int, text_fragment: str = "") -> SendMessage:
            for request in reversed(sent_requests(session)):
                if (
                    isinstance(request, SendMessage)
                    and request.chat_id == chat_id
                    and text_fragment in request.text
                    and request.reply_markup is not None
                    and any(
                        button.text == label
                        for row in request.reply_markup.inline_keyboard
                        for button in row
                    )
                ):
                    return request
            raise AssertionError(f"Не найдено связанное сообщение «{label}» для {chat_id}.")

        group_cancel = bound_request("Отменить", GROUP_ID, "Название: Popping Jam")
        group_transfer = bound_request("Перенести", GROUP_ID, "Проведение · 24.11.2026")
        group_settings = bound_request("Отключить автопубликацию", GROUP_ID)
        group_publication = bound_request("Опубликовать", GROUP_ID)
        group_republication = bound_request("Повторить актуальный выпуск", GROUP_ID)

        # Команды и ввод владельца работают без предварительного /start.
        records_before = await store.list_records()
        assert len(records_before) == 3
        assert "09:00" in private_settings.text
        assert private_publication.reply_markup is not None
        group_send_count_before = len(chat_send_requests(session, GROUP_ID))
        request_snapshot = tuple(session.requests)
        database_snapshot = database_path.read_bytes()
        poster_root = database_path.parent / "posters"
        poster_snapshot = {
            path.relative_to(poster_root): path.read_bytes()
            for path in poster_root.rglob("*")
            if path.is_file()
        }
        sources_before = await store.list_sources()
        drafts_before = await store.list_drafts()
        schedule_before = await store.get_schedule_snapshot()
        settings_before = await store.get_weekly_publication_setting()
        publications_before = [
            await store.get_publication(publication_id)
            for publication_id in (
                delivered_publication.id,
                group_publication_state.id,
                republished_state.id,
            )
        ]

        source_template = "Название: Untrusted Jam\nДата: 05.12.2026\nТип: Джем"
        for user_id, chat_id, chat_type, update_offset in (
            (333, OWNER_ID, "private", 100),
            (OWNER_ID, GROUP_ID, "supergroup", 200),
        ):
            for index, text in enumerate(
                (
                    "/start",
                    "/help",
                    "/event",
                    "/events",
                    "/schedule 23.11.2026",
                    "/settings",
                    "/publish 23.11.2026",
                    "/republish",
                    "/preview 23.11.2026",
                    "/status",
                    "/class",
                    source_template,
                )
            ):
                await dispatcher.feed_update(
                    bot,
                    message_update(
                        bot,
                        update_id=update_offset + index,
                        message_id=100 + update_offset + index,
                        text=text,
                        user_id=user_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                    ),
                )
            await dispatcher.feed_update(
                bot,
                message_update(
                    bot,
                    update_id=update_offset + 20,
                    message_id=120 + update_offset,
                    caption=source_template,
                    photo_file_id=f"unauthorized-poster-{user_id}",
                    user_id=user_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                ),
            )

        await click_button(
            dispatcher,
            bot,
            session,
            private_cancel,
            "Отменить",
            update_id=301,
            user_id=333,
        )
        await click_button(
            dispatcher,
            bot,
            session,
            private_transfer,
            "Перенести",
            update_id=302,
            user_id=333,
        )
        await click_button(
            dispatcher,
            bot,
            session,
            private_settings,
            "Отключить автопубликацию",
            update_id=303,
            user_id=333,
        )
        await click_button(
            dispatcher,
            bot,
            session,
            private_publication,
            "Опубликовать",
            update_id=304,
            user_id=333,
        )
        await click_button(
            dispatcher,
            bot,
            session,
            group_cancel,
            "Отменить",
            update_id=305,
            chat_id=GROUP_ID,
            chat_type="supergroup",
        )
        await click_button(
            dispatcher,
            bot,
            session,
            group_transfer,
            "Перенести",
            update_id=306,
            chat_id=GROUP_ID,
            chat_type="supergroup",
        )
        await click_button(
            dispatcher,
            bot,
            session,
            group_settings,
            "Отключить автопубликацию",
            update_id=307,
            chat_id=GROUP_ID,
            chat_type="supergroup",
        )
        await click_button(
            dispatcher,
            bot,
            session,
            group_publication,
            "Опубликовать",
            update_id=308,
            chat_id=GROUP_ID,
            chat_type="supergroup",
        )
        await click_button(
            dispatcher,
            bot,
            session,
            group_republication,
            "Повторить актуальный выпуск",
            update_id=309,
            chat_id=GROUP_ID,
            chat_type="supergroup",
        )

        assert database_path.read_bytes() == database_snapshot
        assert {
            path.relative_to(poster_root): path.read_bytes()
            for path in poster_root.rglob("*")
            if path.is_file()
        } == poster_snapshot
        assert await store.list_sources() == sources_before
        assert await store.list_drafts() == drafts_before
        assert await store.list_records() == records_before
        assert await store.get_schedule_snapshot() == schedule_before
        assert await store.get_weekly_publication_setting() == settings_before
        assert len(chat_send_requests(session, GROUP_ID)) == group_send_count_before
        assert len(session.requests) == len(request_snapshot)
        assert all(
            current is previous
            for current, previous in zip(session.requests, request_snapshot, strict=True)
        )
        assert not any(
            isinstance(request, GetFile) and "unauthorized" in request.file_id
            for request in session.requests
        )
        publications = [
            await store.get_publication_for_week(GROUP_ID, week_start)
            for week_start in (date(2026, 11, 23), date(2026, 11, 30))
        ]
        assert all(
            publication is not None and publication.status in {"delivered", "prepared"}
            for publication in publications
        )
        assert publications == publications_before[:2]
        assert await store.get_publication(republished_state.id) == publications_before[2]
        assert len(await store.list_pending_publication_notifications()) == 0
        await stop_app(dispatcher, bot)

    asyncio.run(scenario())
