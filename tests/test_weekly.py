from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Generator, Mapping, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dance_community_bot.models import (
    CancelOccurrence,
    ClassData,
    EventData,
    PhotoPostPart,
    Record,
    SourceInput,
    TransferOccurrence,
    WeeklyPost,
)
from dance_community_bot.storage import Store
from dance_community_bot.weekly import build_weekly_post, week_bounds

NOW = datetime(2026, 11, 9, 9, tzinfo=UTC)
MOSCOW = ZoneInfo("Europe/Moscow")


def post_text(post: WeeklyPost) -> str:
    chunks: list[str] = []
    for part in post.parts:
        value = part.text if part.kind == "text" else part.caption
        if value is not None:
            chunks.append(value)
    return "\n".join(chunks)


async def add_event(
    store: Store,
    message_id: int,
    data: EventData,
    *,
    with_poster: bool = False,
) -> Record:
    source = await store.save_source(
        SourceInput(
            chat_id=111,
            message_id=message_id,
            text="Исходное сообщение",
            received_at=NOW,
            telegram_file_id="poster-file" if with_poster else None,
            is_photo=with_poster,
        )
    )
    if with_poster:
        poster_path = Path("posters") / f"{message_id}.jpg"
        await asyncio.to_thread((store.path.parent / poster_path).write_bytes, b"poster")
        await store.set_source_poster_path(source.id, poster_path)
    draft = await store.save_draft(
        source.id,
        data,
        poster_source_id=source.id if with_poster else None,
    )
    return await store.confirm_draft(draft.id, draft.version)


async def add_class(
    store: Store,
    message_id: int,
    data: ClassData,
    *,
    with_poster: bool = False,
) -> Record:
    source = await store.save_source(
        SourceInput(
            chat_id=111,
            message_id=message_id,
            text="Исходное сообщение занятия",
            received_at=NOW,
            telegram_file_id="poster-file" if with_poster else None,
            is_photo=with_poster,
        )
    )
    if with_poster:
        poster_path = Path("posters") / f"{message_id}.jpg"
        await asyncio.to_thread((store.path.parent / poster_path).write_bytes, b"poster")
        await store.set_source_poster_path(source.id, poster_path)
    draft = await store.save_draft(
        source.id,
        data,
        poster_source_id=source.id if with_poster else None,
    )
    return await store.confirm_draft(draft.id, draft.version)


def test_week_bounds_use_moscow_mondays_and_reject_unrepresentable_range() -> None:
    start, end = week_bounds(date(2026, 11, 15))
    assert start == datetime(2026, 11, 9, tzinfo=MOSCOW)
    assert end == datetime(2026, 11, 16, tzinfo=MOSCOW)
    assert start.tzinfo == MOSCOW
    assert end.tzinfo == MOSCOW

    year_start, year_end = week_bounds(date(2027, 1, 1))
    assert year_start == datetime(2026, 12, 28, tzinfo=MOSCOW)
    assert year_end == datetime(2027, 1, 4, tzinfo=MOSCOW)

    with pytest.raises(ValueError, match="границ|диапазон|представ"):
        week_bounds(date.max)


def test_event_ending_on_date_max_does_not_overflow(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_event(
            store,
            1,
            EventData(
                title="Конец года",
                event_type="Фестиваль",
                start_date=date(9999, 12, 26),
                end_date=date.max,
            ),
        )

        post = await build_weekly_post(date(9999, 12, 20), store)

        rendered = post_text(post)
        assert "Конец года" in rendered
        assert "31.12.9999" in rendered

    asyncio.run(scenario())


def test_multiday_event_is_in_each_intersecting_week_with_full_range(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_event(
            store,
            1,
            EventData(
                title="Фестиваль",
                event_type="Событие",
                start_date=date(2026, 11, 8),
                end_date=date(2026, 11, 10),
                price="8 ноября — 500 ₽\n9–10 ноября — 700 ₽",
            ),
        )

        previous_week = await build_weekly_post(date(2026, 11, 2), store)
        current_week = await build_weekly_post(date(2026, 11, 9), store)

        for post in (previous_week, current_week):
            rendered = post_text(post)
            assert rendered.count("Фестиваль") == 1
            assert "08.11.2026" in rendered
            assert "10.11.2026" in rendered
            assert "8 ноября — 500 ₽\n9–10 ноября — 700 ₽" in rendered
        assert len(previous_week.records) == 1
        assert len(current_week.records) == 1

    asyncio.run(scenario())


def test_events_sort_known_times_then_unknown_and_keep_full_price(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        event_date = date(2026, 11, 10)
        # IDs deliberately differ from chronological order; equal times keep ID order.
        for message_id, title, start_time in [
            (1, "Поздно", time(19)),
            (2, "Без времени", None),
            (3, "Рано", time(10)),
            (4, "Первое одинаковое", time(10)),
            (5, "Второе одинаковое", time(10)),
        ]:
            await add_event(
                store,
                message_id,
                EventData(
                    title=title,
                    event_type="Занятие",
                    start_date=event_date,
                    start_time=start_time,
                    price="Разово — 800 ₽\nАбонемент — 2 800 ₽; зрителям бесплатно",
                ),
            )

        post = await build_weekly_post(date(2026, 11, 9), store)
        rendered = post_text(post)
        ordered_titles = [
            "Рано",
            "Первое одинаковое",
            "Второе одинаковое",
            "Поздно",
            "Без времени",
        ]

        assert rendered.index("События") < rendered.index(ordered_titles[0])
        assert [rendered.index(title) for title in ordered_titles] == sorted(
            rendered.index(title) for title in ordered_titles
        )
        assert "Время: не указано" in rendered
        assert "Разово — 800 ₽\nАбонемент — 2 800 ₽; зрителям бесплатно" in rendered
        assert len(post.records) == 5

    asyncio.run(scenario())


def test_weekly_event_selection_uses_inclusive_monday_and_inclusive_sunday_bounds(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        event_specs = [
            (1, "Предыдущее воскресенье", date(2026, 11, 8), time(23, 59)),
            (2, "Воскресенье поздно", date(2026, 11, 15), time(23, 59)),
            (3, "Понедельник в полночь", date(2026, 11, 9), time.min),
            (4, "Следующий понедельник", date(2026, 11, 16), time.min),
            (5, "Воскресенье без времени", date(2026, 11, 15), None),
            (6, "Воскресенье утром", date(2026, 11, 15), time(8)),
        ]
        for message_id, title, event_date, start_time in event_specs:
            await add_event(
                store,
                message_id,
                EventData(
                    title=title,
                    event_type="Событие",
                    start_date=event_date,
                    start_time=start_time,
                ),
            )

        post = await build_weekly_post(date(2026, 11, 9), store)

        assert [record.data.title for record in post.records] == [
            "Понедельник в полночь",
            "Воскресенье утром",
            "Воскресенье поздно",
            "Воскресенье без времени",
        ]
        rendered = post_text(post)
        assert "Предыдущее воскресенье" not in rendered
        assert "Следующий понедельник" not in rendered
        assert "Время: не указано" in rendered

    asyncio.run(scenario())


def test_weekly_post_uses_schedule_transfers_and_skips_cancelled_occurrence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        popping = await add_class(
            store,
            1,
            ClassData(title="Popping", first_date=date(2026, 11, 10), start_time=time(19)),
        )
        waving = await add_class(
            store,
            2,
            ClassData(
                title="Waving",
                first_date=date(2026, 11, 18),
                last_date=date(2026, 11, 18),
                start_time=time(20),
            ),
        )
        transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=popping.id,
                expected_revision=popping.revision,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 18),
                new_time=time(20, 30),
            ),
            NOW,
        )
        await store.confirm_schedule_change(transfer.id, transfer.version, NOW)
        cancellation = await store.propose_schedule_change(
            CancelOccurrence(
                action="cancel_occurrence",
                series_id=waving.id,
                expected_revision=waving.revision,
                original_date=date(2026, 11, 18),
            ),
            NOW,
        )
        await store.confirm_schedule_change(cancellation.id, cancellation.version, NOW)

        post = await build_weekly_post(date(2026, 11, 16), store)

        assert [item.title for item in post.occurrences] == ["Popping", "Popping"]
        assert [(item.original_date, item.actual_date) for item in post.occurrences] == [
            (date(2026, 11, 17), date(2026, 11, 17)),
            (date(2026, 11, 10), date(2026, 11, 18)),
        ]
        rendered = post_text(post)
        assert rendered.count("Popping") == 2
        assert "Waving" not in rendered
        assert "Занятия" in rendered

    asyncio.run(scenario())


def test_pending_records_and_edits_do_not_replace_confirmed_weekly_facts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        record = await add_event(
            store,
            1,
            EventData(
                title="Подтверждённый джем",
                event_type="Джем",
                start_date=date(2026, 11, 14),
            ),
        )
        before = await build_weekly_post(date(2026, 11, 9), store)
        edit_source = await store.save_source(
            SourceInput(
                chat_id=111,
                message_id=2,
                text="Правка",
                received_at=NOW,
            )
        )
        await store.save_draft(
            edit_source.id,
            EventData(
                title="Черновик правки",
                event_type="Джем",
                start_date=date(2026, 11, 14),
            ),
            poster_source_id=None,
            record_id=record.id,
            base_revision=record.revision,
        )
        new_source = await store.save_source(
            SourceInput(
                chat_id=111,
                message_id=3,
                text="Новая черновая карточка",
                received_at=NOW,
            )
        )
        await store.save_draft(
            new_source.id,
            EventData(
                title="Новый черновик",
                event_type="Джем",
                start_date=date(2026, 11, 15),
            ),
            poster_source_id=None,
        )

        post = await build_weekly_post(date(2026, 11, 9), store)

        assert "Подтверждённый джем" in post_text(post)
        assert "Черновик правки" not in post_text(post)
        assert "Новый черновик" not in post_text(post)
        assert post.catalog_revision == before.catalog_revision
        assert [item.id for item in post.records] == [record.id]

        cancelled = await store.cancel_record(record.id, record.revision)
        after_cancel = await build_weekly_post(date(2026, 11, 9), store)
        assert cancelled.status == "cancelled"
        assert after_cancel.records == ()
        assert after_cancel.occurrences == ()
        assert after_cancel.parts == ()
        assert after_cancel.catalog_revision != post.catalog_revision

    asyncio.run(scenario())


def test_weekly_post_keeps_multiple_posters_and_roundtrips_complete_snapshot(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        first_description = "🪩<>&" * 260 + " Конец длинной подписи первой афиши."
        second_description = "Описание второй афиши сохранено полностью."
        final_description = "Текстовое событие после обеих афиш."
        first = await add_event(
            store,
            20,
            EventData(
                title="Первая афиша",
                event_type="Фестиваль",
                start_date=date(2026, 11, 10),
                description=first_description,
            ),
            with_poster=True,
        )
        second = await add_event(
            store,
            21,
            EventData(
                title="Вторая афиша",
                event_type="Джем",
                start_date=date(2026, 11, 11),
                description=second_description,
            ),
            with_poster=True,
        )
        final = await add_event(
            store,
            22,
            EventData(
                title="Событие только с текстом",
                event_type="Практика",
                start_date=date(2026, 11, 12),
                description=final_description,
            ),
        )

        post = await build_weekly_post(date(2026, 11, 9), store)
        photos = [part for part in post.parts if isinstance(part, PhotoPostPart)]
        visible_text = "".join(
            part.text if part.kind == "text" else part.caption for part in post.parts
        )

        assert [record.id for record in post.records] == [first.id, second.id, final.id]
        assert [(part.poster_source_id, part.poster_path) for part in photos] == [
            (first.poster_source_id, Path("posters/20.jpg")),
            (second.poster_source_id, Path("posters/21.jpg")),
        ]
        assert first_description in visible_text
        assert second_description in visible_text
        assert final_description in visible_text
        assert (
            visible_text.index("Первая афиша")
            < visible_text.index("Вторая афиша")
            < visible_text.index("Событие только с текстом")
        )
        assert post.parts.index(photos[-1]) < next(
            index
            for index, part in enumerate(post.parts)
            if part.kind == "text" and "Событие только с текстом" in part.text
        )

        restored = WeeklyPost.model_validate_json(post.model_dump_json())

        assert restored == post

    asyncio.run(scenario())


def test_long_text_and_photo_caption_respect_telegram_utf16_limits(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        long_description = "🪩<>&" * 1200 + "\nВторая строка\n<>&"
        record = await add_event(
            store,
            1,
            EventData(
                title="Jam",
                event_type="Джем",
                start_date=date(2026, 11, 14),
                description=long_description,
            ),
            with_poster=True,
        )

        post = await build_weekly_post(date(2026, 11, 9), store)

        text_parts = [part for part in post.parts if part.kind == "text"]
        photo_parts = [part for part in post.parts if isinstance(part, PhotoPostPart)]
        assert len(photo_parts) == 1
        assert photo_parts[0].poster_source_id == record.poster_source_id
        assert photo_parts[0].poster_path == Path("posters/1.jpg")
        assert len(photo_parts[0].caption.encode("utf-16-le")) // 2 <= 1024
        assert all(len(part.text.encode("utf-16-le")) // 2 <= 4096 for part in text_parts)
        complete_text = "".join(
            part.text if part.kind == "text" else part.caption for part in post.parts
        )
        assert long_description in complete_text
        assert "<>&" in complete_text
        assert "&lt;" not in complete_text

    asyncio.run(scenario())


def test_weekly_snapshot_keeps_schedule_and_poster_path_from_one_wal_revision(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize()
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
        series = await add_class(
            store,
            1,
            ClassData(title="Popping", first_date=date(2026, 11, 10), start_time=time(19)),
            with_poster=True,
        )
        assert series.poster_source_id is not None
        old_path = Path("posters/1.jpg")
        new_path = Path("posters/new.jpg")
        await asyncio.to_thread((database.parent / new_path).write_bytes, b"new")
        transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series.id,
                expected_revision=1,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 18),
                new_time=time(20),
            ),
            NOW,
        )
        await store.confirm_schedule_change(transfer.id, transfer.version, NOW)
        replacement = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series.id,
                expected_revision=1,
                expected_exception_revision=1,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 25),
                new_time=time(21),
            ),
            NOW,
        )

        class InterleavedConnection(sqlite3.Connection):
            triggered = False

            def execute(
                self,
                sql: str,
                parameters: Sequence[object] | Mapping[str, object] = (),
            ) -> sqlite3.Cursor:
                if "FROM schedule_exceptions WHERE archived = 0" in sql and not self.triggered:
                    self.triggered = True
                    store._confirm_schedule_change_sync(replacement.id, replacement.version, NOW)
                    store._set_source_poster_path_sync(series.poster_source_id, new_path)
                return super().execute(sql, parameters)

        class InterleavedStore(Store):
            @contextmanager
            def _connection(self) -> Generator[sqlite3.Connection]:
                connection = sqlite3.connect(
                    self.path,
                    timeout=10,
                    isolation_level=None,
                    factory=InterleavedConnection,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                try:
                    yield connection
                finally:
                    connection.close()

        reader = InterleavedStore(database)
        previous = await build_weekly_post(date(2026, 11, 16), reader)
        latest = await build_weekly_post(date(2026, 11, 16), store)

        assert previous.occurrences[-1].actual_date == date(2026, 11, 18)
        previous_photos = [part for part in previous.parts if isinstance(part, PhotoPostPart)]
        latest_photos = [part for part in latest.parts if isinstance(part, PhotoPostPart)]
        assert previous_photos and previous_photos[-1].poster_path == old_path
        assert latest_photos and latest_photos[-1].poster_path == new_path
        assert all(item.actual_date != date(2026, 11, 25) for item in previous.occurrences)
        assert latest.catalog_revision != previous.catalog_revision

    asyncio.run(scenario())
