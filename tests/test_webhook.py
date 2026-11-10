from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import Message
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from aiohttp.web_urldispatcher import AbstractRoute
from yarl import URL

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


def make_settings(
    database_path: Path | None = None,
    *,
    webhook_url: str = WEBHOOK_URL,
) -> Settings:
    return Settings(
        telegram_bot_token=TOKEN,
        telegram_owner_id=OWNER_ID,
        telegram_webhook_url=webhook_url,
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
        dispatcher.feed_raw_update = feed_update
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


def test_webhook_waits_for_processing_before_acknowledging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run() -> None:
        bot = Bot(token=TOKEN)
        settings = make_settings(tmp_path / "dance.sqlite3")
        dispatcher = Dispatcher()
        processing_started = asyncio.Event()
        allow_processing = asyncio.Event()

        async def delayed_processing(_message: Message) -> None:
            processing_started.set()
            await allow_processing.wait()

        dispatcher.message.register(delayed_processing)
        original_feed_webhook_update = dispatcher.feed_webhook_update

        async def quickly_timeout(current_bot: Bot, update: dict[str, object]) -> object:
            return await original_feed_webhook_update(current_bot, update, _timeout=0.01)

        monkeypatch.setattr(dispatcher, "feed_webhook_update", quickly_timeout)
        app = entrypoint.create_webhook_app(settings, bot, dispatcher)
        route = find_webhook_route(app)
        response_task = asyncio.create_task(
            route.handler(mocked_post_request(app, make_update(), WEBHOOK_SECRET))
        )
        try:
            await asyncio.wait_for(processing_started.wait(), timeout=1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(response_task), timeout=0.03)
            assert not response_task.done()

            allow_processing.set()
            response = await asyncio.wait_for(response_task, timeout=1)

            assert response.status == 200
        finally:
            allow_processing.set()
            if not response_task.done():
                await response_task
            await asyncio.sleep(0)
            await bot.session.close()

    asyncio.run(run())


def test_webhook_timeout_cancels_and_awaits_processing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run() -> None:
        original_timeout = asyncio.timeout
        monkeypatch.setattr(asyncio, "timeout", lambda _: original_timeout(0.01))

        bot = Bot(token=TOKEN)
        settings = make_settings(tmp_path / "dance.sqlite3")
        dispatcher = Dispatcher()
        processing_started = asyncio.Event()
        processing_cancelled = asyncio.Event()
        processing_finalized = asyncio.Event()
        allow_processing = asyncio.Event()

        async def never_finishing_processing(_message: Message) -> None:
            processing_started.set()
            try:
                await allow_processing.wait()
            except asyncio.CancelledError:
                processing_cancelled.set()
                raise
            finally:
                processing_finalized.set()

        dispatcher.message.register(never_finishing_processing)
        original_feed_webhook_update = dispatcher.feed_webhook_update

        async def quickly_timeout(current_bot: Bot, update: dict[str, object]) -> object:
            return await original_feed_webhook_update(current_bot, update, _timeout=0.01)

        monkeypatch.setattr(dispatcher, "feed_webhook_update", quickly_timeout)
        app = entrypoint.create_webhook_app(settings, bot, dispatcher)
        route = find_webhook_route(app)
        try:
            response = await route.handler(mocked_post_request(app, make_update(), WEBHOOK_SECRET))

            assert response.status == 503
            assert processing_started.is_set()
            assert processing_cancelled.is_set()
            assert processing_finalized.is_set()
        finally:
            allow_processing.set()
            if not processing_finalized.is_set():
                await asyncio.wait_for(processing_finalized.wait(), timeout=1)
            await asyncio.sleep(0)
            await bot.session.close()

    asyncio.run(run())


def test_webhook_does_not_acknowledge_handler_failure_as_success(tmp_path: Path) -> None:
    async def run() -> None:
        bot = Bot(token=TOKEN)
        settings = make_settings(tmp_path / "dance.sqlite3")
        dispatcher = Dispatcher()

        async def fail_processing(_message: Message) -> None:
            raise RuntimeError("handler failed")

        dispatcher.message.register(fail_processing)
        app = entrypoint.create_webhook_app(settings, bot, dispatcher)
        route = find_webhook_route(app)
        request = mocked_post_request(app, make_update(), WEBHOOK_SECRET)
        try:
            with pytest.raises(RuntimeError, match="handler failed"):
                await route.handler(request)
        finally:
            await bot.session.close()

    asyncio.run(run())


def test_webhook_acknowledges_update_without_matching_handler(tmp_path: Path) -> None:
    async def run() -> None:
        bot = Bot(token=TOKEN)
        settings = make_settings(tmp_path / "dance.sqlite3")
        app = entrypoint.create_webhook_app(settings, bot, Dispatcher())
        route = find_webhook_route(app)
        try:
            response = await route.handler(mocked_post_request(app, make_update(), WEBHOOK_SECRET))

            assert response.status == 200
        finally:
            await bot.session.close()

    asyncio.run(run())


def test_webhook_sends_returned_telegram_method_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run() -> None:
        bot = Bot(token=TOKEN)
        settings = make_settings(tmp_path / "dance.sqlite3")
        dispatcher = Dispatcher()
        method = SendMessage(chat_id=OWNER_ID, text="done")

        async def return_method(_message: Message) -> SendMessage:
            return method

        dispatcher.message.register(return_method)
        make_request = AsyncMock(return_value=None)
        monkeypatch.setattr(bot.session, "make_request", make_request)
        app = entrypoint.create_webhook_app(settings, bot, dispatcher)
        route = find_webhook_route(app)
        try:
            response = await route.handler(mocked_post_request(app, make_update(), WEBHOOK_SECRET))

            assert response.status == 200
            make_request.assert_awaited_once()
            assert make_request.await_args.args[1] is method
        finally:
            await bot.session.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("configured_path", "request_path"),
    [
        ("/telegram/%77ebhook", "/telegram/%77ebhook"),
        ("/telegram/%2Fwebhook", "/telegram/%2Fwebhook"),
        ("/telegram/тест", "/telegram/тест"),
    ],
)
def test_webhook_route_matches_aiohttp_safe_path(
    tmp_path: Path, configured_path: str, request_path: str
) -> None:
    async def run() -> None:
        bot = Bot(token=TOKEN)
        settings = make_settings(
            tmp_path / "dance.sqlite3",
            webhook_url=f"https://dance.example.org{configured_path}",
        )
        app = entrypoint.create_webhook_app(
            settings,
            bot,
            Dispatcher(),
        )
        request = make_mocked_request("POST", request_path, app=app)
        try:
            match_info = await app.router.resolve(request)

            assert match_info.route.resource is not None
            assert (
                match_info.route.resource.canonical == URL(settings.telegram_webhook_url).path_safe
            )
        finally:
            await bot.session.close()

    asyncio.run(run())
