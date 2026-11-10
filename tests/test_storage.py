from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from dance_community_bot.models import ClassData, EventData, SourceInput
from dance_community_bot.storage import Store


def make_event(title: str = "Popping Jam") -> EventData:
    return EventData(title=title, event_type="Джем", start_date=date(2026, 11, 14))


def make_source(message_id: int, text: str = "Карточка") -> SourceInput:
    return SourceInput(
        chat_id=111,
        message_id=message_id,
        text=text,
        received_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
    )


def test_save_and_reopen_preserves_record_and_draft_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize()
        source = await store.save_source(make_source(1, "Афиша и условия"))
        draft = await store.save_draft(source.id, make_event(), poster_source_id=None)
        confirmed = await store.confirm_draft(draft.id, draft.version)

        reopened = Store(database)
        await reopened.initialize()
        restored_record = await reopened.get_record(confirmed.id)
        restored_draft = await reopened.get_draft(draft.id)

        assert restored_record == confirmed
        assert restored_draft == draft.model_copy(update={"status": "confirmed"})

    asyncio.run(scenario())


def test_initialize_can_apply_migrations_repeatedly(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await store.initialize()
        source = await store.save_source(make_source(1))
        await store.initialize()
        restored_source = await store.save_source(make_source(1))

        assert restored_source.id == source.id

    asyncio.run(scenario())


def test_initialize_upgrades_existing_v1_catalog_and_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "legacy.sqlite3"
        initial_schema = (
            Path(__file__).parents[1]
            / "src"
            / "dance_community_bot"
            / "migrations"
            / "001_initial.sql"
        ).read_text(encoding="utf-8")
        source = make_source(1, "Событие до обновления")
        data = make_event("Сохранённое событие")
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(initial_schema)
            connection.execute("PRAGMA user_version = 1")
            connection.execute(
                "INSERT INTO sources "
                "(id, chat_id, message_id, text, received_at, status) "
                "VALUES (?, ?, ?, ?, ?, 'processed')",
                (
                    1,
                    source.chat_id,
                    source.message_id,
                    source.text,
                    source.received_at.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO records "
                "(id, revision, data_type, data_json, status) "
                "VALUES (1, 1, 'event', ?, 'active')",
                (data.model_dump_json(),),
            )
            connection.execute(
                "INSERT INTO drafts "
                "(id, record_id, version, base_revision, data_type, data_json, "
                "source_id, status) "
                "VALUES (1, 1, 1, NULL, 'event', ?, 1, 'confirmed')",
                (data.model_dump_json(),),
            )
            connection.commit()

        store = Store(database)
        await store.initialize()
        await store.initialize()

        restored_source = await store.get_source(source.chat_id, source.message_id)
        restored_record = await store.get_record(1)
        restored_draft = await store.get_draft(1)
        action = await store.create_card_action("view", record_id=1, revision=1)
        await store.bind_card_actions(source.chat_id, 101, [action.token])
        bound_action = await store.get_card_action(action.token)
        with closing(sqlite3.connect(database)) as connection:
            version_row = connection.execute("PRAGMA user_version").fetchone()

        assert restored_source is not None
        assert restored_source.status == "processed"
        assert restored_source.attachment_status == "none"
        assert not restored_source.is_photo
        assert restored_record is not None and restored_record.data == data
        assert restored_draft is not None and restored_draft.status == "confirmed"
        assert restored_draft.data == data
        assert bound_action is not None
        assert bound_action.chat_id == source.chat_id
        assert bound_action.bot_message_id == 101
        assert version_row == (3,)

    asyncio.run(scenario())


def test_retry_after_source_only_crash_creates_one_record(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()

        first_delivery = await store.save_source(make_source(1))
        retry_store = Store(tmp_path / "dance.sqlite3")
        await retry_store.initialize()
        retry_delivery = await retry_store.save_source(make_source(1))
        draft = await store.save_draft(retry_delivery.id, make_event(), poster_source_id=None)
        confirmed = await store.confirm_draft(draft.id, draft.version)
        reopened = Store(tmp_path / "dance.sqlite3")
        await reopened.initialize()
        repeated_draft = await reopened.save_draft(
            retry_delivery.id, make_event(), poster_source_id=None
        )

        assert first_delivery.id == retry_delivery.id
        assert first_delivery.status == "pending"
        assert first_delivery.error_reason is None
        assert first_delivery.poster_path is None
        assert repeated_draft == draft.model_copy(update={"status": "confirmed"})
        assert await reopened.list_records() == [confirmed]
        assert len(await reopened.list_drafts()) == 1

    asyncio.run(scenario())


def test_two_concurrent_confirmations_create_one_revision(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(make_source(1))
        draft = await store.save_draft(source.id, make_event(), poster_source_id=None)

        first, second = await asyncio.gather(
            store.confirm_draft(draft.id, draft.version),
            store.confirm_draft(draft.id, draft.version),
        )

        assert first == second
        assert first.revision == 1
        assert await store.list_records() == [first]

    asyncio.run(scenario())


def test_draft_with_unfinished_poster_cannot_be_confirmed(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(
            make_source(1).model_copy(update={"telegram_file_id": "poster-file-id"})
        )
        draft = await store.save_draft(source.id, make_event(), poster_source_id=source.id)

        with pytest.raises(ValueError, match="афиш"):
            await store.confirm_draft(draft.id, draft.version)

        assert await store.get_draft(draft.id) == draft
        assert await store.list_records() == []

    asyncio.run(scenario())


def test_correcting_unconfirmed_card_keeps_identity_and_invalidates_old_draft(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(make_source(1))
        original = await store.save_draft(source.id, make_event(), poster_source_id=None)
        assert await store.get_record(original.record_id) is None
        assert await store.list_records() == []

        correction_source = await store.save_source(make_source(2))
        correction = await store.save_draft(
            correction_source.id,
            make_event("Исправленный джем"),
            poster_source_id=None,
            record_id=original.record_id,
            base_revision=original.base_revision,
        )

        assert correction.record_id == original.record_id
        assert correction.version == original.version + 1
        with pytest.raises(ValueError):
            await store.confirm_draft(original.id, original.version)
        record = await store.confirm_draft(correction.id, correction.version)
        assert record.revision == 1
        assert record.data.title == "Исправленный джем"
        assert await store.list_records() == [record]
        assert len(await store.list_drafts()) == 2

    asyncio.run(scenario())


def test_repeated_cancel_and_old_confirmation_return_cancelled_record(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(make_source(1))
        draft = await store.save_draft(source.id, make_event(), poster_source_id=None)
        record = await store.confirm_draft(draft.id, draft.version)

        with pytest.raises(ValueError):
            await store.cancel_record(record.id, record.revision + 1)
        cancelled = await store.cancel_record(record.id, record.revision)
        reopened = Store(store.path)
        assert await reopened.cancel_record(record.id, record.revision) == cancelled
        assert await reopened.confirm_draft(draft.id, draft.version) == cancelled
        assert cancelled.revision == 2
        assert cancelled.status == "cancelled"

    asyncio.run(scenario())


def test_source_processing_is_committed_with_draft(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        payload = make_source(1)
        source = await store.save_source(payload)
        assert source.status == "pending"

        await store.save_draft(source.id, make_event(), poster_source_id=None)
        restored = await Store(store.path).save_source(payload)
        assert restored.id == source.id
        assert restored.status == "processed"
        assert restored.text == payload.text

    asyncio.run(scenario())


def test_source_identity_preserves_original_and_allows_matching_content(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        payload = SourceInput(
            chat_id=111,
            message_id=1,
            text="Подпись с 'кавычками'; <афиша>",
            received_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
            telegram_file_id="test-poster-id",
            media_group_id="test-group-id",
        )
        original = await store.save_source(payload)
        redelivered = await store.save_source(make_source(1, "Изменённая подпись"))
        assert redelivered == original

        another_message = await store.save_source(payload.model_copy(update={"message_id": 2}))
        another_chat = await store.save_source(payload.model_copy(update={"chat_id": 222}))
        assert len({original.id, another_message.id, another_chat.id}) == 3
        records = []
        for source in (original, another_message, another_chat):
            poster_path = Path("posters") / f"{source.id}.jpg"
            (store.path.parent / poster_path).write_bytes(b"saved poster")
            await store.set_source_poster_path(source.id, poster_path)
            draft = await store.save_draft(source.id, make_event(), poster_source_id=source.id)
            records.append(await store.confirm_draft(draft.id, draft.version))
        assert await Store(store.path).list_records() == records
        assert all(record.poster_source_id is not None for record in records)

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["confirm", "reject"])
def test_incorrect_draft_version_cannot_change_pending_card(tmp_path: Path, action: str) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(make_source(1))
        draft = await store.save_draft(source.id, make_event(), poster_source_id=None)

        with pytest.raises(ValueError):
            if action == "confirm":
                await store.confirm_draft(draft.id, draft.version + 1)
            else:
                await store.reject_draft(draft.id, draft.version + 1)

        assert await store.get_draft(draft.id) == draft
        assert await store.list_records() == []

    asyncio.run(scenario())


def test_old_draft_cannot_restore_cancelled_record(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(make_source(1))
        first_draft = await store.save_draft(source.id, make_event(), poster_source_id=None)
        record = await store.confirm_draft(first_draft.id, first_draft.version)

        edit_source = await store.save_source(make_source(2))
        old_edit = await store.save_draft(
            edit_source.id,
            make_event("Изменённый джем"),
            poster_source_id=None,
            record_id=record.id,
            base_revision=record.revision,
        )
        cancelled = await store.cancel_record(record.id, record.revision)

        with pytest.raises(ValueError):
            await store.confirm_draft(old_edit.id, old_edit.version)

        current = await store.get_record(record.id)
        assert current == cancelled
        assert current is not None
        assert current.status == "cancelled"
        assert current.revision == record.revision + 1
        assert current.data == record.data
        assert await store.get_draft(old_edit.id) == old_edit.model_copy(
            update={"status": "superseded"}
        )

    asyncio.run(scenario())


def test_repeating_old_confirmation_returns_current_record(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        first_source = await store.save_source(make_source(1))
        first_draft = await store.save_draft(first_source.id, make_event(), poster_source_id=None)
        first_record = await store.confirm_draft(first_draft.id, first_draft.version)

        edit_source = await store.save_source(make_source(2))
        edit_draft = await store.save_draft(
            edit_source.id,
            make_event("Обновлённый джем"),
            poster_source_id=None,
            record_id=first_record.id,
            base_revision=first_record.revision,
        )
        current_record = await store.confirm_draft(edit_draft.id, edit_draft.version)
        repeated_result = await store.confirm_draft(first_draft.id, first_draft.version)

        assert current_record.revision == first_record.revision + 1
        assert repeated_result == current_record
        assert (await store.get_record(first_record.id)) == current_record

    asyncio.run(scenario())


def test_unknown_ids_are_rejected_without_creating_rows(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()

        assert await store.get_record(404) is None
        assert await store.get_draft(404) is None
        with pytest.raises(ValueError):
            await store.save_draft(404, make_event(), poster_source_id=None)
        with pytest.raises(ValueError):
            await store.confirm_draft(404, 1)
        with pytest.raises(ValueError):
            await store.reject_draft(404, 1)
        with pytest.raises(ValueError):
            await store.cancel_record(404, 1)
        assert await store.list_records() == []
        assert await store.list_drafts() == []

    asyncio.run(scenario())


def test_rejecting_edit_preserves_confirmed_record(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(make_source(1))
        first_draft = await store.save_draft(source.id, make_event(), poster_source_id=None)
        record = await store.confirm_draft(first_draft.id, first_draft.version)

        edit_source = await store.save_source(make_source(2))
        edit_draft = await store.save_draft(
            edit_source.id,
            make_event("Отклонённая правка"),
            poster_source_id=None,
            record_id=record.id,
            base_revision=record.revision,
        )
        await store.reject_draft(edit_draft.id, edit_draft.version)

        assert await store.get_record(record.id) == record
        rejected = await store.get_draft(edit_draft.id)
        assert rejected is not None
        assert rejected.status == "rejected"

    asyncio.run(scenario())


def test_new_edit_draft_invalidates_the_previous_draft(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        source = await store.save_source(make_source(1))
        first_draft = await store.save_draft(source.id, make_event(), poster_source_id=None)
        record = await store.confirm_draft(first_draft.id, first_draft.version)

        first_edit_source = await store.save_source(make_source(2))
        first_edit = await store.save_draft(
            first_edit_source.id,
            make_event("Первая правка"),
            poster_source_id=None,
            record_id=record.id,
            base_revision=record.revision,
        )
        second_edit_source = await store.save_source(make_source(3))
        second_edit = await store.save_draft(
            second_edit_source.id,
            make_event("Вторая правка"),
            poster_source_id=None,
            record_id=record.id,
            base_revision=record.revision,
        )

        with pytest.raises(ValueError):
            await store.confirm_draft(first_edit.id, first_edit.version)
        superseded = await store.get_draft(first_edit.id)
        assert superseded is not None
        assert superseded.status == "superseded"

        updated = await store.confirm_draft(second_edit.id, second_edit.version)
        assert updated.data == make_event("Вторая правка")
        assert len(await store.list_drafts()) == 3

    asyncio.run(scenario())


def test_class_data_round_trip_and_corrupt_record_rejection(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize()
        source = await store.save_source(make_source(1))
        class_data = ClassData(
            title="Popping",
            first_date=date(2026, 11, 10),
            start_time=datetime(2026, 11, 7, 19, 0).time(),
            price="Разовое — 800 ₽\nАбонемент — 2 800 ₽",
        )
        draft = await store.save_draft(source.id, class_data, poster_source_id=None)
        record = await store.confirm_draft(draft.id, draft.version)

        reopened = Store(database)
        await reopened.initialize()
        assert await reopened.get_record(record.id) == record

        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "UPDATE records SET data_json = ? WHERE id = ?",
                ('{"title":"broken"}', record.id),
            )
            connection.commit()

        with pytest.raises(ValidationError):
            await reopened.get_record(record.id)

    asyncio.run(scenario())


def test_failed_draft_insert_rolls_back_superseding_current_draft(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize()
        source = await store.save_source(make_source(1))
        initial = await store.save_draft(source.id, make_event(), poster_source_id=None)
        record = await store.confirm_draft(initial.id, initial.version)
        current_source = await store.save_source(make_source(2))
        current = await store.save_draft(
            current_source.id,
            make_event("Текущий черновик"),
            poster_source_id=None,
            record_id=record.id,
            base_revision=record.revision,
        )
        failing_source = await store.save_source(make_source(3))

        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TRIGGER fail_draft_insert "
                "BEFORE INSERT ON drafts "
                f"WHEN NEW.source_id = {failing_source.id} "
                "BEGIN SELECT RAISE(ABORT, 'simulated insert failure'); END"
            )
            connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            await store.save_draft(
                failing_source.id,
                make_event("Не должна сохраниться"),
                poster_source_id=None,
                record_id=record.id,
                base_revision=record.revision,
            )

        assert await store.get_draft(current.id) == current
        assert await store.get_record(record.id) == record
        assert len(await store.list_drafts()) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_reference", ["poster_source", "base_revision"])
def test_invalid_edit_draft_rolls_back_without_invalidating_current_draft(
    tmp_path: Path, invalid_reference: str
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / f"{invalid_reference}.sqlite3")
        await store.initialize()
        first_source = await store.save_source(make_source(1))
        first_draft = await store.save_draft(first_source.id, make_event(), poster_source_id=None)
        record = await store.confirm_draft(first_draft.id, first_draft.version)

        edit_source = await store.save_source(make_source(2))
        current_draft = await store.save_draft(
            edit_source.id,
            make_event("Текущий черновик"),
            poster_source_id=None,
            record_id=record.id,
            base_revision=record.revision,
        )
        invalid_source = await store.save_source(make_source(3))

        with pytest.raises(ValueError):
            await store.save_draft(
                invalid_source.id,
                make_event("Некорректная правка"),
                poster_source_id=999999 if invalid_reference == "poster_source" else None,
                record_id=record.id,
                base_revision=(
                    record.revision - 1 if invalid_reference == "base_revision" else record.revision
                ),
            )

        assert await store.get_draft(current_draft.id) == current_draft
        assert await store.get_record(record.id) == record

        confirmed = await store.confirm_draft(current_draft.id, current_draft.version)
        assert confirmed.revision == record.revision + 1

    asyncio.run(scenario())
