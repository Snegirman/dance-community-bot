"""Создание Telegram-диспетчера и доступные команды."""

from aiogram import Dispatcher
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import CallbackQuery, Message

from dance_community_bot.access import is_owner_private
from dance_community_bot.cards import show_events
from dance_community_bot.config import Settings
from dance_community_bot.intake import handle_card_action, ingest_message
from dance_community_bot.schedule_handlers import schedule_command_handler
from dance_community_bot.storage import Store

START_TEXT = "Привет! Используйте /help, чтобы посмотреть доступные команды."
HELP_TEXT = (
    "Команды:\n"
    "/event — шаблон разового события\n"
    "/class — шаблон регулярного занятия\n"
    "/events — активные записи, черновики и незавершённые сообщения\n"
    "/schedule [ДД.ММ.ГГГГ] — расписание недели и изменения серий\n"
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


async def events_handler(message: Message, store: Store) -> None:
    await show_events(message, store)


async def schedule_handler(message: Message, store: Store) -> None:
    await schedule_command_handler(message, store)


async def intake_handler(message: Message, store: Store) -> None:
    await ingest_message(message, store)


async def card_action_handler(callback: CallbackQuery, store: Store) -> None:
    await handle_card_action(callback, store)


def create_dispatcher(settings: Settings, store: Store) -> Dispatcher:
    """Создать диспетчер с барьером доступа и постоянным хранилищем."""
    dispatcher = Dispatcher()
    dispatcher["store"] = store
    access_filter = OwnerPrivateFilter(settings.telegram_owner_id)
    dispatcher.message.filter(access_filter)
    dispatcher.edited_message.filter(access_filter)
    dispatcher.callback_query.filter(access_filter)
    dispatcher.startup.register(store.initialize)
    dispatcher.message.register(start_handler, CommandStart())
    dispatcher.message.register(help_handler, Command("help"))
    dispatcher.message.register(event_template_handler, Command("event"))
    dispatcher.message.register(class_template_handler, Command("class"))
    dispatcher.message.register(events_handler, Command("events"))
    dispatcher.message.register(schedule_handler, Command("schedule"))
    dispatcher.message.register(intake_handler)
    dispatcher.edited_message.register(intake_handler)
    dispatcher.callback_query.register(card_action_handler)
    return dispatcher
