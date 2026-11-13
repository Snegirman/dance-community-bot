from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
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


def make_settings(
    database_path: Path,
    *,
    group_id: int | None = GROUP_ID,
    publication_time: time | None = time(9, 0),
) -> Settings:
    return Settings(
        telegram_bot_token=TOKEN,
        telegram_owner_id=OWNER_ID,
        telegram_webhook_url="https://dance.example.org/telegram/webhook",
        telegram_webhook_secret="webhook_secret",
        telegram_group_id=group_id,
        weekly_publication_time=publication_time,
        database_path=database_path,
    )


async def add_event(store: Store) -> None:
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
        EventData(title="Джем", event_type="Джем", start_date=date(2026, 11, 10)),
        poster_source_id=None,
    )
    await store.confirm_draft(draft.id, draft.version)


def create_bot() -> tuple[Bot, RecordingSession]:
    session = RecordingSession(GROUP_ID)
    return Bot(TOKEN, session=session), session


def message_count(session: RecordingSession, chat_id: int) -> int:
    return sum(
        isinstance(request, SendMessage) and request.chat_id == chat_id
        for request in session.requests
    )


def test_crossing_monday_publication_minute_sends_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        await add_event(store)
        settings = make_settings(store.path)
        bot, session = create_bot()

        await check_publication(
            datetime(2026, 11, 9, 8, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )
        assert await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9)) is None

        await check_publication(datetime(2026, 11, 9, 9, 0, 5, tzinfo=MOSCOW), settings, store, bot)
        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
        assert publication is not None
        assert publication.status == "delivered"
        assert message_count(session, GROUP_ID) == 1

        await check_publication(
            datetime(2026, 11, 9, 9, 0, 15, tzinfo=MOSCOW), settings, store, bot
        )
        assert message_count(session, GROUP_ID) == 1
        await bot.session.close()

    asyncio.run(scenario())


def test_late_or_restarted_scheduler_notifies_without_catching_up(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = tmp_path / "dance.sqlite3"
        store = Store(database)
        await store.initialize(time(9, 0))
        await add_event(store)
        settings = make_settings(database)
        bot, session = create_bot()

        await check_publication(
            datetime(2026, 11, 10, 9, 1, 5, tzinfo=MOSCOW), settings, store, bot
        )
        assert await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9)) is None
        assert message_count(session, OWNER_ID) == 1
        assert "пропущ" in session.messages[-1].casefold()

        await check_publication(
            datetime(2026, 11, 10, 9, 1, 15, tzinfo=MOSCOW), settings, store, bot
        )
        restarted_store = Store(database)
        await restarted_store.initialize()
        await check_publication(
            datetime(2026, 11, 10, 9, 2, 5, tzinfo=MOSCOW), settings, restarted_store, bot
        )
        assert message_count(session, OWNER_ID) == 1
        assert message_count(session, GROUP_ID) == 0
        await bot.session.close()

    asyncio.run(scenario())


def test_wakeup_after_publication_minute_notifies_without_catching_up(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        await add_event(store)
        settings = make_settings(store.path)
        bot, session = create_bot()

        await check_publication(
            datetime(2026, 11, 9, 8, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )
        await check_publication(datetime(2026, 11, 9, 9, 1, 5, tzinfo=MOSCOW), settings, store, bot)

        assert await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9)) is None
        assert message_count(session, GROUP_ID) == 0
        assert message_count(session, OWNER_ID) == 1
        assert "пропущ" in session.messages[-1].casefold()
        await bot.session.close()

    asyncio.run(scenario())


def test_empty_week_notifies_once_and_is_not_published(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        settings = make_settings(store.path)
        await store.initialize(settings.weekly_publication_time)
        bot, session = create_bot()

        await check_publication(
            datetime(2026, 11, 9, 8, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )
        await check_publication(datetime(2026, 11, 9, 9, 0, 5, tzinfo=MOSCOW), settings, store, bot)
        await check_publication(
            datetime(2026, 11, 9, 9, 0, 15, tzinfo=MOSCOW), settings, store, bot
        )

        assert await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9)) is None
        assert message_count(session, GROUP_ID) == 0
        assert message_count(session, OWNER_ID) == 1
        assert "записей нет" in session.messages[-1].casefold()
        await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("group_id", "publication_time"),
    [(None, time(9, 0)), (GROUP_ID, None)],
)
def test_missing_time_or_group_disables_automatic_publication(
    tmp_path: Path,
    group_id: int | None,
    publication_time: time | None,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / f"dance-{group_id}-{publication_time}.sqlite3")
        await store.initialize(publication_time)
        await add_event(store)
        settings = make_settings(store.path, group_id=group_id, publication_time=publication_time)
        bot, session = create_bot()

        await check_publication(
            datetime(2026, 11, 9, 8, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )
        await check_publication(datetime(2026, 11, 9, 9, 0, 5, tzinfo=MOSCOW), settings, store, bot)

        assert message_count(session, GROUP_ID) == 0
        assert await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9)) is None
        await bot.session.close()

    asyncio.run(scenario())


def test_manual_and_automatic_publication_share_one_group_attempt(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize(time(9, 0))
        await add_event(store)
        settings = make_settings(store.path)
        bot, session = create_bot()
        post = await build_weekly_post(date(2026, 11, 9), store)
        await check_publication(
            datetime(2026, 11, 9, 8, 59, 55, tzinfo=MOSCOW), settings, store, bot
        )

        await asyncio.gather(
            check_publication(datetime(2026, 11, 9, 9, 0, 5, tzinfo=MOSCOW), settings, store, bot),
            publish_week(post, GROUP_ID, store, bot),
        )

        publication = await store.get_publication_for_week(GROUP_ID, date(2026, 11, 9))
        assert publication is not None
        assert publication.status == "delivered"
        assert message_count(session, GROUP_ID) == 1
        await bot.session.close()

    asyncio.run(scenario())
