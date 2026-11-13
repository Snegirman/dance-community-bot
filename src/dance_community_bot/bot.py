"""Создание Telegram-диспетчера и доступные команды."""

import asyncio
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import CallbackQuery, Message

from dance_community_bot.access import is_owner_private
from dance_community_bot.cards import show_events
from dance_community_bot.config import Settings
from dance_community_bot.intake import handle_card_action, ingest_message
from dance_community_bot.publication_handlers import (
    publication_callback_handler,
    publish_command_handler,
    republish_command_handler,
)
from dance_community_bot.schedule_handlers import schedule_command_handler
from dance_community_bot.scheduler import run_scheduler
from dance_community_bot.settings_handlers import (
    WeeklyPublicationReplyFilter,
    edited_settings_time_reply_handler,
    settings_callback_handler,
    settings_command_handler,
    settings_time_reply_handler,
)
from dance_community_bot.storage import Store
from dance_community_bot.weekly import preview_command_handler

START_TEXT = "Привет! Используйте /help, чтобы посмотреть доступные команды."
HELP_TEXT = (
    "Команды:\n"
    "/event — шаблон разового события\n"
    "/class — шаблон регулярного занятия\n"
    "/events — активные записи, черновики и незавершённые сообщения\n"
    "/schedule [ДД.ММ.ГГГГ] — расписание недели и изменения серий\n"
    "/preview [ДД.ММ.ГГГГ] — предпросмотр публикации за неделю\n"
    "/publish [ДД.ММ.ГГГГ] — предпросмотр и ручная публикация недели\n"
    "/republish — повторить актуальный выпуск текущей недели\n"
    "/settings — время автопубликации\n"
    "/status — настройки и состояние публикаций\n"
    "/help — эта справка"
)
EVENT_TEMPLATE = """Название: Popping Jam
Дата: 14.11.2026
Тип: Джем
Время: 18:30
Место: Студия, адрес
Стоимость: До 12 ноября — 500 ₽; на входе — 700 ₽; зрителям бесплатно
Описание: Свободные выходы, после — общая практика."""
CLASS_TEMPLATE = """Название: Popping
Первая дата: 10.11.2026
Время: 19:00
Место: Студия, адрес
Стоимость: 600 ₽
Описание: Открытая группа"""


class OwnerPrivateFilter(BaseFilter):
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user is not None else None
            return is_owner_private(user_id, event.chat.type, self.owner_id)

        message = event.message
        if not isinstance(message, Message):
            return False
        return is_owner_private(event.from_user.id, message.chat.type, self.owner_id)


async def start_handler(message: Message) -> None:
    await message.answer(START_TEXT)


async def help_handler(message: Message) -> None:
    await message.answer(HELP_TEXT)


async def event_template_handler(message: Message) -> None:
    await message.answer(EVENT_TEMPLATE)


async def class_template_handler(message: Message) -> None:
    await message.answer(CLASS_TEMPLATE)


async def status_handler(message: Message, settings: Settings, store: Store) -> None:
    status = await store.get_automation_status()
    publication_setting = await store.get_weekly_publication_setting()
    publication_time_value = publication_setting.weekly_publication_time
    if publication_time_value is None or settings.telegram_group_id is None:
        automation = "выключена: нужны время и целевая группа"
    else:
        automation = "включена"
    publication_time = (
        publication_time_value.strftime("%H:%M")
        if publication_time_value is not None
        else "не задано"
    )
    group = (
        str(settings.telegram_group_id) if settings.telegram_group_id is not None else "не задана"
    )

    publication_labels = {
        "prepared": "подготовлены",
        "sending": "отправляются",
        "partial": "отправлены частично",
        "unknown": "с неизвестной доставкой",
        "delivered": "доставлены",
    }
    publication_summary = (
        ", ".join(
            f"{label}: {status.publications[key]}"
            for key, label in publication_labels.items()
            if status.publications.get(key, 0) > 0
        )
        or "нет записей"
    )
    unknown_publications = status.publications.get("unknown", 0)
    notifications = (
        f"ожидают отправки: {status.pending_publication_notifications}; "
        f"пропуски/пустые недели ожидают: {status.pending_scheduler_notices}"
    )
    await message.answer(
        "Автопубликация: "
        f"{automation}.\n"
        f"Время по понедельникам (Europe/Moscow): {publication_time}.\n"
        f"Целевая группа: {group}.\n"
        f"Незавершённые вложения: {status.incomplete_attachments}.\n"
        f"Публикации: {publication_summary}.\n"
        f"Неизвестная доставка публикаций: {unknown_publications}.\n"
        f"Уведомления владельцу: {notifications}."
    )


async def events_handler(message: Message, store: Store) -> None:
    await show_events(message, store)


async def schedule_handler(message: Message, store: Store) -> None:
    await schedule_command_handler(message, store)


async def preview_handler(message: Message, store: Store) -> None:
    await preview_command_handler(message, store)


async def intake_handler(message: Message, store: Store) -> None:
    await ingest_message(message, store)


async def card_action_handler(callback: CallbackQuery, store: Store) -> None:
    await handle_card_action(callback, store)


def create_dispatcher(settings: Settings, store: Store) -> Dispatcher:
    """Создать диспетчер с барьером доступа и постоянным хранилищем."""
    dispatcher = Dispatcher()
    dispatcher["store"] = store
    dispatcher["settings"] = settings
    access_filter = OwnerPrivateFilter(settings.telegram_owner_id)
    dispatcher.message.filter(access_filter)
    dispatcher.edited_message.filter(access_filter)
    dispatcher.callback_query.filter(access_filter)

    async def initialize_store() -> None:
        await store.initialize(settings.weekly_publication_time)

    dispatcher.startup.register(initialize_store)
    dispatcher.startup.register(store.recover_publications_on_startup)

    scheduler_task: asyncio.Task[None] | None = None

    async def start_scheduler(bot: Bot) -> None:
        nonlocal scheduler_task
        if scheduler_task is None or scheduler_task.done():
            scheduler_task = asyncio.create_task(
                run_scheduler(settings, store, bot), name="weekly-publication-scheduler"
            )

    async def stop_scheduler() -> None:
        nonlocal scheduler_task
        if scheduler_task is not None:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
            scheduler_task = None

    dispatcher.startup.register(store.recover_automation_notices_on_startup)
    dispatcher.startup.register(start_scheduler)
    dispatcher.shutdown.register(stop_scheduler)
    dispatcher.message.register(
        settings_time_reply_handler,
        WeeklyPublicationReplyFilter(),
    )
    dispatcher.message.register(start_handler, CommandStart())
    dispatcher.message.register(help_handler, Command("help"))
    dispatcher.message.register(event_template_handler, Command("event"))
    dispatcher.message.register(class_template_handler, Command("class"))
    dispatcher.message.register(events_handler, Command("events"))
    dispatcher.message.register(schedule_handler, Command("schedule"))
    dispatcher.message.register(preview_handler, Command("preview"))
    dispatcher.message.register(publish_command_handler, Command("publish"))
    dispatcher.message.register(republish_command_handler, Command("republish"))
    dispatcher.message.register(settings_command_handler, Command("settings"))
    dispatcher.message.register(status_handler, Command("status"))
    dispatcher.message.register(intake_handler)
    dispatcher.edited_message.register(
        edited_settings_time_reply_handler,
        WeeklyPublicationReplyFilter(),
    )
    dispatcher.edited_message.register(intake_handler)
    dispatcher.callback_query.register(
        settings_callback_handler,
        F.data.startswith("settings:"),
    )
    dispatcher.callback_query.register(publication_callback_handler, F.data.startswith("pub:"))
    dispatcher.callback_query.register(card_action_handler)
    return dispatcher
