import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dance_community_bot.models import (
    CancelOccurrence,
    ChangeSeries,
    ClassData,
    EndSeries,
    EventData,
    RemoveException,
    ScheduleException,
    SeriesVersion,
    SourceInput,
    TransferOccurrence,
)
from dance_community_bot.schedule import expand_schedule
from dance_community_bot.storage import Store


def make_series(
    *, first_date: date = date(2026, 11, 10), last_date: date | None = None
) -> SeriesVersion:
    return SeriesVersion(
        series_id=1,
        revision=1,
        effective_from=first_date,
        effective_until=None,
        data=ClassData(
            title="Popping",
            first_date=first_date,
            last_date=last_date,
            start_time=time(19),
            place="Зал A",
            price="600 ₽",
            description="Открытая группа",
        ),
        poster_source_id=7,
    )


def make_transfer(
    original_date: date,
    new_date: date,
    *,
    revision: int = 1,
    new_time: time = time(20, 30),
    place: str | None = "Зал B",
    cancelled: bool = False,
) -> ScheduleException:
    return ScheduleException(
        series_id=1,
        original_date=original_date,
        revision=revision,
        cancelled=cancelled,
        new_date=new_date,
        new_time=new_time,
        place=place,
    )


def test_repeated_transfer_keeps_original_identity_and_cancel_does_not_restore_it() -> None:
    series = make_series()
    first_transfer = make_transfer(date(2026, 11, 10), date(2026, 11, 18))

    week_of_10 = expand_schedule([series], [first_transfer], date(2026, 11, 9))
    assert week_of_10 == []

    week_of_17 = expand_schedule([series], [first_transfer], date(2026, 11, 16))
    assert [(item.original_date, item.actual_date) for item in week_of_17] == [
        (date(2026, 11, 17), date(2026, 11, 17)),
        (date(2026, 11, 10), date(2026, 11, 18)),
    ]
    moved = week_of_17[1]
    assert (moved.title, moved.start_time, moved.place, moved.poster_source_id) == (
        "Popping",
        time(20, 30),
        "Зал B",
        7,
    )

    repeated_transfer = make_transfer(date(2026, 11, 10), date(2026, 11, 25), revision=2)
    week_after_second_transfer = expand_schedule([series], [repeated_transfer], date(2026, 11, 16))
    assert [(item.original_date, item.actual_date) for item in week_after_second_transfer] == [
        (date(2026, 11, 17), date(2026, 11, 17))
    ]

    cancelled_transfer = make_transfer(
        date(2026, 11, 10), date(2026, 11, 25), revision=3, cancelled=True
    )
    week_of_25 = expand_schedule([series], [cancelled_transfer], date(2026, 11, 23))
    assert [item.actual_date for item in week_of_25] == [date(2026, 11, 24)]
    cancelled_occurrences = expand_schedule(
        [series], [cancelled_transfer], date(2026, 11, 23), include_cancelled=True
    )
    assert [
        (item.original_date, item.actual_date, item.cancelled) for item in cancelled_occurrences
    ] == [
        (date(2026, 11, 24), date(2026, 11, 24), False),
        (date(2026, 11, 10), date(2026, 11, 25), True),
    ]

    restored = expand_schedule([series], [], date(2026, 11, 9))
    assert [(item.original_date, item.actual_date) for item in restored] == [
        (date(2026, 11, 10), date(2026, 11, 10))
    ]


def test_last_date_is_inclusive_and_transfer_can_leave_series_bounds() -> None:
    series = make_series(last_date=date(2026, 11, 24))
    transfer = make_transfer(date(2026, 11, 24), date(2026, 12, 1))

    last_week = expand_schedule([series], [transfer], date(2026, 11, 23))
    assert last_week == []
    next_week = expand_schedule([series], [transfer], date(2026, 11, 30))
    assert [(item.original_date, item.actual_date) for item in next_week] == [
        (date(2026, 11, 24), date(2026, 12, 1))
    ]


def test_cancelled_occurrence_can_be_shown_for_removal_in_schedule_view() -> None:
    series = make_series()
    cancelled = ScheduleException(
        series_id=1,
        original_date=date(2026, 11, 10),
        revision=1,
        cancelled=True,
    )

    assert expand_schedule([series], [cancelled], date(2026, 11, 9)) == []
    visible = expand_schedule([series], [cancelled], date(2026, 11, 9), include_cancelled=True)
    assert [(item.original_date, item.actual_date, item.cancelled) for item in visible] == [
        (date(2026, 11, 10), date(2026, 11, 10), True)
    ]


def test_overlapping_occurrences_are_kept_separate_and_warned() -> None:
    first = make_series()
    second = SeriesVersion(
        series_id=2,
        revision=1,
        effective_from=date(2026, 11, 12),
        effective_until=None,
        data=ClassData(
            title="Waving",
            first_date=date(2026, 11, 12),
            start_time=time(19),
            place="Зал C",
        ),
        poster_source_id=None,
    )
    moved = make_transfer(date(2026, 11, 10), date(2026, 11, 12), new_time=time(19))

    occurrences = expand_schedule([first, second], [moved], date(2026, 11, 9))
    overlapping = [item for item in occurrences if item.actual_date == date(2026, 11, 12)]
    assert [(item.series_id, item.original_date) for item in overlapping] == [
        (1, date(2026, 11, 10)),
        (2, date(2026, 11, 12)),
    ]
    assert all(item.warnings for item in overlapping)


async def create_confirmed_class(
    store: Store, *, last_date: date | None = None, with_poster: bool = False
) -> tuple[int, int, int | None]:
    source_input = SourceInput(
        chat_id=111,
        message_id=1,
        text="Название: Popping",
        received_at=datetime(2026, 11, 9, 9, tzinfo=UTC),
        telegram_file_id="poster-file" if with_poster else None,
        is_photo=with_poster,
    )
    source = await store.save_source(source_input)
    if with_poster:
        poster_path = Path("posters/class.jpg")
        file_path = store.path.parent / poster_path
        await asyncio.to_thread(file_path.write_bytes, b"fake poster")
        await store.set_source_poster_path(source.id, poster_path)
    data = ClassData(
        title="Popping",
        first_date=date(2026, 11, 10),
        last_date=last_date,
        start_time=time(19),
        place="Зал A",
    )
    draft = await store.save_draft(
        source.id,
        data,
        poster_source_id=source.id if with_poster else None,
    )
    record = await store.confirm_draft(draft.id, draft.version)
    return record.id, source.id, record.poster_source_id


def test_confirming_class_creates_first_series_version(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        series_id, source_id, poster_source_id = await create_confirmed_class(
            store, last_date=date(2026, 11, 24), with_poster=True
        )

        versions, exceptions = await store.get_schedule()

        assert exceptions == []
        assert len(versions) == 1
        assert (
            versions[0].series_id,
            versions[0].revision,
            versions[0].effective_from,
            versions[0].effective_until,
            versions[0].source_id,
            versions[0].poster_source_id,
        ) == (
            series_id,
            1,
            date(2026, 11, 10),
            date(2026, 11, 25),
            source_id,
            poster_source_id,
        )

    asyncio.run(scenario())


def test_schedule_change_confirmation_is_idempotent_and_rejects_stale_draft(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        series_id, _, _ = await create_confirmed_class(store)
        now = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        first_change = TransferOccurrence(
            action="transfer_occurrence",
            series_id=series_id,
            expected_revision=1,
            original_date=date(2026, 11, 10),
            new_date=date(2026, 11, 18),
            new_time=time(20, 30),
            place="Зал B",
        )
        replacement_change = TransferOccurrence(
            action="transfer_occurrence",
            series_id=series_id,
            expected_revision=1,
            original_date=date(2026, 11, 10),
            new_date=date(2026, 11, 25),
            new_time=time(21),
        )
        confirmed = await store.propose_schedule_change(first_change, now)
        replacement = await store.propose_schedule_change(replacement_change, now)

        with pytest.raises(ValueError, match="устар|замен"):
            await store.confirm_schedule_change(confirmed.id, confirmed.version, now)
        await store.confirm_schedule_change(replacement.id, replacement.version, now)
        await store.confirm_schedule_change(replacement.id, replacement.version, now)

        versions, exceptions = await store.get_schedule()
        assert len(versions) == 1
        assert len(exceptions) == 1
        assert (
            exceptions[0].series_id,
            exceptions[0].original_date,
            exceptions[0].revision,
            exceptions[0].new_date,
            exceptions[0].new_time,
        ) == (
            series_id,
            date(2026, 11, 10),
            1,
            date(2026, 11, 25),
            time(21),
        )

    asyncio.run(scenario())


def test_removing_exception_does_not_reuse_its_revision_or_old_button(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        series_id, _, _ = await create_confirmed_class(store)
        now = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series_id,
                expected_revision=1,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 18),
                new_time=time(20),
            ),
            now,
        )
        await store.confirm_schedule_change(transfer.id, transfer.version, now)
        remove = await store.propose_schedule_change(
            RemoveException(
                action="remove_exception",
                series_id=series_id,
                expected_revision=1,
                expected_exception_revision=1,
                original_date=date(2026, 11, 10),
            ),
            now,
        )
        await store.confirm_schedule_change(remove.id, remove.version, now)

        stale_button = TransferOccurrence(
            action="transfer_occurrence",
            series_id=series_id,
            expected_revision=1,
            expected_exception_revision=1,
            original_date=date(2026, 11, 10),
            new_date=date(2026, 11, 25),
            new_time=time(21),
        )
        with pytest.raises(ValueError, match="устар|ревиз"):
            await store.propose_schedule_change(stale_button, now)

        fresh_button = stale_button.model_copy(update={"expected_exception_revision": 2})
        fresh = await store.propose_schedule_change(fresh_button, now)
        await store.confirm_schedule_change(fresh.id, fresh.version, now)
        _, exceptions = await store.get_schedule()
        assert len(exceptions) == 1
        assert (exceptions[0].revision, exceptions[0].new_date) == (3, date(2026, 11, 25))

    asyncio.run(scenario())


def test_exception_cannot_target_a_date_outside_the_confirmed_series(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        series_id, _, _ = await create_confirmed_class(store, last_date=date(2026, 11, 24))
        now = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Europe/Moscow"))

        for original_date in (date(2026, 11, 11), date(2026, 12, 1)):
            change = CancelOccurrence(
                action="cancel_occurrence",
                series_id=series_id,
                expected_revision=1,
                original_date=original_date,
            )
            with pytest.raises(ValueError, match="проведения|занятия"):
                await store.propose_schedule_change(change, now)

        versions, exceptions = await store.get_schedule()
        assert len(versions) == 1
        assert exceptions == []

    asyncio.run(scenario())


def test_cancel_transfer_preserves_target_and_removing_exception_restores_origin(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        series_id, _, _ = await create_confirmed_class(store)
        now = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series_id,
                expected_revision=1,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 18),
                new_time=time(20),
            ),
            now,
        )
        await store.confirm_schedule_change(transfer.id, transfer.version, now)
        cancel = await store.propose_schedule_change(
            CancelOccurrence(
                action="cancel_occurrence",
                series_id=series_id,
                expected_revision=1,
                expected_exception_revision=1,
                original_date=date(2026, 11, 10),
            ),
            now,
        )
        await store.confirm_schedule_change(cancel.id, cancel.version, now)

        versions, exceptions = await store.get_schedule()
        assert len(exceptions) == 1
        assert (exceptions[0].cancelled, exceptions[0].new_date) == (True, date(2026, 11, 18))
        week = expand_schedule(versions, exceptions, date(2026, 11, 16), include_cancelled=True)
        assert [(item.original_date, item.actual_date, item.cancelled) for item in week] == [
            (date(2026, 11, 17), date(2026, 11, 17), False),
            (date(2026, 11, 10), date(2026, 11, 18), True),
        ]

        remove = await store.propose_schedule_change(
            RemoveException(
                action="remove_exception",
                series_id=series_id,
                expected_revision=1,
                expected_exception_revision=2,
                original_date=date(2026, 11, 10),
            ),
            now,
        )
        await store.confirm_schedule_change(remove.id, remove.version, now)
        versions, exceptions = await store.get_schedule()
        assert exceptions == []
        restored = expand_schedule(versions, exceptions, date(2026, 11, 9))
        assert [(item.original_date, item.actual_date) for item in restored] == [
            (date(2026, 11, 10), date(2026, 11, 10))
        ]

    asyncio.run(scenario())


def test_change_series_lists_future_versions_and_requires_tomorrow_boundary(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        series_id, _, _ = await create_confirmed_class(store)
        now = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        tomorrow_data = ClassData(
            title="Popping",
            first_date=date(2026, 11, 10),
            start_time=time(20),
            place="Зал B",
        )
        same_day = ChangeSeries(
            action="change_series",
            series_id=series_id,
            expected_revision=1,
            effective_from=date(2026, 11, 9),
            data=tomorrow_data,
        )
        with pytest.raises(ValueError, match="завтрашн"):
            await store.propose_schedule_change(same_day, now)

        initial_future_rule = ChangeSeries(
            action="change_series",
            series_id=series_id,
            expected_revision=1,
            effective_from=date(2026, 11, 17),
            data=ClassData(
                title="Popping",
                first_date=date(2026, 11, 17),
                start_time=time(20),
                place="Зал B",
            ),
        )
        first_draft = await store.propose_schedule_change(initial_future_rule, now)
        await store.confirm_schedule_change(first_draft.id, first_draft.version, now)

        replacement = ChangeSeries(
            action="change_series",
            series_id=series_id,
            expected_revision=2,
            effective_from=date(2026, 11, 17),
            data=ClassData(
                title="Popping",
                first_date=date(2026, 11, 17),
                start_time=time(21),
                place="Зал C",
            ),
        )
        replacement_draft = await store.propose_schedule_change(replacement, now)
        assert [version.revision for version in replacement_draft.superseded_versions] == [2]
        await store.confirm_schedule_change(replacement_draft.id, replacement_draft.version, now)

        versions, _ = await store.get_schedule()
        assert [
            (item.revision, item.effective_from, item.effective_until) for item in versions
        ] == [
            (1, date(2026, 11, 10), date(2026, 11, 17)),
            (3, date(2026, 11, 17), None),
        ]
        assert versions[-1].data.start_time == time(21)

    asyncio.run(scenario())


def test_ending_series_keeps_last_date_and_disables_future_versions(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        series_id, _, _ = await create_confirmed_class(store)
        now = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        future = await store.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=series_id,
                expected_revision=1,
                effective_from=date(2026, 11, 17),
                data=ClassData(
                    title="Popping",
                    first_date=date(2026, 11, 17),
                    start_time=time(20),
                ),
            ),
            now,
        )
        await store.confirm_schedule_change(future.id, future.version, now)
        end = await store.propose_schedule_change(
            EndSeries(
                action="end_series",
                series_id=series_id,
                expected_revision=2,
                last_date=date(2026, 11, 17),
            ),
            now,
        )
        await store.confirm_schedule_change(end.id, end.version, now)

        versions, exceptions = await store.get_schedule()
        assert [item.revision for item in versions] == [1, 2]
        assert [(item.effective_from, item.effective_until) for item in versions] == [
            (date(2026, 11, 10), date(2026, 11, 17)),
            (date(2026, 11, 17), date(2026, 11, 18)),
        ]
        assert [
            item.actual_date for item in expand_schedule(versions, exceptions, date(2026, 11, 16))
        ] == [date(2026, 11, 17)]
        assert expand_schedule(versions, exceptions, date(2026, 11, 23)) == []

    asyncio.run(scenario())


def test_detach_occurrence_is_atomic_and_preserves_source_links(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        series_id, original_source_id, poster_source_id = await create_confirmed_class(
            store, with_poster=True
        )
        transfer_source = await store.save_source(
            SourceInput(
                chat_id=111,
                message_id=2,
                text="Дата: 18.11.2026\nВремя: 20:30",
                received_at=datetime(2026, 11, 9, 10, tzinfo=UTC),
            )
        )
        now = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        transfer = TransferOccurrence(
            action="transfer_occurrence",
            series_id=series_id,
            expected_revision=1,
            original_date=date(2026, 11, 10),
            new_date=date(2026, 11, 18),
            new_time=time(20, 30),
            source_id=transfer_source.id,
        )
        transfer_draft = await store.propose_schedule_change(transfer, now)
        await store.confirm_schedule_change(transfer_draft.id, transfer_draft.version, now)

        change = ChangeSeries(
            action="change_series",
            series_id=series_id,
            expected_revision=1,
            effective_from=date(2026, 11, 17),
            data=ClassData(
                title="Popping",
                first_date=date(2026, 11, 17),
                start_time=time(19),
                place="Зал C",
            ),
        )
        draft = await store.propose_schedule_change(change, now)
        assert [item.original_date for item in draft.affected_exceptions] == [date(2026, 11, 10)]
        with pytest.raises(ValueError, match="решен"):
            await store.confirm_schedule_change(draft.id, draft.version, now)

        resolved = await store.resolve_schedule_change(
            draft.id,
            draft.version,
            original_date=date(2026, 11, 10),
            resolution="detach",
        )
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute(
                "CREATE TRIGGER fail_series_revision_update "
                "BEFORE UPDATE OF revision ON records "
                f"WHEN OLD.id = {series_id} "
                "BEGIN SELECT RAISE(ABORT, 'forced schedule failure'); END"
            )
            connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="forced schedule failure"):
            await store.confirm_schedule_change(resolved.id, resolved.version, now)

        records_after_rollback = await store.list_records()
        versions_after_rollback, exceptions_after_rollback = await store.get_schedule()
        assert len(records_after_rollback) == 1
        assert versions_after_rollback[0].effective_until is None
        assert len(exceptions_after_rollback) == 1
        assert exceptions_after_rollback[0].new_date == date(2026, 11, 18)
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute("DROP TRIGGER fail_series_revision_update")
            connection.commit()

        await store.confirm_schedule_change(resolved.id, resolved.version, now)
        records = await store.list_records()
        detached = [record for record in records if isinstance(record.data, EventData)]
        assert len(detached) == 1
        assert (
            detached[0].data.title,
            detached[0].data.start_date,
            detached[0].data.start_time,
            detached[0].poster_source_id,
        ) == ("Popping", date(2026, 11, 18), time(20, 30), poster_source_id)
        versions, exceptions = await store.get_schedule()
        occurrences = expand_schedule(versions, exceptions, date(2026, 11, 16))
        assert [(item.original_date, item.actual_date) for item in occurrences] == [
            (date(2026, 11, 17), date(2026, 11, 17))
        ]
        prior_week = expand_schedule(
            versions, exceptions, date(2026, 11, 9), include_cancelled=True
        )
        assert [(item.original_date, item.cancelled) for item in prior_week] == [
            (date(2026, 11, 10), True)
        ]

        with closing(sqlite3.connect(store.path)) as connection:
            link = connection.execute(
                "SELECT series_id, original_date, source_id, exception_source_id, poster_source_id "
                "FROM detached_occurrence_links WHERE event_record_id = ?",
                (detached[0].id,),
            ).fetchone()
        assert link == (
            series_id,
            "2026-11-10",
            original_source_id,
            transfer_source.id,
            poster_source_id,
        )

    asyncio.run(scenario())


def test_change_series_confirmation_rechecks_new_affected_exceptions(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        series_id, _, _ = await create_confirmed_class(store)
        now = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        draft = await store.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=series_id,
                expected_revision=1,
                effective_from=date(2026, 11, 17),
                data=ClassData(
                    title="Popping",
                    first_date=date(2026, 11, 17),
                    start_time=time(20),
                ),
            ),
            now,
        )
        moved = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series_id,
                expected_revision=1,
                original_date=date(2026, 11, 24),
                new_date=date(2026, 11, 25),
                new_time=time(20),
            ),
            now,
        )
        await store.confirm_schedule_change(moved.id, moved.version, now)

        with pytest.raises(ValueError, match="изменил|повторно"):
            await store.confirm_schedule_change(draft.id, draft.version, now)

        versions, exceptions = await store.get_schedule()
        assert len(versions) == 1
        assert versions[0].effective_until is None
        assert len(exceptions) == 1
        assert exceptions[0].original_date == date(2026, 11, 24)

    asyncio.run(scenario())


def test_migration_backfills_active_class_records_as_series_versions(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    data = ClassData(
        title="Waving",
        first_date=date(2026, 11, 12),
        last_date=date(2026, 12, 3),
        start_time=time(20),
    )
    source = SourceInput(
        chat_id=111,
        message_id=1,
        text="Старое занятие",
        received_at=datetime(2026, 11, 9, 9, tzinfo=UTC),
    )
    initial_schema = (
        Path(__file__).parents[1] / "src" / "dance_community_bot" / "migrations" / "001_initial.sql"
    ).read_text(encoding="utf-8")
    intake_schema = (
        Path(__file__).parents[1] / "src" / "dance_community_bot" / "migrations" / "002_intake.sql"
    ).read_text(encoding="utf-8")
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(initial_schema)
        connection.executescript(intake_schema)
        connection.execute("PRAGMA user_version = 2")
        connection.execute(
            "INSERT INTO sources (chat_id, message_id, text, received_at, status) "
            "VALUES (?, ?, ?, ?, 'processed')",
            (source.chat_id, source.message_id, source.text, source.received_at.isoformat()),
        )
        connection.execute(
            "INSERT INTO records (revision, data_type, data_json, status) "
            "VALUES (4, 'class', ?, 'active')",
            (data.model_dump_json(),),
        )
        connection.execute(
            "INSERT INTO drafts (record_id, version, base_revision, data_type, data_json, "
            "source_id, status) VALUES (1, 1, NULL, 'class', ?, 1, 'confirmed')",
            (data.model_dump_json(),),
        )
        connection.commit()

    async def scenario() -> None:
        store = Store(database)
        await store.initialize()
        versions, exceptions = await store.get_schedule()
        assert exceptions == []
        assert len(versions) == 1
        assert (
            versions[0].series_id,
            versions[0].revision,
            versions[0].effective_from,
            versions[0].effective_until,
            versions[0].source_id,
        ) == (1, 4, date(2026, 11, 12), date(2026, 12, 4), 1)

    asyncio.run(scenario())


def test_active_class_cannot_be_changed_through_generic_card_draft(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        series_id, _, _ = await create_confirmed_class(store)
        source = await store.save_source(
            SourceInput(
                chat_id=111,
                message_id=2,
                text="Название: Новое расписание",
                received_at=datetime(2026, 11, 9, 10, tzinfo=UTC),
            )
        )

        with pytest.raises(ValueError, match="регулярн|распис"):
            await store.save_draft(
                source.id,
                ClassData(
                    title="Popping",
                    first_date=date(2026, 11, 10),
                    start_time=time(20),
                ),
                poster_source_id=None,
                record_id=series_id,
                base_revision=1,
            )

        assert await store.get_source(source.chat_id, source.message_id) == source
        record = await store.get_record(series_id)
        assert record is not None and record.revision == 1

    asyncio.run(scenario())
