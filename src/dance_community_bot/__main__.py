"""Запуск Telegram-бота через входящие webhook-запросы."""

import sys
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web

from dance_community_bot.bot import create_dispatcher
from dance_community_bot.config import Settings, SettingsError, load_settings

WEBHOOK_HOST = "127.0.0.1"


def create_webhook_app(settings: Settings, bot: Bot, dispatcher: Dispatcher) -> web.Application:
    """Создать aiohttp-приложение для одного Telegram webhook."""
    app = web.Application()
    request_handler = SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        handle_in_background=False,
        secret_token=settings.telegram_webhook_secret.get_secret_value(),
    )
    path = urlsplit(settings.telegram_webhook_url).path
    app.router.add_post(path, request_handler.handle)

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
    dispatcher = create_dispatcher(settings.telegram_owner_id)
    app = create_webhook_app(settings, bot, dispatcher)
    web.run_app(app, host=WEBHOOK_HOST, port=settings.webhook_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
