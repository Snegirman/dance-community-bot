from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from telegram_transport import RecordingSession

from dance_community_bot.config import Settings
from dance_community_bot.models import EventData, SourceInput
from dance_community_bot.publishing import publish_week
from dance_community_bot.scheduler import check_publication
from dance_community_bot.storage import Store
from dance_community_bot.weekly import build_weekly_post

OWNER_ID = 111
GROUP_ID = -100123
TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
MOSCOW = ZoneInfo("Europe/Moscow")


def make_settings(database_path: Path, publication_time: time | None) -> Settings:
    return Settings(
        telegram_bot_token=TOKEN,
        telegram_owner_id=OWNER_ID,
        telegram_webhook_url="https://dance.example.org/telegram/webhook",
        telegram_webhook_secret="webhook_secret",
        telegram_group_id=GROUP_ID,
        weekly_publication_time=publication_time,
        database_path=database_path,
    )


async def add_event(store: Store, *, description: str | None = None) -> int:
    source = await store.save_source(
        SourceInput(
            chat_id=OWNER_ID,
            message_id=1,
            text="Джем",
            received_at=datetime(2026, 11, 8, 12, tzinfo=MOSCOW),
        )
    )
    draft = await store.save_draft(
        source.id,
        EventData(
            title="Джем",
            event_type="Джем",
            start_date=date(2026, 11, 10),
            description=description,
        ),
        poster_source_id=None,
    )
    return (await store.confirm_draft(draft.id, draft.version)).id


def create_bot(session: RecordingSession) -> Bot:
    return Bot(TOKEN, session=session)


def message_count(session: RecordingSession, chat_id: int) -> int:
    return sum(
        isinstance(request, SendMessage) and request.chat_id == chat_id
        for request in session.requests
    )


def scheduler_notice_kinds(database_path: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(database_path)) as connection:
        return connection.execute(
            "SELECT kind, status FROM scheduler_notices ORDER BY kind"
        ).fetchall()


def test_scheduler_catches_midnight_target_across_sunday_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(0, 0))
        await add_event(store)
        settings = make_settings(store.path, time(0, 0))
        session = RecordingSession(GROUP_ID)
        bot = create_bot(session)

        await check_publication(
            datetime(2026, 11, 8, 23, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )
        await check_publication(datetime(2026, 11, 9, 0, 0, 5, tzinfo=MOSCOW), settings, store, bot)

        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
        assert publication is not None
        assert publication.status == "delivered"
        assert message_count(session, GROUP_ID) == 1
        await bot.session.close()

    asyncio.run(scenario())


def test_scheduler_catches_wakeup_late_within_target_minute(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        await add_event(store)
        settings = make_settings(store.path, time(9, 0))
        session = RecordingSession(GROUP_ID)
        bot = create_bot(session)

        await check_publication(
            datetime(2026, 11, 9, 8, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )
        await check_publication(
            datetime(2026, 11, 9, 9, 0, 45, tzinfo=MOSCOW), settings, store, bot
        )

        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
        assert publication is not None
        assert publication.status == "delivered"
        assert message_count(session, GROUP_ID) == 1
        await bot.session.close()

    asyncio.run(scenario())


def test_empty_week_does_not_become_missed_after_notification_or_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize(time(9, 0))
        settings = make_settings(database, time(9, 0))
        session = RecordingSession(GROUP_ID)
        bot = create_bot(session)

        await check_publication(
            datetime(2026, 11, 9, 8, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )
        await check_publication(datetime(2026, 11, 9, 9, 0, 5, tzinfo=MOSCOW), settings, store, bot)
        await check_publication(datetime(2026, 11, 9, 9, 1, 5, tzinfo=MOSCOW), settings, store, bot)

        reopened = Store(database)
        await reopened.initialize()
        await reopened.recover_automation_notices_on_startup()
        await check_publication(
            datetime(2026, 11, 9, 9, 2, 5, tzinfo=MOSCOW), settings, reopened, bot
        )

        assert scheduler_notice_kinds(database) == [("empty", "sent")]
        assert message_count(session, OWNER_ID) == 1
        assert message_count(session, GROUP_ID) == 0
        await bot.session.close()

    asyncio.run(scenario())


def test_new_week_tick_reports_previous_week_missed_without_publication(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        await add_event(store)
        settings = make_settings(store.path, time(9, 0))
        session = RecordingSession(GROUP_ID)
        bot = create_bot(session)

        await check_publication(
            datetime(2026, 11, 9, 8, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )
        await check_publication(
            datetime(2026, 11, 16, 8, 0, 0, tzinfo=MOSCOW), settings, store, bot
        )

        assert await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9)) is None
        assert await store.get_publication_for_week(GROUP_ID, date(2026, 11, 16)) is None
        assert message_count(session, GROUP_ID) == 0
        assert message_count(session, OWNER_ID) == 1
        assert "09.11.2026" in session.messages[-1]
        assert "пропущ" in session.messages[-1].casefold()
        await bot.session.close()

    asyncio.run(scenario())


def test_new_revision_can_publish_after_old_revision_missed_deadline(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(8, 0))
        await add_event(store)
        settings = make_settings(store.path, time(8, 0))
        session = RecordingSession(GROUP_ID)
        bot = create_bot(session)

        await check_publication(
            datetime(2026, 11, 9, 8, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )
        await store.set_weekly_publication_time(
            time(9, 0),
            expected_revision=0,
            changed_at=datetime(2026, 11, 9, 8, 59, 58, tzinfo=MOSCOW),
        )
        await check_publication(datetime(2026, 11, 9, 9, 0, 5, tzinfo=MOSCOW), settings, store, bot)

        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
        assert publication is not None
        assert publication.status == "delivered"
        assert message_count(session, GROUP_ID) == 1
        await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("saved_second", [0, 2])
def test_new_deadline_already_passed_when_saved_does_not_catch_up(
    tmp_path: Path, saved_second: int
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(8, 0))
        await add_event(store)
        settings = make_settings(store.path, time(8, 0))
        session = RecordingSession(GROUP_ID)
        bot = create_bot(session)

        await check_publication(
            datetime(2026, 11, 9, 8, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )
        await store.set_weekly_publication_time(
            time(9, 0),
            expected_revision=0,
            changed_at=datetime(2026, 11, 9, 9, 0, saved_second, tzinfo=MOSCOW),
        )
        await check_publication(datetime(2026, 11, 9, 9, 0, 5, tzinfo=MOSCOW), settings, store, bot)

        assert await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9)) is None
        assert message_count(session, GROUP_ID) == 0
        await bot.session.close()

    asyncio.run(scenario())


def test_pending_notices_for_two_setting_revisions_are_delivered_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        week_start = date(2026, 11, 9)
        for revision in (0, 1):
            await store.save_scheduler_notice(
                week_start,
                "missed",
                f"Пропущен срок настройки {revision}.",
                setting_revision=revision,
            )
        settings = make_settings(store.path, None)
        session = RecordingSession(GROUP_ID)
        bot = create_bot(session)
        try:
            await check_publication(datetime(2026, 11, 9, 12, tzinfo=MOSCOW), settings, store, bot)
            await check_publication(
                datetime(2026, 11, 9, 12, 0, 10, tzinfo=MOSCOW), settings, store, bot
            )

            assert message_count(session, OWNER_ID) == 2
            assert set(session.messages) == {
                "Пропущен срок настройки 0.",
                "Пропущен срок настройки 1.",
            }
            assert (await store.get_automation_status()).pending_scheduler_notices == 0
        finally:
            await bot.session.close()

    asyncio.run(scenario())


class LoseGroupResponseSession(RecordingSession):
    def __init__(self) -> None:
        super().__init__(GROUP_ID)
        self.group_attempts = 0

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        result = await super().make_request(bot, method, timeout)
        if isinstance(method, SendMessage) and method.chat_id == GROUP_ID:
            self.group_attempts += 1
            if self.group_attempts == 1:
                raise TimeoutError("reply lost after possible group delivery")
        return result


def test_unknown_automatic_group_delivery_is_not_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        await add_event(store)
        settings = make_settings(store.path, time(9, 0))
        session = LoseGroupResponseSession()
        bot = create_bot(session)

        await check_publication(
            datetime(2026, 11, 9, 8, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )
        await check_publication(datetime(2026, 11, 9, 9, 0, 5, tzinfo=MOSCOW), settings, store, bot)
        await check_publication(
            datetime(2026, 11, 9, 9, 0, 15, tzinfo=MOSCOW), settings, store, bot
        )

        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
        assert publication is not None
        assert publication.status == "unknown"
        assert session.group_attempts == 1
        await bot.session.close()

    asyncio.run(scenario())


class LoseOwnerResponseSession(RecordingSession):
    def __init__(self) -> None:
        super().__init__(GROUP_ID)
        self.owner_attempts = 0

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        result = await super().make_request(bot, method, timeout)
        if isinstance(method, SendMessage) and method.chat_id == OWNER_ID:
            self.owner_attempts += 1
            if self.owner_attempts == 1:
                raise TimeoutError("reply lost after possible owner notification")
        return result


def test_owner_notice_retries_after_lost_reply_and_process_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize(None)
        await store.save_scheduler_notice(
            date(2026, 11, 9), "empty", "На неделю с 09.11.2026 записей нет."
        )
        settings = make_settings(database, None)
        session = LoseOwnerResponseSession()
        bot = create_bot(session)

        await check_publication(datetime(2026, 11, 9, 9, 1, 5, tzinfo=MOSCOW), settings, store, bot)
        assert (await store.get_automation_status()).pending_scheduler_notices == 1

        reopened = Store(database)
        await reopened.initialize()
        await reopened.recover_automation_notices_on_startup()
        await check_publication(
            datetime(2026, 11, 9, 9, 2, 5, tzinfo=MOSCOW), settings, reopened, bot
        )

        assert session.owner_attempts == 2
        assert message_count(session, OWNER_ID) == 2
        assert (await reopened.get_automation_status()).pending_scheduler_notices == 0
        await bot.session.close()

    asyncio.run(scenario())


class TelegramLimitSession(RecordingSession):
    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        if (
            isinstance(method, SendMessage)
            and method.chat_id == OWNER_ID
            and len(method.text.encode("utf-16-le")) // 2 > 4096
        ):
            raise TelegramBadRequest(method=method, message="message is too long")
        return await super().make_request(bot, method, timeout)


def test_long_owner_change_notification_is_split_at_telegram_utf16_limit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        record_id = await add_event(store, description="До")
        session = TelegramLimitSession(GROUP_ID)
        bot = create_bot(session)
        post = await build_weekly_post(date(2026, 11, 9), store)
        result = await publish_week(post, GROUP_ID, store, bot)
        assert result.status == "delivered"
        record = await store.get_record(record_id)
        assert record is not None
        source = await store.save_source(
            SourceInput(
                chat_id=OWNER_ID,
                message_id=2,
                text="Правка описания",
                received_at=datetime(2026, 11, 8, 12, tzinfo=MOSCOW),
            )
        )
        description = "🙂" * 5000
        draft = await store.save_draft(
            source.id,
            EventData(
                title="Джем",
                event_type="Джем",
                start_date=date(2026, 11, 10),
                description=description,
            ),
            poster_source_id=None,
            record_id=record_id,
            base_revision=record.revision,
        )
        await store.confirm_draft(draft.id, draft.version)

        await check_publication(
            datetime(2026, 11, 9, 12, tzinfo=MOSCOW),
            make_settings(store.path, None),
            store,
            bot,
        )

        owner_messages = [
            request.text
            for request in session.requests
            if isinstance(request, SendMessage) and request.chat_id == OWNER_ID
        ]
        assert owner_messages
        assert all(len(text.encode("utf-16-le")) // 2 <= 4096 for text in owner_messages)
        assert sum(text.count("🙂") for text in owner_messages) == 5000
        assert await store.list_pending_publication_notifications() == []
        await bot.session.close()

    asyncio.run(scenario())
