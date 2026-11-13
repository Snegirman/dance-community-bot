from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from telegram_transport import RecordingSession

from dance_community_bot.models import (
    CancelOccurrence,
    ChangeSeries,
    ClassData,
    EndSeries,
    EventData,
    RemoveException,
    SourceInput,
    TransferOccurrence,
)
from dance_community_bot.publishing import publish_week as send_week
from dance_community_bot.storage import Store
from dance_community_bot.storage import records as records_storage
from dance_community_bot.weekly import build_weekly_post

OWNER_ID = 111
GROUP_ID = -100123
TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
NOW = datetime(2026, 11, 8, 12)
MOSCOW = ZoneInfo("Europe/Moscow")


async def add_event(
    store: Store,
    *,
    message_id: int,
    title: str,
    event_date: date,
    end_date: date | None = None,
    description: str | None = None,
    record_id: int | None = None,
    base_revision: int | None = None,
) -> int:
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
        EventData(
            title=title,
            event_type="Джем",
            start_date=event_date,
            end_date=end_date,
            description=description,
        ),
        poster_source_id=None,
        record_id=record_id,
        base_revision=base_revision,
    )
    return (await store.confirm_draft(draft.id, draft.version)).id


async def publish_release(store: Store, week_start: date, bot: Bot) -> None:
    post = await build_weekly_post(week_start, store)
    result = await send_week(post, GROUP_ID, store, bot)
    assert result.status == "delivered"


def create_bot() -> tuple[Bot, RecordingSession]:
    session = RecordingSession(GROUP_ID)
    return Bot(TOKEN, session=session), session


def test_confirmed_week_change_is_persisted_with_diff_and_known_links(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        record_id = await add_event(
            store,
            message_id=1,
            title="Джем",
            event_date=date(2026, 11, 10),
        )
        bot, _ = create_bot()
        await publish_release(store, date(2026, 11, 9), bot)
        original = await store.get_record(record_id)
        assert original is not None

        await add_event(
            store,
            message_id=2,
            title="Джем",
            event_date=date(2026, 11, 12),
            record_id=record_id,
            base_revision=original.revision,
        )

        pending = await store.list_pending_publication_notifications()
        assert len(pending) == 1
        assert pending[0].publication_id is not None
        assert "10.11.2026" in pending[0].text
        assert "12.11.2026" in pending[0].text
        assert "https://t.me/c/123/101" in pending[0].text
        publication = await store.get_publication(pending[0].publication_id)
        assert publication is not None
        assert publication.notification_revision == 1
        assert publication.notified_revision == 0
        await bot.session.close()

    asyncio.run(scenario())


def test_catalog_changes_outside_release_week_do_not_mark_it_stale(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_event(
            store,
            message_id=1,
            title="Ноябрьский джем",
            event_date=date(2026, 11, 10),
        )
        bot, _ = create_bot()
        await publish_release(store, date(2026, 11, 9), bot)

        await add_event(
            store,
            message_id=2,
            title="Декабрьский джем",
            event_date=date(2026, 11, 17),
        )

        assert await store.list_pending_publication_notifications() == []
        await bot.session.close()

    asyncio.run(scenario())


def test_prepared_preview_is_not_treated_as_an_outgoing_publication(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_event(
            store,
            message_id=1,
            title="Первый джем",
            event_date=date(2026, 11, 10),
        )
        post = await build_weekly_post(date(2026, 11, 9), store)
        await store.prepare_publication(post, GROUP_ID, OWNER_ID, 50)

        await add_event(
            store,
            message_id=2,
            title="Второй джем",
            event_date=date(2026, 11, 11),
        )

        assert await store.list_pending_publication_notifications() == []

    asyncio.run(scenario())


def test_notification_ack_only_advances_the_revision_that_was_sent(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        record_id = await add_event(
            store,
            message_id=1,
            title="Первый джем",
            event_date=date(2026, 11, 10),
        )
        bot, _ = create_bot()
        await publish_release(store, date(2026, 11, 9), bot)

        record = await store.get_record(record_id)
        assert record is not None
        await add_event(
            store,
            message_id=2,
            title="Первый джем",
            event_date=date(2026, 11, 12),
            record_id=record_id,
            base_revision=record.revision,
        )
        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
        assert publication is not None
        await add_event(
            store,
            message_id=3,
            title="Новый джем",
            event_date=date(2026, 11, 13),
        )

        await store.acknowledge_publication_notification(publication.id, 1)

        refreshed = await store.get_publication(publication.id)
        pending = await store.list_pending_publication_notifications()
        assert refreshed is not None
        assert refreshed.notification_revision == 2
        assert refreshed.notified_revision == 1
        assert [notice.revision for notice in pending] == [2]
        await bot.session.close()

    asyncio.run(scenario())


def test_failed_notification_recovers_and_does_not_block_later_revision(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize()
        record_id = await add_event(
            store,
            message_id=1,
            title="Первый джем",
            event_date=date(2026, 11, 10),
        )
        bot, _ = create_bot()
        await publish_release(store, date(2026, 11, 9), bot)

        record = await store.get_record(record_id)
        assert record is not None
        await add_event(
            store,
            message_id=2,
            title="Первый джем",
            event_date=date(2026, 11, 12),
            record_id=record_id,
            base_revision=record.revision,
        )
        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
        assert publication is not None
        first = await store.claim_next_publication_notification()
        assert first is not None and first.revision == 1

        await store.recover_automation_notices_on_startup()
        reopened = Store(database)
        await reopened.initialize()
        recovered = await reopened.claim_next_publication_notification()
        assert recovered == first
        await reopened.finish_publication_notification(publication.id, 1, "retry")

        await add_event(
            reopened,
            message_id=3,
            title="Новый джем",
            event_date=date(2026, 11, 13),
        )
        await reopened.acknowledge_publication_notification(publication.id, 1)

        next_notice = await reopened.claim_next_publication_notification()
        assert next_notice is not None
        assert next_notice.publication_id == publication.id
        assert next_notice.revision == 2
        restored = await reopened.get_publication(publication.id)
        assert restored is not None
        assert restored.notification_revision == 2
        assert restored.notified_revision == 1
        await bot.session.close()

    asyncio.run(scenario())


def test_failed_marking_rolls_back_catalog_change_and_stale_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        record_id = await add_event(
            store,
            message_id=1,
            title="Джем",
            event_date=date(2026, 11, 10),
        )
        bot, _ = create_bot()
        await publish_release(store, date(2026, 11, 9), bot)
        record = await store.get_record(record_id)
        assert record is not None
        source = await store.save_source(
            SourceInput(
                chat_id=OWNER_ID,
                message_id=2,
                text="Изменение",
                received_at=NOW,
            )
        )
        draft = await store.save_draft(
            source.id,
            EventData(
                title="Джем после",
                event_type="Джем",
                start_date=date(2026, 11, 11),
            ),
            poster_source_id=None,
            record_id=record.id,
            base_revision=record.revision,
        )

        def fail_marking(connection: object, before: object, after: object) -> None:
            raise RuntimeError("marking failed")

        monkeypatch.setattr(records_storage, "mark_published_week_changes", fail_marking)
        try:
            await store.confirm_draft(draft.id, draft.version)
        except RuntimeError as error:
            assert str(error) == "marking failed"
        else:
            raise AssertionError("confirmation should roll back when stale marking fails")

        unchanged = await store.get_record(record.id)
        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
        assert unchanged == record
        assert publication is not None
        assert publication.notification_revision == 0
        assert await store.list_pending_publication_notifications() == []
        await bot.session.close()

    asyncio.run(scenario())


def test_repeated_occurrence_transfer_tracks_each_published_week(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            SourceInput(
                chat_id=OWNER_ID,
                message_id=1,
                text="Popping",
                received_at=NOW,
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
        series = await store.confirm_draft(draft.id, draft.version)
        bot, _ = create_bot()
        for week in (date(2026, 11, 9), date(2026, 11, 16), date(2026, 11, 23)):
            await publish_release(store, week, bot)

        first_transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series.id,
                expected_revision=series.revision,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 18),
                new_time=time(20),
                expected_exception_revision=None,
            ),
            datetime(2026, 11, 9, 12, tzinfo=MOSCOW),
        )
        await store.confirm_schedule_change(
            first_transfer.id,
            first_transfer.version,
            datetime(2026, 11, 9, 12, tzinfo=MOSCOW),
        )
        first_notices = await store.list_pending_publication_notifications()
        assert [(item.revision, item.publication_id) for item in first_notices] == [(1, 1), (1, 2)]
        assert any(
            "удалено из выпуска" in item.text and "10.11.2026" in item.text
            for item in first_notices
        )
        assert any(
            "Добавлено занятие" in item.text and "18.11.2026" in item.text for item in first_notices
        )
        for item in first_notices:
            await store.acknowledge_publication_notification(item.publication_id, item.revision)

        second_transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series.id,
                expected_revision=series.revision,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 25),
                new_time=time(21),
                expected_exception_revision=1,
            ),
            datetime(2026, 11, 9, 12, tzinfo=MOSCOW),
        )
        await store.confirm_schedule_change(
            second_transfer.id,
            second_transfer.version,
            datetime(2026, 11, 9, 12, tzinfo=MOSCOW),
        )
        second_notices = await store.list_pending_publication_notifications()
        assert [(item.revision, item.publication_id) for item in second_notices] == [(2, 2), (1, 3)]
        assert any(
            "18.11.2026" in item.text and "удалено из выпуска" in item.text
            for item in second_notices
        )
        assert any(
            "Добавлено занятие" in item.text and "25.11.2026" in item.text
            for item in second_notices
        )
        for item in second_notices:
            await store.acknowledge_publication_notification(item.publication_id, item.revision)

        cancel = await store.propose_schedule_change(
            CancelOccurrence(
                action="cancel_occurrence",
                series_id=series.id,
                expected_revision=series.revision,
                original_date=date(2026, 11, 10),
                expected_exception_revision=2,
            ),
            datetime(2026, 11, 9, 12, tzinfo=MOSCOW),
        )
        await store.confirm_schedule_change(
            cancel.id, cancel.version, datetime(2026, 11, 9, 12, tzinfo=MOSCOW)
        )
        cancel_notices = await store.list_pending_publication_notifications()
        assert len(cancel_notices) == 1
        assert cancel_notices[0].publication_id == 3
        assert "25.11.2026" in cancel_notices[0].text
        assert "удалено из выпуска" in cancel_notices[0].text
        await store.acknowledge_publication_notification(
            cancel_notices[0].publication_id, cancel_notices[0].revision
        )

        remove = await store.propose_schedule_change(
            RemoveException(
                action="remove_exception",
                series_id=series.id,
                expected_revision=series.revision,
                original_date=date(2026, 11, 10),
                expected_exception_revision=3,
            ),
            datetime(2026, 11, 9, 12, tzinfo=MOSCOW),
        )
        await store.confirm_schedule_change(
            remove.id, remove.version, datetime(2026, 11, 9, 12, tzinfo=MOSCOW)
        )
        restored_week = await build_weekly_post(date(2026, 11, 9), store)
        assert any(item.original_date == date(2026, 11, 10) for item in restored_week.occurrences)
        restored_notices = await store.list_pending_publication_notifications()
        assert any(item.publication_id == 1 for item in restored_notices)
        await bot.session.close()

    asyncio.run(scenario())


def test_new_confirmed_event_after_publication_is_reported(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_event(
            store,
            message_id=1,
            title="Первый джем",
            event_date=date(2026, 11, 10),
        )
        bot, _ = create_bot()
        await publish_release(store, date(2026, 11, 9), bot)

        await add_event(
            store,
            message_id=2,
            title="Поздний джем",
            event_date=date(2026, 11, 12),
        )

        pending = await store.list_pending_publication_notifications()
        assert len(pending) == 1
        assert "Добавлено событие «Поздний джем»" in pending[0].text
        assert "12.11.2026" in pending[0].text
        await bot.session.close()

    asyncio.run(scenario())


def test_changed_multiday_range_is_reported_with_both_end_dates(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        record_id = await add_event(
            store,
            message_id=1,
            title="Фестиваль",
            event_date=date(2026, 11, 8),
            end_date=date(2026, 11, 10),
        )
        bot, _ = create_bot()
        await publish_release(store, date(2026, 11, 9), bot)
        original = await store.get_record(record_id)
        assert original is not None

        await add_event(
            store,
            message_id=2,
            title="Фестиваль",
            event_date=date(2026, 11, 8),
            end_date=date(2026, 11, 11),
            record_id=record_id,
            base_revision=original.revision,
        )

        pending = await store.list_pending_publication_notifications()
        assert len(pending) == 1
        assert "10.11.2026 → 11.11.2026" in pending[0].text
        await bot.session.close()

    asyncio.run(scenario())


def test_confirming_identical_visible_contents_does_not_mark_publication_stale(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        record_id = await add_event(
            store,
            message_id=1,
            title="Одинаковый джем",
            event_date=date(2026, 11, 10),
            description="Тот же текст",
        )
        bot, _ = create_bot()
        await publish_release(store, date(2026, 11, 9), bot)
        original = await store.get_record(record_id)
        assert original is not None

        await add_event(
            store,
            message_id=2,
            title="Одинаковый джем",
            event_date=date(2026, 11, 10),
            description="Тот же текст",
            record_id=record_id,
            base_revision=original.revision,
        )

        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
        assert publication is not None
        assert publication.notification_revision == 0
        assert await store.list_pending_publication_notifications() == []
        await bot.session.close()

    asyncio.run(scenario())


async def _add_popping_series(store: Store) -> int:
    source = await store.save_source(
        SourceInput(
            chat_id=OWNER_ID,
            message_id=50,
            text="Popping",
            received_at=NOW,
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
    return (await store.confirm_draft(draft.id, draft.version)).id


def test_series_change_and_end_update_weeks_with_external_transfer(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        series_id = await _add_popping_series(store)
        now = datetime(2026, 11, 9, 12, tzinfo=MOSCOW)
        transfer = await store.propose_schedule_change(
            TransferOccurrence(
                action="transfer_occurrence",
                series_id=series_id,
                expected_revision=1,
                original_date=date(2026, 11, 10),
                new_date=date(2026, 11, 25),
                new_time=time(20),
                expected_exception_revision=None,
            ),
            now,
        )
        await store.confirm_schedule_change(transfer.id, transfer.version, now)
        bot, _ = create_bot()
        await publish_release(store, date(2026, 11, 16), bot)
        await publish_release(store, date(2026, 11, 23), bot)

        change = await store.propose_schedule_change(
            ChangeSeries(
                action="change_series",
                series_id=series_id,
                expected_revision=1,
                effective_from=date(2026, 11, 17),
                data=ClassData(
                    title="Popping Advanced",
                    first_date=date(2026, 11, 17),
                    start_time=time(20),
                    place="Зал B",
                ),
                resolutions={date(2026, 11, 10): "detach"},
            ),
            now,
        )
        await store.confirm_schedule_change(change.id, change.version, now)

        after_change = await store.list_pending_publication_notifications()
        assert [(item.publication_id, item.revision) for item in after_change] == [(1, 1), (2, 1)]
        assert any("Popping Advanced" in item.text for item in after_change)
        assert any("25.11.2026" in item.text for item in after_change)
        for item in after_change:
            await store.acknowledge_publication_notification(item.publication_id, item.revision)

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

        after_end = await store.list_pending_publication_notifications()
        assert len(after_end) == 1
        assert after_end[0].publication_id == 2
        assert "24.11.2026" in after_end[0].text
        assert "25.11.2026" not in after_end[0].text
        versions, exceptions = await store.get_schedule()
        final_week = await build_weekly_post(date(2026, 11, 23), store)
        assert [item.actual_date for item in final_week.occurrences] == []
        assert any(
            isinstance(record.data, EventData) and record.data.start_date == date(2026, 11, 25)
            for record in final_week.records
        )
        assert len(exceptions) == 1
        assert exceptions[0].original_date == date(2026, 11, 10)
        assert exceptions[0].cancelled
        assert exceptions[0].new_date is None
        assert versions[-1].effective_until == date(2026, 11, 18)
        await bot.session.close()

    asyncio.run(scenario())
