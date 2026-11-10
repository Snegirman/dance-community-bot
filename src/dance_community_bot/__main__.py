"""Запуск Telegram-бота через входящие webhook-запросы."""

import asyncio
import secrets
import sys
from collections.abc import AsyncIterator

from aiogram import Bot, Dispatcher
from aiogram.methods import TelegramMethod
from aiohttp import web
from yarl import URL

from dance_community_bot.bot import create_dispatcher
from dance_community_bot.config import Settings, SettingsError, load_settings
from dance_community_bot.storage import Store

WEBHOOK_HOST = "127.0.0.1"


def create_webhook_app(settings: Settings, bot: Bot, dispatcher: Dispatcher) -> web.Application:
    """Создать aiohttp-приложение для одного Telegram webhook."""
    app = web.Application()
    secret = settings.telegram_webhook_secret.get_secret_value()
    path = URL(settings.telegram_webhook_url).path_safe

    async def handle_webhook(request: web.Request) -> web.Response:
        if not secrets.compare_digest(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret
        ):
            return web.Response(body="Unauthorized", status=401)

        try:
            async with asyncio.timeout(50):
                update = await request.json(loads=bot.session.json_loads)
                result = await dispatcher.feed_raw_update(bot=bot, update=update)
                if isinstance(result, TelegramMethod):
                    await bot(result)
        except TimeoutError:
            return web.Response(status=503)

        return web.json_response({})

    app.router.add_post(path, handle_webhook)

    async def manage_bot_lifecycle(_: web.Application) -> AsyncIterator[None]:
        try:
            await bot.set_webhook(
                url=settings.telegram_webhook_url,
                secret_token=settings.telegram_webhook_secret.get_secret_value(),
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
            await dispatcher.emit_startup(bot=bot)
            yield
        finally:
            try:
                await dispatcher.emit_shutdown(bot=bot)
            finally:
                await bot.session.close()

    app.cleanup_ctx.append(manage_bot_lifecycle)
    return app


def main() -> int:
    """Загрузить настройки, запустить webhook или сообщить безопасную ошибку."""
    try:
        settings = load_settings()
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 1

    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    store = Store(settings.database_path)
    dispatcher = create_dispatcher(settings, store)
    app = create_webhook_app(settings, bot, dispatcher)
    web.run_app(app, host=WEBHOOK_HOST, port=settings.webhook_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
