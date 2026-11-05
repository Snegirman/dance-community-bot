"""Создание Telegram-диспетчера и доступные команды."""

from aiogram import Dispatcher
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import CallbackQuery, Message

from dance_community_bot.access import is_owner_private

START_TEXT = (
    "Привет! Я бот танцевального сообщества. Пока доступны команды /start и /help. "
    "Приём, распознавание, хранение и публикация материалов ещё не реализованы."
)
HELP_TEXT = (
    "Пока доступны команды /start и /help. Приём, распознавание, хранение и публикация "
    "материалов ещё не реализованы."
)
UNAVAILABLE_TEXT = "Приём материалов пока не реализован; сообщение не сохранено."


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


async def unavailable_message_handler(message: Message) -> None:
    await message.answer(UNAVAILABLE_TEXT)


def create_dispatcher(owner_id: int) -> Dispatcher:
    dispatcher = Dispatcher()
    access_filter = OwnerPrivateFilter(owner_id)
    dispatcher.message.filter(access_filter)
    dispatcher.callback_query.filter(access_filter)
    dispatcher.message.register(start_handler, CommandStart())
    dispatcher.message.register(help_handler, Command("help"))
    dispatcher.message.register(unavailable_message_handler)
    return dispatcher
