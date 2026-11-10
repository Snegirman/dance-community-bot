from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from telegram_transport import RecordingSession

from dance_community_bot import schedule_handlers
from dance_community_bot.bot import create_dispatcher
from dance_community_bot.config import Settings
from dance_community_bot.models import ClassData, SourceInput, TransferOccurrence
from dance_community_bot.schedule_handlers import parse_schedule_date, show_schedule_week
from dance_community_bot.storage import Store

TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
CHAT_ID = 222


@pytest.mark.parametrize(
    ("value", "expected"),
    [("10.11.2026", date(2026, 11, 10)), ("01.01.0001", date(1, 1, 1))],
)
def test_schedule_date_parser_accepts_only_full_real_dates(value: str, expected: date) -> None:
    assert parse_schedule_date(value) == expected


@pytest.mark.parametrize("value", ["1.11.2026", "10.11.26", "31.02.2026", "завтра"])
def test_schedule_date_parser_rejects_noncanonical_or_relative_dates(value: str) -> None:
    with pytest.raises(ValueError):
        parse_schedule_date(value)


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
