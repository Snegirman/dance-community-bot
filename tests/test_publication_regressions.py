from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Update
from pydantic import ValidationError
from telegram_transport import RecordingSession
from test_publication_handlers import (
    GROUP_ID,
    OWNER_ID,
    TOKEN,
    _callback_update,
    add_event,
    make_settings,
    private_message,
)

from dance_community_bot.bot import create_dispatcher
from dance_community_bot.models import TextPostPart, WeeklyPost
from dance_community_bot.storage import Store
from dance_community_bot.weekly import build_weekly_post


class LimitedSession(RecordingSession):
    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        if isinstance(method, SendMessage) and len(method.text.encode("utf-16-le")) // 2 > 4096:
            raise TelegramBadRequest(method=method, message="message is too long")
        return await super().make_request(bot, method, timeout)


@pytest.mark.parametrize("group_id", [0, 111, -(2**80), 2**80])
def test_invalid_group_configuration_is_rejected_before_sqlite(
    tmp_path: Path, group_id: int
) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path / "dance.sqlite3", group_id)


def test_oversized_callback_identifier_is_rejected_without_sqlite_overflow(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        session = RecordingSession(OWNER_ID)
        async with Bot(TOKEN, session=session) as bot:
            dispatcher = create_dispatcher(make_settings(store.path), store)
            await dispatcher.feed_update(
                bot,
                _callback_update(bot, message_id=1, callback_data=f"pub:confirm:{2**80}:1"),
            )
        assert any(isinstance(request, AnswerCallbackQuery) for request in session.requests)
        assert session.messages == []

    asyncio.run(scenario())


def test_long_interrupted_publication_has_complete_status_and_bound_controls(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "dance.sqlite3")
        await store.initialize()
        await add_event(store)
        original = await build_weekly_post(date(2026, 11, 9), store)
        post = WeeklyPost(
            week_start=original.week_start,
            catalog_revision=original.catalog_revision,
            parts=tuple(TextPostPart(text=f"Часть {number}") for number in range(300)),
            records=original.records,
            occurrences=original.occurrences,
        )
        claim = await store.claim_publication(post, GROUP_ID, "interrupted")
        assert claim.publication is not None
        await store.recover_publications_on_startup()
        session = LimitedSession(OWNER_ID)
        async with Bot(TOKEN, session=session) as bot:
            dispatcher = create_dispatcher(make_settings(store.path), store)
            await dispatcher.feed_update(
                bot,
                Update(update_id=1, message=private_message(bot, 50, "/publish 10.11.2026")),
            )
        messages = [request for request in session.requests if isinstance(request, SendMessage)]
        assert len(messages) >= 2
        assert all(request.reply_markup is None for request in messages[:-1])
        assert messages[-1].reply_markup is not None
        assert "300 —" in "".join(request.text for request in messages)
        publication = await store.get_publication(claim.publication.id)
        assert publication is not None
        assert publication.owner_message_id == 100 + len(messages)
        assert publication.parts[0].status == "unknown"

    asyncio.run(scenario())
