from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from aiohttp.web_urldispatcher import AbstractRoute

from dance_community_bot import __main__ as entrypoint
from dance_community_bot.bot import HELP_TEXT, create_dispatcher
from dance_community_bot.config import Settings
from dance_community_bot.storage import Store

TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
OWNER_ID = 222
WEBHOOK_URL = "https://dance.example.org/telegram/webhook"
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_SECRET = "test_webhook_secret"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def make_settings(database_path: Path | None = None) -> Settings:
    return Settings(
        telegram_bot_token=TOKEN,
        telegram_owner_id=OWNER_ID,
        telegram_webhook_url=WEBHOOK_URL,
        telegram_webhook_secret=WEBHOOK_SECRET,
        database_path=database_path or Path("data/test.sqlite3"),
    )


def make_update(
    *, sender_id: int = OWNER_ID, chat_type: str = "private", chat_id: int = OWNER_ID
) -> dict[str, object]:
    return {
        "update_id": sender_id + chat_id,
        "message": {
            "message_id": 1,
            "date": 1793836800,
            "chat": {"id": chat_id, "type": chat_type, "first_name": "Owner"},
            "from": {"id": sender_id, "is_bot": False, "first_name": "Owner"},
            "text": "/help",
            "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
        },
    }


def find_webhook_route(app: web.Application) -> AbstractRoute:
    routes = [route for route in app.router.routes() if route.method == "POST"]
    assert len(routes) == 1
    route = routes[0]
    assert route.resource is not None
    assert route.resource.canonical == WEBHOOK_PATH
    return route


def mocked_post_request(
    app: web.Application, update: dict[str, object], secret: str | None
) -> web.Request:
    headers = {} if secret is None else {SECRET_HEADER: secret}
    request = make_mocked_request("POST", WEBHOOK_PATH, headers=headers, app=app)
    request.json = AsyncMock(return_value=update)
    return request


def test_webhook_rejects_missing_or_wrong_secret_before_dispatch(tmp_path: Path) -> None:
    async def run() -> None:
        bot = Bot(token=TOKEN)
        settings = make_settings(tmp_path / "dance.sqlite3")
        dispatcher = create_dispatcher(settings, Store(settings.database_path))
        feed_update = AsyncMock()
        dispatcher.feed_webhook_update = feed_update
        app = entrypoint.create_webhook_app(settings, bot, dispatcher)
        route = find_webhook_route(app)
        try:
            for secret in (None, "wrong_secret"):
                request = mocked_post_request(app, make_update(), secret)
                response = await route.handler(request)
                assert response.status == 401
                request.json.assert_not_awaited()
            feed_update.assert_not_awaited()
        finally:
            await bot.session.close()

    asyncio.run(run())


def test_help_webhook_dispatches_only_for_owner_in_private_chat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run() -> None:
        bot = Bot(token=TOKEN)
        settings = make_settings(tmp_path / "dance.sqlite3")
        dispatcher = create_dispatcher(settings, Store(settings.database_path))
        make_request = AsyncMock(return_value=None)
        monkeypatch.setattr(bot.session, "make_request", make_request)
        app = entrypoint.create_webhook_app(settings, bot, dispatcher)
        route = find_webhook_route(app)
        try:
            owner_request = mocked_post_request(app, make_update(), WEBHOOK_SECRET)
            owner_response = await route.handler(owner_request)
            assert owner_response.status == 200
            assert make_request.await_count == 1
            sent_method = make_request.await_args.args[1]
            assert sent_method.chat_id == OWNER_ID
            assert sent_method.text == HELP_TEXT

            for update in (
                make_update(sender_id=OWNER_ID + 1, chat_id=OWNER_ID + 1),
                make_update(sender_id=OWNER_ID, chat_id=-100123, chat_type="group"),
            ):
                response = await route.handler(mocked_post_request(app, update, WEBHOOK_SECRET))
                assert response.status == 200
            assert make_request.await_count == 1
        finally:
            await bot.session.close()

    asyncio.run(run())


def test_webhook_does_not_acknowledge_handler_failure_as_success(tmp_path: Path) -> None:
    async def run() -> None:
        bot = Bot(token=TOKEN)
        settings = make_settings(tmp_path / "dance.sqlite3")
        dispatcher = create_dispatcher(settings, Store(settings.database_path))
        feed_update = AsyncMock(side_effect=RuntimeError("handler failed"))
        dispatcher.feed_webhook_update = feed_update
        app = entrypoint.create_webhook_app(settings, bot, dispatcher)
        route = find_webhook_route(app)
        request = mocked_post_request(app, make_update(), WEBHOOK_SECRET)
        try:
            with pytest.raises(RuntimeError, match="handler failed"):
                await route.handler(request)
        finally:
            await bot.session.close()

    asyncio.run(run())
