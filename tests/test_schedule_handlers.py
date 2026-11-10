from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User
from telegram_transport import RecordingSession

from dance_community_bot import schedule_handlers
from dance_community_bot.bot import create_dispatcher
from dance_community_bot.config import Settings
from dance_community_bot.intake import handle_card_action, ingest_message
from dance_community_bot.models import (
    CardBinding,
    ChangeSeries,
    ClassData,
    EndSeries,
    SourceInput,
    TransferOccurrence,
)
from dance_community_bot.schedule_handlers import (
    _schedule_draft_preview,
    parse_schedule_date,
    schedule_command_handler,
    show_schedule_week,
)
from dance_community_bot.storage import Store

TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
CHAT_ID = 222


@pytest.mark.parametrize(
    ("value", "expected"),
    [("10.11.2026", date(2026, 11, 10)), ("01.01.0001", date(1, 1, 1))],
)
def test_schedule_date_parser_accepts_only_full_real_dates(value: str, expected: date) -> None:
    assert parse_schedule_date(value) == expected


@pytest.mark.parametrize(
    ("value", "expected_error"),
    [
        ("1.11.2026", "Введите дату строго в формате ДД.ММ.ГГГГ."),
        ("10.11.26", "Введите дату строго в формате ДД.ММ.ГГГГ."),
        ("31.02.2026", "Такой календарной даты не существует."),
        ("завтра", "Введите дату строго в формате ДД.ММ.ГГГГ."),
    ],
)
def test_schedule_date_parser_rejects_noncanonical_or_relative_dates(
    value: str, expected_error: str
) -> None:
    with pytest.raises(ValueError) as error:
        parse_schedule_date(value)
    assert str(error.value) == expected_error


def test_long_schedule_card_binds_actions_only_to_last_delivered_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 11, 9, 9, tzinfo=UTC)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

        monkeypatch.setattr(schedule_handlers, "datetime", FrozenDateTime)
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "long-schedule.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=1,
                text="long class source",
                received_at=now,
            )
        )
        draft = await store.save_draft(
            source.id,
            ClassData(
                title="Popping",
                first_date=date(2026, 11, 10),
                start_time=time(19),
                description="x" * 4000,
            ),
            poster_source_id=None,
        )
        record = await store.confirm_draft(draft.id, draft.version)
        message = Message(
            message_id=2,
            date=now,
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        ).as_(bot)

        async def fail_before_second_series_chunk() -> bool:
            return len(session.messages) < 2

        session.before_send_check = fail_before_second_series_chunk
        try:
            with pytest.raises(AssertionError, match="Проверка перед отправкой"):
                await show_schedule_week(message, store, date(2026, 11, 9))
            assert len(session.requests) == 2
            assert session.requests[1].reply_markup is None

            session.before_send_check = None
            request_start = len(session.requests)
            message_count_before_retry = len(session.messages)
            await show_schedule_week(message, store, date(2026, 11, 9))
            delivered = session.requests[request_start:]
            series_chunks = [
                request
                for request in delivered
                if request.text.startswith("Серия №") or request.text.startswith("Описание:")
            ]
            assert len(series_chunks) == 2
            assert series_chunks[0].reply_markup is None
            assert series_chunks[1].reply_markup is not None
            end_button = next(
                button
                for row in series_chunks[1].reply_markup.inline_keyboard
                for button in row
                if button.text == "Завершить серию"
            )
            assert end_button.callback_data is not None
            action = await store.get_card_action(end_button.callback_data.removeprefix("a:"))
            assert action is not None
            assert action.bot_message_id == (
                100 + message_count_before_retry + delivered.index(series_chunks[1]) + 1
            )

            callback = CallbackQuery(
                id="end-from-last-chunk",
                from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
                chat_instance="schedule-test",
                data=end_button.callback_data,
                message=Message(
                    message_id=action.bot_message_id,
                    date=now,
                    chat=Chat(id=CHAT_ID, type="private"),
                    reply_markup=series_chunks[1].reply_markup,
                ).as_(bot),
            ).as_(bot)
            await handle_card_action(callback, store)
            assert "последней датой" in session.messages[-1]
            assert await store.get_record(record.id) is not None
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("schedule_action", ["transfer_occurrence", "end_series"])
def test_schedule_rejects_photo_replies_but_keeps_text_correction_context(
    tmp_path: Path, schedule_action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 11, 9, 9, tzinfo=UTC)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

        monkeypatch.setattr(schedule_handlers, "datetime", FrozenDateTime)
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / f"{schedule_action}.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(chat_id=CHAT_ID, message_id=1, received_at=now, text="class")
        )
        draft = await store.save_draft(
            source.id,
            ClassData(title="Popping", first_date=date(2026, 11, 10), start_time=time(19)),
            poster_source_id=None,
        )
        record = await store.confirm_draft(draft.id, draft.version)
        await store.bind_card_message(
            CardBinding(
                chat_id=CHAT_ID,
                bot_message_id=90,
                schedule_action=schedule_action,
                schedule_series_id=record.id,
                schedule_revision=record.revision,
                schedule_original_date=(
                    date(2026, 11, 10) if schedule_action == "transfer_occurrence" else None
                ),
            )
        )
        caption = (
            "Дата: 18.11.2026\nВремя: 20:30"
            if schedule_action == "transfer_occurrence"
            else "24.11.2026"
        )
        photo = Message(
            message_id=2,
            date=now,
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
            caption=caption,
            photo=[
                PhotoSize(
                    file_id="schedule-poster",
                    file_unique_id="schedule-poster",
                    width=20,
                    height=20,
                )
            ],
            reply_to_message=Message(
                message_id=90, date=now, chat=Chat(id=CHAT_ID, type="private")
            ),
        ).as_(bot)
        try:
            await ingest_message(photo, store)
            rejected_source = await store.get_source(CHAT_ID, 2)
            assert rejected_source is not None
            assert rejected_source.status == "failed"
            assert rejected_source.attachment_status == "saved"
            assert rejected_source.poster_path is not None
            assert (
                store.path.parent / rejected_source.poster_path
            ).read_bytes() == session.poster_bytes
            assert "фото" in (rejected_source.error_reason or "").lower()
            assert await store.list_pending_schedule_changes() == []

            sent = [request for request in session.requests if isinstance(request, SendMessage)]
            correction_prompt_id = 100 + len(sent)
            bound = await store.get_card_binding(CHAT_ID, correction_prompt_id)
            assert bound is not None
            assert bound.schedule_action == schedule_action

            correction_text = (
                "Дата: 18.11.2026\nВремя: 20:30"
                if schedule_action == "transfer_occurrence"
                else "24.11.2026"
            )
            correction = Message(
                message_id=3,
                date=now,
                chat=Chat(id=CHAT_ID, type="private"),
                from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
                text=correction_text,
                reply_to_message=Message(
                    message_id=correction_prompt_id,
                    date=now,
                    chat=Chat(id=CHAT_ID, type="private"),
                ),
            ).as_(bot)
            await ingest_message(correction, store)
            corrected_source = await store.get_source(CHAT_ID, 3)
            assert corrected_source is not None and corrected_source.status == "processed"
            assert corrected_source.telegram_file_id is None
            pending = await store.list_pending_schedule_changes()
            assert len(pending) == 1
            assert pending[0].change.source_id == corrected_source.id
            if schedule_action == "end_series":
                assert isinstance(pending[0].change, EndSeries)
        finally:
            await session.close()

    asyncio.run(scenario())


def test_long_schedule_reply_error_is_chunked_and_remains_correctable(tmp_path: Path) -> None:
    class RejectLongSendSession(RecordingSession):
        async def make_request(
            self,
            bot: Bot,
            method: TelegramMethod[TelegramType],
            timeout: int | None = None,
        ) -> TelegramType:
            if isinstance(method, SendMessage) and len(method.text.encode("utf-16-le")) // 2 > 4096:
                raise RuntimeError("Telegram rejected a message over 4096 UTF-16 units.")
            return await super().make_request(bot, method, timeout)

    async def scenario() -> None:
        now = datetime(2026, 11, 9, 9, tzinfo=UTC)
        session = RejectLongSendSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "long-schedule-error.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(chat_id=CHAT_ID, message_id=1, received_at=now, text="class")
        )
        draft = await store.save_draft(
            source.id,
            ClassData(title="Popping", first_date=date(2026, 11, 10), start_time=time(19)),
            poster_source_id=None,
        )
        record = await store.confirm_draft(draft.id, draft.version)
        await store.bind_card_message(
            CardBinding(
                chat_id=CHAT_ID,
                bot_message_id=90,
                schedule_action="change_series",
                schedule_series_id=record.id,
                schedule_revision=record.revision,
            )
        )
        invalid = (
            "Действует с: 01.12.2099\n"
            "Название: Popping\n"
            "Первая дата: 01.12.2099\n"
            "Время: 19:00\n" + "X: 1\n" * 500
        )
        assert len(invalid) < 4096
        message = Message(
            message_id=2,
            date=now,
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
            text=invalid,
            reply_to_message=Message(
                message_id=90, date=now, chat=Chat(id=CHAT_ID, type="private")
            ),
        ).as_(bot)
        try:
            await ingest_message(message, store)
            failed_source = await store.get_source(CHAT_ID, 2)
            assert failed_source is not None and failed_source.status == "failed"
            assert len(failed_source.error_reason or "") > 4096
            error_messages = [
                request for request in session.requests if isinstance(request, SendMessage)
            ]
            assert len(error_messages) > 1
            assert all(
                len(request.text.encode("utf-16-le")) // 2 <= 4096 for request in error_messages
            )
            correction_prompt_id = 100 + len(error_messages)
            binding = await store.get_card_binding(CHAT_ID, correction_prompt_id)
            assert binding is not None and binding.schedule_action == "change_series"

            correction = Message(
                message_id=3,
                date=now,
                chat=Chat(id=CHAT_ID, type="private"),
                from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
                text=(
                    "Действует с: 01.12.2099\n"
                    "Название: Popping\n"
                    "Первая дата: 01.12.2099\n"
                    "Время: 19:00"
                ),
                reply_to_message=Message(
                    message_id=correction_prompt_id,
                    date=now,
                    chat=Chat(id=CHAT_ID, type="private"),
                ),
            ).as_(bot)
            await ingest_message(correction, store)
            pending = await store.list_pending_schedule_changes()
            assert len(pending) == 1
            assert pending[0].change.source_id == 3
        finally:
            await session.close()

    asyncio.run(scenario())


def test_resolved_schedule_preview_recovers_after_send_failure_and_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "schedule-recovery.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(chat_id=CHAT_ID, message_id=1, received_at=now, text="class")
        )
        class_draft = await store.save_draft(
            source.id,
            ClassData(
                title="Popping",
                first_date=date(2026, 11, 10),
                start_time=time(19),
                place="Зал A",
            ),
            poster_source_id=None,
        )
        record = await store.confirm_draft(class_draft.id, class_draft.version)
        transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=record.id,
                expected_revision=record.revision,
                original_date=date(2026, 11, 24),
                new_date=date(2026, 11, 25),
                new_time=time(20),
            ),
            now,
        )
        await store.confirm_schedule_change(transfer.id, transfer.version, now)
        current_record = await store.get_record(record.id)
        assert current_record is not None
        change_source = await store.save_source(
            SourceInput(chat_id=CHAT_ID, message_id=2, received_at=now, text="change")
        )
        pending = await store.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=current_record.id,
                expected_revision=current_record.revision,
                effective_from=date(2026, 11, 18),
                data=ClassData(
                    title="Popping",
                    first_date=date(2026, 11, 24),
                    start_time=time(18),
                    place="Зал B",
                ),
                source_id=change_source.id,
            ),
            now,
        )
        assert len(pending.affected_exceptions) == 1
        message = Message(
            message_id=10,
            date=now,
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        ).as_(bot)
        await _schedule_draft_preview(message, pending, store)
        resolution_message = next(
            request
            for request in session.requests
            if isinstance(request, SendMessage)
            and request.reply_markup is not None
            and any(
                button.text == "Снять исключение"
                for row in request.reply_markup.inline_keyboard
                for button in row
            )
        )
        markup = resolution_message.reply_markup
        assert markup is not None
        remove_button = next(
            button
            for row in markup.inline_keyboard
            for button in row
            if button.text == "Снять исключение"
        )
        assert remove_button.callback_data is not None
        action = await store.get_card_action(remove_button.callback_data.removeprefix("a:"))
        assert action is not None and action.bot_message_id is not None
        callback = CallbackQuery(
            id="resolution-send-failure",
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
            chat_instance="schedule-test",
            data=remove_button.callback_data,
            message=Message(
                message_id=action.bot_message_id,
                date=now,
                chat=Chat(id=CHAT_ID, type="private"),
                reply_markup=markup,
            ).as_(bot),
        ).as_(bot)

        fail_next = True

        async def fail_first_preview_send() -> bool:
            nonlocal fail_next
            if fail_next:
                fail_next = False
                return False
            return True

        session.before_send_check = fail_first_preview_send
        with pytest.raises(AssertionError, match="Проверка перед отправкой"):
            await handle_card_action(callback, store)
        after_failure = await store.list_pending_schedule_changes()
        assert len(after_failure) == 1
        assert after_failure[0].version == pending.version + 1
        assert after_failure[0].change.resolutions[date(2026, 11, 24)] == "remove"

        session.before_send_check = None
        await handle_card_action(callback, store)
        after_retry = await store.list_pending_schedule_changes()
        assert len(after_retry) == 1
        assert after_retry[0].version == after_failure[0].version
        completed_action = await store.get_card_action(action.token)
        assert completed_action is not None and completed_action.completed

        restarted = Store(store.path)
        await restarted.initialize()
        session.requests.clear()
        recovery_message = Message(
            message_id=11,
            date=now,
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
            text="/schedule 02.11.2026",
        ).as_(bot)
        await schedule_command_handler(recovery_message, restarted)
        recovery_text = "\n".join(
            request.text for request in session.requests if isinstance(request, SendMessage)
        )
        assert "На этой неделе нет регулярных проведений." in recovery_text
        assert "Предпросмотр · новое расписание" in recovery_text
        assert "Зал B" in recovery_text

        stale_action = await restarted.create_card_action(
            "schedule_confirm",
            schedule_draft_id=pending.id,
            schedule_draft_version=pending.version,
        )
        await restarted.bind_card_actions(CHAT_ID, 999, [stale_action.token])
        stale_callback = CallbackQuery(
            id="stale-schedule-confirm",
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
            chat_instance="schedule-test",
            data=f"a:{stale_action.token}",
            message=Message(
                message_id=999,
                date=now,
                chat=Chat(id=CHAT_ID, type="private"),
            ).as_(bot),
        ).as_(bot)
        await handle_card_action(stale_callback, restarted)
        still_pending = await restarted.list_pending_schedule_changes()
        assert len(still_pending) == 1
        assert still_pending[0].version == after_failure[0].version
        assert "устарел" in session.messages[-1].lower()
        current_record = await restarted.get_record(record.id)
        assert current_record is not None and current_record.revision == record.revision
        await session.close()

    asyncio.run(scenario())


def test_schedule_shows_moved_occurrence_in_origin_and_actual_weeks_with_bound_action(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=1,
                text="Регулярное занятие",
                received_at=datetime(2026, 11, 9, 9, tzinfo=UTC),
            )
        )
        draft = await store.save_draft(
            source.id,
            ClassData(
                title="Popping",
                first_date=date(2026, 11, 10),
                start_time=time(19),
                place="Зал A",
            ),
            poster_source_id=None,
        )
        record = await store.confirm_draft(draft.id, draft.version)
        now = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        change = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=record.id,
                expected_revision=record.revision,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 18),
                new_time=time(20, 30),
            ),
            now,
        )
        await store.confirm_schedule_change(change.id, change.version, now)
        message = Message(
            message_id=2,
            date=now,
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
        ).as_(bot)

        await show_schedule_week(message, store, date(2026, 11, 9))
        origin_text = "\n".join(
            request.text for request in session.requests if isinstance(request, SendMessage)
        )
        assert "10.11.2026" in origin_text
        assert "18.11.2026" in origin_text
        occurrence_message = next(
            request
            for request in session.requests
            if isinstance(request, SendMessage)
            and request.reply_markup is not None
            and any(
                button.text == "Убрать исключение"
                for row in request.reply_markup.inline_keyboard
                for button in row
            )
        )
        origin_markup = occurrence_message.reply_markup
        assert origin_markup is not None
        remove_button = next(
            button
            for row in origin_markup.inline_keyboard
            for button in row
            if button.text == "Убрать исключение"
        )
        assert remove_button.callback_data is not None
        action = await store.get_card_action(remove_button.callback_data.removeprefix("a:"))
        assert action is not None
        assert action.bot_message_id is not None
        send_messages = [
            request for request in session.requests if isinstance(request, SendMessage)
        ]
        assert action.bot_message_id == 100 + send_messages.index(occurrence_message) + 1
        assert (
            action.action,
            action.chat_id,
            action.series_id,
            action.schedule_revision,
            action.original_date,
            action.expected_exception_revision,
        ) == (
            "schedule_remove_exception",
            CHAT_ID,
            record.id,
            record.revision,
            date(2026, 11, 10),
            1,
        )

        session.requests.clear()
        await show_schedule_week(
            message.model_copy(update={"message_id": 3}), store, date(2026, 11, 16)
        )
        actual_text = "\n".join(
            request.text for request in session.requests if isinstance(request, SendMessage)
        )
        assert "10.11.2026" in actual_text
        assert "18.11.2026" in actual_text
        await bot.session.close()

    asyncio.run(scenario())


def test_dispatcher_schedule_transfer_preview_and_confirmation(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dispatcher-schedule.sqlite3")
        await store.initialize()
        now = datetime(2026, 11, 10, 9, tzinfo=UTC)
        for message_id, title, first_date in (
            (1, "Popping", date(2026, 11, 10)),
            (2, "House", date(2026, 11, 11)),
        ):
            source = await store.save_source(
                SourceInput(
                    chat_id=CHAT_ID,
                    message_id=message_id,
                    text=title,
                    received_at=now,
                )
            )
            draft = await store.save_draft(
                source.id,
                ClassData(
                    title=title,
                    first_date=first_date,
                    start_time=time(19) if title == "Popping" else time(20, 30),
                    place="Зал A",
                ),
                poster_source_id=None,
            )
            await store.confirm_draft(draft.id, draft.version)

        dispatcher = create_dispatcher(
            Settings(
                telegram_bot_token=TOKEN,
                telegram_owner_id=CHAT_ID,
                telegram_webhook_url="https://dance.example.org/telegram/webhook",
                telegram_webhook_secret="test_webhook_secret",
                database_path=tmp_path / "dispatcher-schedule.sqlite3",
            ),
            store,
        )

        async def send_message(
            update_id: int,
            message_id: int,
            text: str,
            *,
            reply_to_message: Message | None = None,
        ) -> None:
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=update_id,
                    message=Message(
                        message_id=message_id,
                        date=now,
                        chat=Chat(id=CHAT_ID, type="private"),
                        from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
                        text=text,
                        reply_to_message=reply_to_message,
                    ),
                ),
            )

        def sent_id(request: SendMessage) -> int:
            sent = [item for item in session.requests if isinstance(item, SendMessage)]
            return 101 + sent.index(request)

        async def click(request: SendMessage, callback_data: str) -> None:
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=1000 + len(session.requests),
                    callback_query=CallbackQuery(
                        id=f"schedule-{len(session.requests)}",
                        from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
                        chat_instance="schedule-test",
                        message=Message(
                            message_id=sent_id(request),
                            date=now,
                            chat=Chat(id=CHAT_ID, type="private"),
                            text=request.text,
                            reply_markup=request.reply_markup,
                        ),
                        data=callback_data,
                    ),
                ),
            )

        await send_message(1, 10, "/schedule 09.11.2026")
        schedule_message = next(
            request
            for request in session.requests
            if isinstance(request, SendMessage)
            and request.reply_markup is not None
            and any(
                button.text == "Перенести"
                for row in request.reply_markup.inline_keyboard
                for button in row
            )
        )
        markup = schedule_message.reply_markup
        assert markup is not None
        transfer_button = next(
            button for row in markup.inline_keyboard for button in row if button.text == "Перенести"
        )
        assert transfer_button.callback_data is not None
        await click(schedule_message, transfer_button.callback_data)

        prompt = next(
            request
            for request in reversed(session.requests)
            if isinstance(request, SendMessage) and "новую дату и время" in request.text
        )
        await send_message(
            2,
            11,
            "Дата: 18.11.2026\nВремя: 20:30\nМесто: Зал B",
            reply_to_message=Message(
                message_id=sent_id(prompt),
                date=now,
                chat=Chat(id=CHAT_ID, type="private"),
                from_user=User(id=0, is_bot=True, first_name="Бот"),
                text=prompt.text,
            ),
        )

        preview = next(
            request
            for request in reversed(session.requests)
            if isinstance(request, SendMessage)
            and request.reply_markup is not None
            and any(
                button.text == "Подтвердить"
                for row in request.reply_markup.inline_keyboard
                for button in row
            )
        )
        assert "18.11.2026 в 20:30" in preview.text
        assert "Место: Зал B" in preview.text
        assert "Предупреждение: в это время уже есть проведение" in preview.text
        preview_markup = preview.reply_markup
        assert preview_markup is not None
        confirm_button = next(
            button
            for row in preview_markup.inline_keyboard
            for button in row
            if button.text == "Подтвердить"
        )
        assert confirm_button.callback_data is not None
        await click(preview, confirm_button.callback_data)

        _, exceptions = await store.get_schedule()
        moved = next(item for item in exceptions if item.original_date == date(2026, 11, 10))
        assert (moved.new_date, moved.new_time, moved.place) == (
            date(2026, 11, 18),
            time(20, 30),
            "Зал B",
        )
        await click(preview, confirm_button.callback_data)
        _, replayed_exceptions = await store.get_schedule()
        assert replayed_exceptions == exceptions
        await bot.session.close()

    asyncio.run(scenario())


def test_dispatcher_series_change_waits_for_exception_decision_and_removes_poster_by_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            frozen = datetime(2026, 11, 10, 9, tzinfo=UTC)
            return frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)

    monkeypatch.setattr(schedule_handlers, "datetime", FrozenDateTime)

    async def scenario() -> None:
        session = RecordingSession(CHAT_ID)
        bot = Bot(token=TOKEN, session=session)
        store = Store(tmp_path / "dispatcher-series.sqlite3")
        await store.initialize()
        now = datetime(2026, 11, 10, 9, tzinfo=UTC)
        source = await store.save_source(
            SourceInput(
                chat_id=CHAT_ID,
                message_id=1,
                text="Регулярное занятие",
                received_at=now,
                telegram_file_id="poster-file",
                is_photo=True,
            )
        )
        poster_path = Path("posters/series.jpg")
        await asyncio.to_thread((store.path.parent / poster_path).write_bytes, b"poster")
        await store.set_source_poster_path(source.id, poster_path)
        draft = await store.save_draft(
            source.id,
            ClassData(
                title="Popping",
                first_date=date(2026, 11, 10),
                start_time=time(19),
                place="Зал A",
            ),
            poster_source_id=source.id,
        )
        record = await store.confirm_draft(draft.id, draft.version)
        moved = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=record.id,
                expected_revision=record.revision,
                original_date=date(2026, 11, 24),
                new_date=date(2026, 11, 25),
                new_time=time(20),
            ),
            datetime(2026, 11, 10, 12, tzinfo=ZoneInfo("Europe/Moscow")),
        )
        await store.confirm_schedule_change(
            moved.id,
            moved.version,
            datetime(2026, 11, 10, 12, tzinfo=ZoneInfo("Europe/Moscow")),
        )
        dispatcher = create_dispatcher(
            Settings(
                telegram_bot_token=TOKEN,
                telegram_owner_id=CHAT_ID,
                telegram_webhook_url="https://dance.example.org/telegram/webhook",
                telegram_webhook_secret="test_webhook_secret",
                database_path=tmp_path / "dispatcher-series.sqlite3",
            ),
            store,
        )

        async def send_message(
            update_id: int,
            message_id: int,
            text: str,
            reply_to: SendMessage | None = None,
        ) -> None:
            sent_reply = (
                Message(
                    message_id=sent_id(reply_to),
                    date=now,
                    chat=Chat(id=CHAT_ID, type="private"),
                    from_user=User(id=0, is_bot=True, first_name="Бот"),
                    text=reply_to.text,
                )
                if reply_to is not None
                else None
            )
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=update_id,
                    message=Message(
                        message_id=message_id,
                        date=now,
                        chat=Chat(id=CHAT_ID, type="private"),
                        from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
                        text=text,
                        reply_to_message=sent_reply,
                    ),
                ),
            )

        def sent_id(request: SendMessage | None) -> int:
            assert request is not None
            sent = [item for item in session.requests if isinstance(item, SendMessage)]
            return 101 + sent.index(request)

        async def click(request: SendMessage, callback_data: str) -> None:
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=2000 + len(session.requests),
                    callback_query=CallbackQuery(
                        id=f"series-{len(session.requests)}",
                        from_user=User(id=CHAT_ID, is_bot=False, first_name="Владелец"),
                        chat_instance="schedule-series-test",
                        message=Message(
                            message_id=sent_id(request),
                            date=now,
                            chat=Chat(id=CHAT_ID, type="private"),
                            text=request.text,
                            reply_markup=request.reply_markup,
                        ),
                        data=callback_data,
                    ),
                ),
            )

        await send_message(1, 10, "/schedule 23.11.2026")
        series_message = next(
            request
            for request in session.requests
            if isinstance(request, SendMessage)
            and request.reply_markup is not None
            and any(
                button.text == "Убрать афишу"
                for row in request.reply_markup.inline_keyboard
                for button in row
            )
        )
        series_markup = series_message.reply_markup
        assert series_markup is not None
        remove_poster_button = next(
            button
            for row in series_markup.inline_keyboard
            for button in row
            if button.text == "Убрать афишу"
        )
        assert remove_poster_button.callback_data is not None
        await click(series_message, remove_poster_button.callback_data)
        prompt = next(
            request
            for request in reversed(session.requests)
            if isinstance(request, SendMessage) and "Снятие афиши" in request.text
        )
        await send_message(2, 11, "Действует с: 18.11.2026", prompt)

        preview_without_decision = next(
            request
            for request in reversed(session.requests)
            if isinstance(request, SendMessage)
            and "Выберите решение для каждого" in request.text
            and request.reply_markup is not None
        )
        unresolved_markup = preview_without_decision.reply_markup
        assert unresolved_markup is not None
        assert all(
            button.text != "Подтвердить изменение серии"
            for row in unresolved_markup.inline_keyboard
            for button in row
        )
        resolution_message = next(
            request
            for request in reversed(session.requests)
            if isinstance(request, SendMessage)
            and request.reply_markup is not None
            and any(
                button.text == "Снять исключение"
                for row in request.reply_markup.inline_keyboard
                for button in row
            )
        )
        resolution_markup = resolution_message.reply_markup
        assert resolution_markup is not None
        remove_exception_button = next(
            button
            for row in resolution_markup.inline_keyboard
            for button in row
            if button.text == "Снять исключение"
        )
        assert remove_exception_button.callback_data is not None
        await click(resolution_message, remove_exception_button.callback_data)

        final_preview = next(
            request
            for request in reversed(session.requests)
            if isinstance(request, SendMessage)
            and request.reply_markup is not None
            and any(
                button.text == "Подтвердить изменение серии"
                for row in request.reply_markup.inline_keyboard
                for button in row
            )
        )
        assert "снять исключение" in "\n".join(
            request.text for request in session.requests if isinstance(request, SendMessage)
        )
        final_markup = final_preview.reply_markup
        assert final_markup is not None
        confirm_button = next(
            button
            for row in final_markup.inline_keyboard
            for button in row
            if button.text == "Подтвердить изменение серии"
        )
        assert confirm_button.callback_data is not None
        await click(final_preview, confirm_button.callback_data)

        versions, exceptions = await store.get_schedule()
        current = await store.get_record(record.id)
        assert current is not None
        assert current.poster_source_id is None
        assert exceptions == []
        assert [
            (item.revision, item.effective_from, item.effective_until) for item in versions
        ] == [
            (1, date(2026, 11, 10), date(2026, 11, 18)),
            (2, date(2026, 11, 18), None),
        ]
        assert versions[0].poster_source_id == source.id
        assert versions[1].poster_source_id is None
        assert versions[1].data.first_date == date(2026, 11, 24)
        await bot.session.close()

    asyncio.run(scenario())
