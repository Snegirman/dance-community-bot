import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Generator, Mapping, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, PhotoSize, User
from telegram_transport import RecordingSession

from dance_community_bot.cards import show_source
from dance_community_bot.intake import handle_card_action, ingest_message
from dance_community_bot.models import (
    CardBinding,
    ChangeSeries,
    ClassData,
    EndSeries,
    EventData,
    RemoveException,
    SeriesVersion,
    SourceInput,
    TransferOccurrence,
)
from dance_community_bot.schedule import expand_schedule
from dance_community_bot.storage import Store

NOW = datetime(2026, 11, 9, 9, tzinfo=UTC)


async def saved_series(store: Store) -> int:
    await store.initialize()
    source = await store.save_source(
        SourceInput(chat_id=111, message_id=1, text="Занятие", received_at=NOW)
    )
    draft = await store.save_draft(
        source.id,
        ClassData(title="Popping", first_date=date(2026, 11, 10), start_time=time(19)),
        None,
    )
    record = await store.confirm_draft(draft.id, draft.version)
    return record.id


async def saved_poster_source(store: Store, database: Path, message_id: int, filename: str) -> int:
    source = await store.save_source(
        SourceInput(
            chat_id=111,
            message_id=message_id,
            text=filename,
            received_at=NOW,
            telegram_file_id=filename,
            is_photo=True,
        )
    )
    poster_path = Path("posters") / filename
    full_path = database.parent / poster_path
    await asyncio.to_thread(full_path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(full_path.write_bytes, b"poster")
    await store.set_source_poster_path(source.id, poster_path)
    return source.id


def test_maximum_last_date_does_not_break_an_ordinary_week() -> None:
    version = SeriesVersion(
        series_id=1,
        revision=1,
        effective_from=date(2026, 11, 10),
        data=ClassData(
            title="Popping",
            first_date=date(2026, 11, 10),
            last_date=date.max,
            start_time=time(19),
        ),
    )

    occurrences = expand_schedule([version], [], date(2026, 11, 9))

    assert [item.actual_date for item in occurrences] == [date(2026, 11, 10)]


def test_unrepresentable_week_end_gives_domain_error() -> None:
    with pytest.raises(ValueError, match="Границы недели"):
        expand_schedule([], [], date(9999, 12, 27))


@pytest.mark.parametrize(
    ("last_date", "expected_dates"),
    [(date(2026, 11, 9), []), (date(2026, 11, 11), [date(2026, 11, 10)])],
)
def test_series_can_end_on_a_day_without_an_occurrence(
    tmp_path: Path, last_date: date, expected_dates: list[date]
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        series_id = await saved_series(store)
        draft = await store.propose_schedule_change(
            EndSeries(
                action="end_series",
                series_id=series_id,
                expected_revision=1,
                last_date=last_date,
            ),
            NOW,
        )
        await store.confirm_schedule_change(draft.id, draft.version, NOW)
        versions, exceptions = await store.get_schedule()
        assert [
            item.actual_date for item in expand_schedule(versions, exceptions, date(2026, 11, 9))
        ] == expected_dates
        assert expand_schedule(versions, exceptions, date(2026, 11, 16)) == []

    asyncio.run(scenario())


def test_correcting_saved_schedule_source_keeps_series_and_original_poster(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        series_id = await saved_series(store)
        session = RecordingSession(111)
        bot = Bot(token="987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno", session=session)
        await store.bind_card_message(
            CardBinding(
                chat_id=111,
                bot_message_id=101,
                schedule_action="change_series",
                schedule_series_id=series_id,
                schedule_revision=1,
            )
        )
        chat = Chat(id=111, type="private")
        original = Message(
            message_id=2,
            date=NOW,
            chat=chat,
            caption="Ошибочный шаблон",
            photo=[PhotoSize(file_id="poster", file_unique_id="unique", width=20, height=20)],
            reply_to_message=Message(message_id=101, date=NOW, chat=chat),
        ).as_(bot)
        await ingest_message(original, store)
        source = await store.get_source(111, 2)
        assert source is not None and source.status == "failed"
        await show_source(original, source, store)
        source_card_id = 100 + len(session.messages)
        await ingest_message(
            Message(
                message_id=3,
                date=NOW,
                chat=chat,
                text="Ошибочная подпись",
                reply_to_message=Message(message_id=source_card_id, date=NOW, chat=chat),
            ).as_(bot),
            Store(store.path),
        )
        bad_text_source = await store.get_source(111, 3)
        assert bad_text_source is not None and bad_text_source.status == "failed"
        await show_source(
            Message(
                message_id=3,
                date=NOW,
                chat=chat,
                text="Ошибочная подпись",
                reply_to_message=Message(message_id=source_card_id, date=NOW, chat=chat),
            ).as_(bot),
            bad_text_source,
            Store(store.path),
        )
        bad_source_card_id = 100 + len(session.messages)
        corrected = Message(
            message_id=4,
            date=NOW,
            chat=chat,
            text=(
                "Действует с: 01.12.2099\nНазвание: Waving\nПервая дата: 01.12.2099\nВремя: 21:00"
            ),
            reply_to_message=Message(message_id=bad_source_card_id, date=NOW, chat=chat),
        ).as_(bot)
        await ingest_message(corrected, Store(store.path))
        actions = [
            await store.get_card_action(button.callback_data.removeprefix("a:"))
            for request in session.requests
            if isinstance(request, SendMessage) and request.reply_markup is not None
            for row in request.reply_markup.inline_keyboard
            for button in row
            if button.callback_data is not None and button.text == "Подтвердить изменение серии"
        ]
        assert len(actions) == 1
        action = actions[0]
        assert action is not None and action.schedule_draft_id is not None
        assert action.schedule_draft_version is not None
        await store.confirm_schedule_change(
            action.schedule_draft_id, action.schedule_draft_version, NOW
        )
        records = await store.list_records()
        assert len(records) == 1 and records[0].id == series_id
        assert records[0].data.title == "Waving"
        assert records[0].poster_source_id == source.id
        assert len(await store.list_drafts()) == 1
        original_source = await store.get_source_by_id(source.id)
        assert original_source is not None and original_source.status == "processed"
        assert original_source.poster_path is not None
        assert (tmp_path / original_source.poster_path).read_bytes() == session.poster_bytes
        await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("last_date", [None, date(2099, 11, 17), date(2099, 11, 25)])
def test_remove_poster_keeps_end_series_boundary_through_real_confirmation(
    tmp_path: Path,
    last_date: date | None,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize()
        poster = await store.save_source(
            SourceInput(
                chat_id=111,
                message_id=1,
                text="Исходная афиша",
                received_at=NOW,
                telegram_file_id="poster-file",
                is_photo=True,
            )
        )
        poster_path = Path("posters/class.jpg")
        full_poster_path = database.parent / poster_path
        await asyncio.to_thread(full_poster_path.write_bytes, b"saved poster")
        await store.set_source_poster_path(poster.id, poster_path)
        draft = await store.save_draft(
            poster.id,
            ClassData(
                title="Popping",
                first_date=date(2099, 11, 10),
                last_date=last_date,
                start_time=time(19),
                place="Зал A",
            ),
            poster_source_id=poster.id,
        )
        series = await store.confirm_draft(draft.id, draft.version)
        now = NOW
        end = await store.propose_schedule_change(
            EndSeries(
                action="end_series",
                series_id=series.id,
                expected_revision=1,
                last_date=date(2099, 11, 18),
            ),
            now,
        )
        await store.confirm_schedule_change(end.id, end.version, now)

        session = RecordingSession(111)
        bot = Bot(token="987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno", session=session)
        await store.bind_card_message(
            CardBinding(
                chat_id=111,
                bot_message_id=101,
                schedule_action="remove_poster",
                schedule_series_id=series.id,
                schedule_revision=2,
            )
        )
        chat = Chat(id=111, type="private")
        request = Message(
            message_id=2,
            date=NOW,
            chat=chat,
            text="Действует с: 11.11.2099",
            reply_to_message=Message(message_id=101, date=NOW, chat=chat),
        ).as_(bot)
        await ingest_message(request, store)

        confirm_request = next(
            request
            for request in session.requests
            if isinstance(request, SendMessage)
            and isinstance(request.reply_markup, InlineKeyboardMarkup)
            and any(
                button.text == "Подтвердить изменение серии"
                for row in request.reply_markup.inline_keyboard
                for button in row
            )
        )
        assert isinstance(confirm_request.reply_markup, InlineKeyboardMarkup)
        confirm_token = next(
            button.callback_data
            for row in confirm_request.reply_markup.inline_keyboard
            for button in row
            if button.text == "Подтвердить изменение серии"
        )
        assert confirm_token is not None
        action = await store.get_card_action(confirm_token.removeprefix("a:"))
        assert action is not None and action.bot_message_id is not None
        callback = CallbackQuery(
            id="confirm-remove-poster",
            from_user=User(id=111, is_bot=False, first_name="Владелец"),
            chat_instance="private",
            data=confirm_token,
            message=Message(
                message_id=action.bot_message_id,
                date=NOW,
                chat=chat,
            ).as_(bot),
        ).as_(bot)
        await handle_card_action(callback, Store(database))

        reopened = Store(database)
        records = await reopened.list_records()
        versions, exceptions = await reopened.get_schedule()
        assert len(records) == 1
        assert records[0].revision == 3
        assert isinstance(records[0].data, ClassData)
        assert records[0].data.last_date == min(last_date or date.max, date(2099, 11, 18))
        assert records[0].poster_source_id is None
        assert exceptions == []
        assert [
            (item.effective_from, item.effective_until, item.poster_source_id) for item in versions
        ] == [
            (date(2099, 11, 10), date(2099, 11, 11), poster.id),
            (date(2099, 11, 11), None, None),
        ]
        assert [
            item.actual_date for item in expand_schedule(versions, exceptions, date(2099, 11, 16))
        ] == [date(2099, 11, 17)]
        assert expand_schedule(versions, exceptions, date(2099, 11, 23)) == []
        assert expand_schedule(versions, exceptions, date(2099, 11, 30)) == []
        restored_poster = await reopened.get_source_by_id(poster.id)
        assert restored_poster is not None and restored_poster.attachment_status == "saved"
        assert full_poster_path.read_bytes() == b"saved poster"
        await bot.session.close()

    asyncio.run(scenario())


def test_repeated_detach_after_v3_migration_preserves_both_histories(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "legacy-v3.sqlite3"
        migration_dir = Path(__file__).parents[1] / "src" / "dance_community_bot" / "migrations"
        with closing(sqlite3.connect(database)) as connection:
            for migration_name in (
                "001_initial.sql",
                "002_intake.sql",
                "003_schedule.sql",
            ):
                connection.executescript(
                    (migration_dir / migration_name).read_text(encoding="utf-8")
                )
            connection.execute("PRAGMA user_version = 3")

        store = Store(database)
        series_source = await store.save_source(
            SourceInput(
                chat_id=111,
                message_id=1,
                text="Исходное занятие",
                received_at=NOW,
                telegram_file_id="poster-file",
                is_photo=True,
            )
        )
        poster_path = Path("posters/class.jpg")
        (database.parent / poster_path.parent).mkdir()
        await asyncio.to_thread((database.parent / poster_path).write_bytes, b"poster")
        await store.set_source_poster_path(series_source.id, poster_path)
        class_draft = await store.save_draft(
            series_source.id,
            ClassData(
                title="Popping",
                first_date=date(2026, 11, 10),
                start_time=time(19),
                place="Зал A",
            ),
            poster_source_id=series_source.id,
        )
        series = await store.confirm_draft(class_draft.id, class_draft.version)
        now = NOW
        first_transfer_source = await store.save_source(
            SourceInput(chat_id=111, message_id=2, text="Первый перенос", received_at=NOW)
        )
        transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series.id,
                expected_revision=1,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 18),
                new_time=time(20, 30),
                source_id=first_transfer_source.id,
            ),
            now,
        )
        await store.confirm_schedule_change(transfer.id, transfer.version, now)
        first_series_change = await store.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=series.id,
                expected_revision=1,
                effective_from=date(2026, 11, 17),
                data=ClassData(
                    title="Popping",
                    first_date=date(2026, 11, 17),
                    start_time=time(20),
                    place="Зал B",
                ),
            ),
            now,
        )
        await store.resolve_schedule_change(
            first_series_change.id,
            first_series_change.version,
            original_date=date(2026, 11, 10),
            resolution="detach",
        )
        await store.confirm_schedule_change(
            first_series_change.id, first_series_change.version + 1, now
        )
        first_event = next(
            record for record in await store.list_records() if isinstance(record.data, EventData)
        )
        await store.cancel_record(first_event.id, first_event.revision)

        exception_revision = await store.get_schedule_exception_revision(
            series.id, date(2026, 11, 10)
        )
        remove = await store.propose_schedule_change(
            RemoveException(
                action="remove_exception",
                series_id=series.id,
                expected_revision=2,
                expected_exception_revision=exception_revision,
                original_date=date(2026, 11, 10),
            ),
            now,
        )
        await store.confirm_schedule_change(remove.id, remove.version, now)
        exception_revision = await store.get_schedule_exception_revision(
            series.id, date(2026, 11, 10)
        )
        second_transfer_source = await store.save_source(
            SourceInput(chat_id=111, message_id=3, text="Второй перенос", received_at=NOW)
        )
        second_transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series.id,
                expected_revision=2,
                expected_exception_revision=exception_revision,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 18),
                new_time=time(21),
                source_id=second_transfer_source.id,
            ),
            now,
        )
        await store.confirm_schedule_change(second_transfer.id, second_transfer.version, now)

        migrated = Store(database)
        await migrated.initialize()
        await migrated.initialize()
        with closing(sqlite3.connect(database)) as connection:
            existing_links = connection.execute(
                "SELECT event_record_id FROM detached_occurrence_links"
            ).fetchall()
        assert existing_links == [(first_event.id,)]

        second_series_change = await migrated.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=series.id,
                expected_revision=2,
                effective_from=date(2026, 11, 18),
                data=ClassData(
                    title="Popping",
                    first_date=date(2026, 11, 24),
                    start_time=time(21),
                    place="Зал C",
                ),
            ),
            now,
        )
        detached_draft = await migrated.resolve_schedule_change(
            second_series_change.id,
            second_series_change.version,
            original_date=date(2026, 11, 10),
            resolution="detach",
        )
        await migrated.confirm_schedule_change(detached_draft.id, detached_draft.version, now)
        await migrated.confirm_schedule_change(detached_draft.id, detached_draft.version, now)

        records = await migrated.list_records()
        events = [record for record in records if isinstance(record.data, EventData)]
        assert len(events) == 2
        assert (
            next(record for record in events if record.id == first_event.id).status == "cancelled"
        )
        second_event = next(record for record in events if record.id != first_event.id)
        assert second_event.status == "active"
        assert (database.parent / poster_path).read_bytes() == b"poster"
        with closing(sqlite3.connect(database)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()
            links = connection.execute(
                "SELECT event_record_id, series_id, original_date, source_id, "
                "exception_source_id, poster_source_id "
                "FROM detached_occurrence_links ORDER BY event_record_id"
            ).fetchall()
            foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            history_count = connection.execute(
                "SELECT COUNT(*) FROM schedule_exception_history"
            ).fetchone()
        assert version == (4,)
        assert links == [
            (
                first_event.id,
                series.id,
                "2026-11-10",
                series_source.id,
                first_transfer_source.id,
                series_source.id,
            ),
            (
                second_event.id,
                series.id,
                "2026-11-10",
                series_source.id,
                second_transfer_source.id,
                series_source.id,
            ),
        ]
        assert foreign_key_violations == []
        assert history_count is not None and history_count[0] >= 6

    asyncio.run(scenario())


def test_schedule_poster_retry_after_restart_restores_the_same_change(tmp_path: Path) -> None:
    class FailedDownloadSession(RecordingSession):
        async def stream_content(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            timeout: int = 30,
            chunk_size: int = 65536,
            raise_for_status: bool = True,
        ) -> AsyncGenerator[bytes]:
            raise OSError("test download failure")
            yield b""

    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        series_id = await saved_series(store)
        session = FailedDownloadSession(111)
        bot = Bot(token="987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno", session=session)
        await store.bind_card_message(
            CardBinding(
                chat_id=111,
                bot_message_id=101,
                schedule_action="change_series",
                schedule_series_id=series_id,
                schedule_revision=1,
            )
        )
        chat = Chat(id=111, type="private")
        message = Message(
            message_id=2,
            date=NOW,
            chat=chat,
            caption=(
                "Действует с: 01.12.2099\nНазвание: Waving\nПервая дата: 01.12.2099\nВремя: 21:00"
            ),
            photo=[PhotoSize(file_id="poster", file_unique_id="unique", width=20, height=20)],
            reply_to_message=Message(message_id=101, date=NOW, chat=chat),
        ).as_(bot)
        await ingest_message(message, store)
        source = await store.get_source(111, 2)
        assert source is not None and source.attachment_status == "failed"
        retry = next(
            button.callback_data
            for request in session.requests
            if isinstance(request, SendMessage) and request.reply_markup is not None
            for row in request.reply_markup.inline_keyboard
            for button in row
            if button.text == "Повторить загрузку афиши"
        )
        assert retry is not None
        action = await store.get_card_action(retry.removeprefix("a:"))
        assert action is not None and action.bot_message_id is not None
        await bot.session.close()
        restored_session = RecordingSession(111)
        restored_bot = Bot(
            token="987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno", session=restored_session
        )
        callback = CallbackQuery(
            id="retry",
            from_user=User(id=111, is_bot=False, first_name="Владелец"),
            chat_instance="private",
            data=retry,
            message=Message(message_id=action.bot_message_id, date=NOW, chat=chat).as_(
                restored_bot
            ),
        ).as_(restored_bot)
        await handle_card_action(callback, Store(store.path))
        restored_source = await store.get_source_by_id(source.id)
        assert restored_source is not None and restored_source.attachment_status == "saved"
        assert restored_source.status == "processed"
        assert "новое расписание" in "\n".join(restored_session.messages)
        assert len(await store.list_records()) == 1
        assert len(await store.list_drafts()) == 1
        versions, _ = await store.get_schedule()
        assert len(versions) == 1 and versions[0].data.title == "Popping"
        await restored_bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("explicit_date", [False, True])
def test_reply_to_confirmed_class_uses_dated_schedule_and_keeps_photo(
    tmp_path: Path, explicit_date: bool
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        series_id = await saved_series(store)
        session = RecordingSession(111)
        bot = Bot(token="987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno", session=session)
        await store.bind_card_message(
            CardBinding(chat_id=111, bot_message_id=101, record_id=series_id, revision=1)
        )
        text = "Название: Waving\nПервая дата: 01.12.2099\nВремя: 21:00"
        if explicit_date:
            text = "Действует с: 01.12.2099\n" + text
        message = Message(
            message_id=2,
            date=NOW,
            chat=Chat(id=111, type="private"),
            caption=text,
            photo=[PhotoSize(file_id="poster", file_unique_id="unique", width=20, height=20)],
            reply_to_message=Message(message_id=101, date=NOW, chat=Chat(id=111, type="private")),
        ).as_(bot)

        await ingest_message(message, store)

        source = await store.get_source(111, 2)
        assert source is not None and source.text == text
        assert source.attachment_status == "saved" and source.poster_path is not None
        assert (tmp_path / source.poster_path).read_bytes() == session.poster_bytes
        assert len(await store.list_drafts()) == 1
        record = await store.get_record(series_id)
        assert record is not None and record.revision == 1
        if explicit_date:
            previews = [
                request
                for request in session.requests
                if isinstance(request, SendMessage) and "новое расписание" in request.text
            ]
            assert len(previews) == 1 and "01.12.2099" in previews[0].text
            assert source.status == "processed"
        else:
            assert source.status == "failed"
            assert "Действует с" in "\n".join(session.messages)
        await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("with_button_revisions", [False, True])
def test_schedule_read_keeps_one_snapshot_across_concurrent_confirmation(
    tmp_path: Path, with_button_revisions: bool
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        series_id = await saved_series(store)
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
        transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series_id,
                expected_revision=1,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 18),
                new_time=time(20),
            ),
            NOW,
        )
        await store.confirm_schedule_change(transfer.id, transfer.version, NOW)
        replacement = await store.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=series_id,
                expected_revision=1,
                effective_from=date(2026, 11, 17),
                data=ClassData(title="Waving", first_date=date(2026, 11, 17), start_time=time(21)),
                resolutions={date(2026, 11, 10): "remove"},
            ),
            NOW,
        )

        class InterleavedConnection(sqlite3.Connection):
            def execute(
                self, sql: str, parameters: Sequence[object] | Mapping[str, object] = ()
            ) -> sqlite3.Cursor:
                if "FROM schedule_exceptions WHERE archived = 0" in sql:
                    store._confirm_schedule_change_sync(replacement.id, replacement.version, NOW)
                return super().execute(sql, parameters)

        class InterleavedStore(Store):
            @contextmanager
            def _connection(self) -> Generator[sqlite3.Connection]:
                connection = sqlite3.connect(
                    self.path, isolation_level=None, factory=InterleavedConnection
                )
                connection.row_factory = sqlite3.Row
                try:
                    yield connection
                finally:
                    connection.close()

        reader = InterleavedStore(store.path)
        if with_button_revisions:
            versions, exceptions, revisions, records = await reader.get_schedule_snapshot()
            assert revisions[(series_id, date(2026, 11, 10))] == 1
            assert len(records) == 1 and records[0].revision == 1
        else:
            versions, exceptions = await reader.get_schedule()
        assert len(versions) == 1 and versions[0].effective_until is None
        assert len(exceptions) == 1 and exceptions[0].new_date == date(2026, 11, 18)
        assert expand_schedule(versions, exceptions, date(2026, 11, 9)) == []
        latest_versions, latest_exceptions = await store.get_schedule()
        assert len(latest_versions) == 2 and latest_exceptions == []

    asyncio.run(scenario())


def test_legacy_pending_class_edit_cannot_bypass_schedule_versions(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        series_id = await saved_series(store)
        source = await store.save_source(
            SourceInput(chat_id=111, message_id=2, text="Старая правка", received_at=NOW)
        )
        changed = ClassData(
            title="Изменение без даты действия",
            first_date=date(2026, 11, 10),
            start_time=time(21),
        )
        with closing(sqlite3.connect(store.path)) as connection:
            cursor = connection.execute(
                "INSERT INTO drafts (record_id, version, base_revision, data_type, data_json, "
                "source_id, status) VALUES (?, 2, 1, 'class', ?, ?, 'pending')",
                (series_id, changed.model_dump_json(), source.id),
            )
            legacy_draft_id = cursor.lastrowid
            connection.commit()
        assert legacy_draft_id is not None
        with pytest.raises(ValueError, match="расписани"):
            await store.confirm_draft(legacy_draft_id, 2)
        record = await store.get_record(series_id)
        assert record is not None and record.revision == 1 and record.data.title == "Popping"
        versions, _ = await store.get_schedule()
        assert len(versions) == 1 and versions[0].data.start_time == time(19)

    asyncio.run(scenario())


def test_end_preview_keeps_version_starting_on_the_inclusive_last_date(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        series_id = await saved_series(store)
        for revision, first_date in [(1, date(2026, 11, 17)), (2, date(2026, 11, 24))]:
            draft = await store.propose_schedule_change(
                ChangeSeries(
                    action="change_series",
                    series_id=series_id,
                    expected_revision=revision,
                    effective_from=first_date,
                    data=ClassData(title="Popping", first_date=first_date, start_time=time(20)),
                ),
                NOW,
            )
            await store.confirm_schedule_change(draft.id, draft.version, NOW)
        end = await store.propose_schedule_change(
            EndSeries(
                action="end_series",
                series_id=series_id,
                expected_revision=3,
                last_date=date(2026, 11, 17),
            ),
            NOW,
        )
        assert [item.effective_from for item in end.superseded_versions] == [date(2026, 11, 24)]
        await store.confirm_schedule_change(end.id, end.version, NOW)
        versions, exceptions = await store.get_schedule()
        week = expand_schedule(versions, exceptions, date(2026, 11, 16))
        assert [(item.actual_date, item.start_time) for item in week] == [
            (date(2026, 11, 17), time(20))
        ]

    asyncio.run(scenario())


def test_series_change_inherits_poster_from_timeline_on_non_occurrence_day(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize()
        first_poster_id = await saved_poster_source(store, database, 1, "first.jpg")
        initial_data = ClassData(title="Popping", first_date=date(2026, 11, 1), start_time=time(19))
        initial = await store.save_draft(
            first_poster_id, initial_data, poster_source_id=first_poster_id
        )
        series = await store.confirm_draft(initial.id, initial.version)

        future_poster_id = await saved_poster_source(store, database, 2, "future.jpg")
        future = await store.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=series.id,
                expected_revision=1,
                effective_from=date(2026, 12, 1),
                data=ClassData(title="Waving", first_date=date(2026, 12, 1), start_time=time(20)),
                poster_source_id=future_poster_id,
            ),
            NOW,
        )
        await store.confirm_schedule_change(future.id, future.version, NOW)

        changed = await store.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=series.id,
                expected_revision=2,
                effective_from=date(2026, 11, 10),
                data=ClassData(
                    title="Animation", first_date=date(2026, 11, 10), start_time=time(21)
                ),
            ),
            NOW,
        )
        await store.confirm_schedule_change(changed.id, changed.version, NOW)

        versions, _ = await Store(database).get_schedule()
        actual = next(version for version in versions if version.revision == 3)
        assert actual.poster_source_id == first_poster_id
        assert actual.source_id is None
        assert actual.effective_from == date(2026, 11, 10)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("initial_last_date", "expected_effective_until"),
    [(None, date(2026, 12, 1)), (date(2026, 11, 29), date(2026, 11, 30))],
)
def test_end_series_syncs_record_and_supersedes_future_version(
    tmp_path: Path,
    initial_last_date: date | None,
    expected_effective_until: date,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize()
        first_poster_id = await saved_poster_source(store, database, 1, "first.jpg")
        initial_data = ClassData(
            title="Popping",
            first_date=date(2026, 11, 1),
            last_date=initial_last_date,
            start_time=time(19),
        )
        initial = await store.save_draft(
            first_poster_id, initial_data, poster_source_id=first_poster_id
        )
        series = await store.confirm_draft(initial.id, initial.version)

        future_poster_id = await saved_poster_source(store, database, 2, "future.jpg")
        future = await store.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=series.id,
                expected_revision=1,
                effective_from=date(2026, 12, 1),
                data=ClassData(title="Waving", first_date=date(2026, 12, 1), start_time=time(20)),
                poster_source_id=future_poster_id,
            ),
            NOW,
        )
        await store.confirm_schedule_change(future.id, future.version, NOW)
        end = await store.propose_schedule_change(
            EndSeries(
                action="end_series",
                series_id=series.id,
                expected_revision=2,
                last_date=date(2026, 11, 30),
            ),
            NOW,
        )
        await store.confirm_schedule_change(end.id, end.version, NOW)

        reopened = Store(database)
        record = await reopened.get_record(series.id)
        assert record is not None
        assert record.data == initial_data
        assert record.poster_source_id == first_poster_id
        versions, exceptions = await reopened.get_schedule()
        assert [(item.effective_from, item.effective_until) for item in versions] == [
            (date(2026, 11, 1), expected_effective_until)
        ]
        assert expand_schedule(versions, exceptions, date(2026, 11, 30)) == []
        with closing(sqlite3.connect(database)) as connection:
            history = connection.execute(
                "SELECT revision, source_id, poster_source_id, status FROM schedule_versions "
                "WHERE series_id = ? ORDER BY revision",
                (series.id,),
            ).fetchall()
        assert history == [
            (1, first_poster_id, first_poster_id, "active"),
            (2, None, future_poster_id, "superseded"),
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("after_explicit_end", [False, True], ids=["gap", "after-end"])
def test_series_change_keeps_poster_outside_occurrence_interval(
    tmp_path: Path, after_explicit_end: bool
) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize()
        poster_id = await saved_poster_source(store, database, 1, "first.jpg")
        initial = await store.save_draft(
            poster_id,
            ClassData(title="Popping", first_date=date(2026, 11, 1), start_time=time(19)),
            poster_source_id=poster_id,
        )
        series = await store.confirm_draft(initial.id, initial.version)

        if after_explicit_end:
            end = await store.propose_schedule_change(
                EndSeries(
                    action="end_series",
                    series_id=series.id,
                    expected_revision=1,
                    last_date=date(2026, 11, 30),
                ),
                NOW,
            )
            await store.confirm_schedule_change(end.id, end.version, NOW)
            effective_from = date(2026, 12, 1)
            first_date = effective_from
            expected_revision = 2
        else:
            first_gap_version = await store.propose_schedule_change(
                ChangeSeries(
                    action="change_series",
                    series_id=series.id,
                    expected_revision=1,
                    effective_from=date(2026, 11, 10),
                    data=ClassData(
                        title="Waving", first_date=date(2026, 11, 17), start_time=time(20)
                    ),
                    poster_source_id=poster_id,
                ),
                NOW,
            )
            await store.confirm_schedule_change(
                first_gap_version.id, first_gap_version.version, NOW
            )
            effective_from = date(2026, 11, 11)
            first_date = date(2026, 11, 17)
            expected_revision = 2

        changed = await store.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=series.id,
                expected_revision=expected_revision,
                effective_from=effective_from,
                data=ClassData(title="Animation", first_date=first_date, start_time=time(21)),
            ),
            NOW,
        )
        await store.confirm_schedule_change(changed.id, changed.version, NOW)

        versions, _ = await Store(database).get_schedule()
        actual = next(version for version in versions if version.revision == 3)
        assert actual.poster_source_id == poster_id

    asyncio.run(scenario())


def test_schedule_resolution_replay_is_idempotent_and_pending_can_be_reopened(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        series_id = await saved_series(store)
        transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series_id,
                expected_revision=1,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 18),
                new_time=time(20),
            ),
            NOW,
        )
        await store.confirm_schedule_change(transfer.id, transfer.version, NOW)
        pending = await store.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=series_id,
                expected_revision=1,
                effective_from=date(2026, 11, 17),
                data=ClassData(title="Waving", first_date=date(2026, 11, 17), start_time=time(21)),
            ),
            NOW,
        )
        resolved = await store.resolve_schedule_change(
            pending.id,
            pending.version,
            original_date=date(2026, 11, 10),
            resolution="remove",
        )

        reopened = Store(store.path)
        replay = await reopened.resolve_schedule_change(
            pending.id,
            pending.version,
            original_date=date(2026, 11, 10),
            resolution="remove",
        )
        assert replay == resolved
        assert replay.version == pending.version + 1
        assert await reopened.list_pending_schedule_changes() == [resolved]

        with pytest.raises(ValueError, match="устарело"):
            await reopened.resolve_schedule_change(
                pending.id,
                resolved.version + 1,
                original_date=date(2026, 11, 10),
                resolution="remove",
            )
        with pytest.raises(ValueError, match="устарел"):
            await reopened.resolve_schedule_change(
                pending.id,
                pending.version,
                original_date=date(2026, 11, 10),
                resolution="detach",
            )
        with pytest.raises(ValueError, match="устарела"):
            await reopened.confirm_schedule_change(pending.id, pending.version, NOW)

        await reopened.confirm_schedule_change(resolved.id, resolved.version, NOW)
        assert await Store(store.path).list_pending_schedule_changes() == []
        with pytest.raises(ValueError):
            await reopened.resolve_schedule_change(
                pending.id,
                resolved.version,
                original_date=date(2026, 11, 10),
                resolution="remove",
            )

    asyncio.run(scenario())
