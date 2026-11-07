from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import web

from dance_community_bot import __main__ as entrypoint
from dance_community_bot.config import Settings
from dance_community_bot.storage import Store

TOKEN = "987654:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmno"
OWNER_ID = 222
WEBHOOK_URL = "https://dance.example.org/telegram/webhook"
WEBHOOK_SECRET = "test_webhook_secret"


class ClosingSession(AiohttpSession):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await super().close()


def make_settings(database_path: Path | None = None) -> Settings:
    return Settings(
        telegram_bot_token=TOKEN,
        telegram_owner_id=OWNER_ID,
        telegram_webhook_url=WEBHOOK_URL,
        telegram_webhook_secret=WEBHOOK_SECRET,
        database_path=database_path or Path("data/test.sqlite3"),
    )


def test_main_runs_webhook_server_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    dispatcher = object()
    app = web.Application()
    calls: list[tuple[object, str | None, int]] = []

    def run_app(app_arg: object, *, host: str, port: int) -> None:
        calls.append((app_arg, host, port))

    monkeypatch.setattr(entrypoint, "load_settings", lambda: settings)
    monkeypatch.setattr(entrypoint, "Bot", lambda token: object())
    monkeypatch.setattr(entrypoint, "create_dispatcher", lambda current_settings, store: dispatcher)
    monkeypatch.setattr(
        entrypoint,
        "create_webhook_app",
        lambda current_settings, bot, current_dispatcher: app,
        raising=False,
    )
    monkeypatch.setattr(entrypoint, "web", SimpleNamespace(run_app=run_app), raising=False)

    exit_code = entrypoint.main()

    assert exit_code == 0
    assert calls == [(app, "127.0.0.1", 8080)]


def test_webhook_registration_and_shutdown_close_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = ClosingSession()
    bot = Bot(token=TOKEN, session=session)
    settings = make_settings(tmp_path / "dance.sqlite3")
    dispatcher = entrypoint.create_dispatcher(settings, Store(settings.database_path))
    lifecycle: list[str] = []

    async def on_startup(bot: Bot) -> None:
        lifecycle.append("startup")

    async def on_shutdown(bot: Bot) -> None:
        lifecycle.append("shutdown")

    dispatcher.startup.register(on_startup)
    dispatcher.shutdown.register(on_shutdown)
    set_webhook = AsyncMock()
    get_updates = AsyncMock()
    delete_webhook = AsyncMock()
    monkeypatch.setattr(bot, "set_webhook", set_webhook)
    monkeypatch.setattr(bot, "get_updates", get_updates)
    monkeypatch.setattr(bot, "delete_webhook", delete_webhook)

    async def run() -> None:
        app = entrypoint.create_webhook_app(settings, bot, dispatcher)
        runner = web.AppRunner(app)
        await runner.setup()
        await runner.cleanup()

    asyncio.run(run())

    set_webhook.assert_awaited_once_with(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dispatcher.resolve_used_update_types(),
    )
    get_updates.assert_not_awaited()
    delete_webhook.assert_not_awaited()
    assert lifecycle == ["startup", "shutdown"]
    assert session.closed


@pytest.mark.parametrize("failure_point", ["registration", "startup", "shutdown"])
def test_webhook_lifecycle_failure_closes_session_and_dispatcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_point: str
) -> None:
    session = ClosingSession()
    bot = Bot(token=TOKEN, session=session)
    settings = make_settings(tmp_path / "dance.sqlite3")
    dispatcher = entrypoint.create_dispatcher(settings, Store(settings.database_path))
    lifecycle: list[str] = []

    async def on_startup(bot: Bot) -> None:
        lifecycle.append("startup")
        if failure_point == "startup":
            raise RuntimeError("startup failed")

    async def on_shutdown(bot: Bot) -> None:
        lifecycle.append("shutdown")
        if failure_point == "shutdown":
            raise RuntimeError("shutdown failed")

    dispatcher.startup.register(on_startup)
    dispatcher.shutdown.register(on_shutdown)
    set_webhook = AsyncMock()
    if failure_point == "registration":
        set_webhook.side_effect = RuntimeError("registration failed")
    monkeypatch.setattr(bot, "set_webhook", set_webhook)

    async def run() -> None:
        app = entrypoint.create_webhook_app(settings, bot, dispatcher)
        runner = web.AppRunner(app)
        try:
            with pytest.raises(RuntimeError, match=f"{failure_point} failed"):
                await runner.setup()
                await runner.cleanup()
        finally:
            await runner.cleanup()

    asyncio.run(run())

    if failure_point == "registration":
        assert "shutdown" in lifecycle
    else:
        assert lifecycle == ["startup", "shutdown"]
    assert session.closed
