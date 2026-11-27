"""Создание Telegram-диспетчера и доступные команды."""

import asyncio
from contextlib import suppress
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.enums import MessageEntityType
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    MessageEntity,
    ReplyKeyboardMarkup,
)

from dance_community_bot.access import is_owner_private
from dance_community_bot.cards import show_events
from dance_community_bot.config import Settings
from dance_community_bot.formatting import entities_for_text
from dance_community_bot.intake import handle_card_action, ingest_message
from dance_community_bot.publication_handlers import (
    publication_callback_handler,
    publish_command_handler,
    republish_command_handler,
)
from dance_community_bot.schedule_handlers import schedule_command_handler, show_schedule_week
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

_MOSCOW = ZoneInfo("Europe/Moscow")
_SCHEDULE_BUTTON = "Расписание"
_SCHEDULE_WEEK_CALLBACK_PREFIX = "schedule-week:"
_EARLIEST_SELECTABLE_WEEK = date.min + timedelta(days=7)
_LATEST_SELECTABLE_WEEK = date.max - timedelta(days=date.max.weekday() + 6)

_SCHEDULE_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=_SCHEDULE_BUTTON)]],
    is_persistent=True,
    resize_keyboard=True,
)

START_TEXT = (
    "Привет! Я помогу вести расписание и события танцевального сообщества.\n"
    "Нажмите «Расписание» в нижнем меню, чтобы выбрать неделю. Используйте /help "
    "для списка команд."
)
HELP_TEXT = (
    "Команды\n\n"
    "Добавление событий\n"
    "/event — шаблон разового события\n"
    "/class — шаблон регулярного занятия\n\n"
    "Управление\n"
    "/events — активные записи, черновики и незавершённые сообщения\n"
    "/schedule [ДД.ММ.ГГГГ] — расписание недели и управление занятиями\n"
    "/preview [ДД.ММ.ГГГГ] — предпросмотр публикации за неделю\n"
    "/publish [ДД.ММ.ГГГГ] — предпросмотр и ручная публикация недели\n"
    "/republish — повторить актуальный выпуск текущей недели\n"
    "/settings — настроить время автопубликации\n"
    "/status — состояние публикаций и автопубликации\n"
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
    await message.answer(
        START_TEXT,
        entities=entities_for_text(START_TEXT, heading_lines=(0,)),
        parse_mode=None,
        reply_markup=_SCHEDULE_REPLY_KEYBOARD,
    )


async def help_handler(message: Message) -> None:
    await message.answer(
        HELP_TEXT,
        entities=entities_for_text(HELP_TEXT, heading_lines=(0, 2, 6)),
        parse_mode=None,
        reply_markup=_SCHEDULE_REPLY_KEYBOARD,
    )


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


async def _send_copy_template(message: Message, introduction: str, template: str) -> None:
    text = f"{introduction}\n\n{template}"
    template_offset = _utf16_length(f"{introduction}\n\n")
    entities = entities_for_text(introduction, heading_lines=(0,))
    entities.append(
        MessageEntity(
            type=MessageEntityType.PRE,
            offset=template_offset,
            length=_utf16_length(template),
        )
    )
    await message.answer(text, entities=entities, parse_mode=None)


async def event_template_handler(message: Message) -> None:
    await _send_copy_template(
        message,
        "Разовое событие\n"
        "Обязательные поля: название, дата (ДД.ММ.ГГГГ) и тип.\n"
        "Необязательные поля: дата окончания, время, место, стоимость, описание и афиша.\n"
        "Скопируйте шаблон из блока ниже и заполните известными данными.",
        EVENT_TEMPLATE,
    )


async def class_template_handler(message: Message) -> None:
    await _send_copy_template(
        message,
        "Регулярное занятие\n"
        "Обязательные поля: название, первая дата (ДД.ММ.ГГГГ) и время (ЧЧ:ММ).\n"
        "Необязательные поля: последняя дата, место, стоимость, описание и афиша.\n"
        "Скопируйте шаблон из блока ниже и заполните известными данными.",
        CLASS_TEMPLATE,
    )


def _week_monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _week_range_label(week_start: date) -> str:
    week_end = date.fromordinal(week_start.toordinal() + 6)
    return f"{week_start:%d.%m.%Y} — {week_end:%d.%m.%Y}"


def _week_callback(week_start: date) -> str:
    return f"{_SCHEDULE_WEEK_CALLBACK_PREFIX}{week_start.isoformat()}"


def _schedule_picker_keyboard(week_starts: tuple[tuple[str, date], ...]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{label} · {_week_range_label(week_start)}",
                    callback_data=_week_callback(week_start),
                )
            ]
            for label, week_start in week_starts
        ]
    )


def _schedule_navigation_keyboard(week_start: date) -> InlineKeyboardMarkup:
    buttons: list[InlineKeyboardButton] = []
    if week_start - timedelta(days=7) >= _EARLIEST_SELECTABLE_WEEK:
        previous = week_start - timedelta(days=7)
        buttons.append(
            InlineKeyboardButton(
                text=f"← {_week_range_label(previous)}",
                callback_data=_week_callback(previous),
            )
        )
    if week_start + timedelta(days=7) <= _LATEST_SELECTABLE_WEEK:
        following = week_start + timedelta(days=7)
        buttons.append(
            InlineKeyboardButton(
                text=f"{_week_range_label(following)} →",
                callback_data=_week_callback(following),
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def _parse_schedule_week_callback(data: str) -> date | None:
    value = data.removeprefix(_SCHEDULE_WEEK_CALLBACK_PREFIX)
    try:
        week_start = date.fromisoformat(value)
    except ValueError:
        return None
    if (
        week_start.isoformat() != value
        or week_start.weekday() != 0
        or not _EARLIEST_SELECTABLE_WEEK <= week_start <= _LATEST_SELECTABLE_WEEK
    ):
        return None
    return week_start


async def schedule_menu_handler(message: Message) -> None:
    current_week = _week_monday(datetime.now(_MOSCOW).date())
    following_week = current_week + timedelta(days=7)
    choices = [("Эта неделя", current_week)]
    if following_week <= _LATEST_SELECTABLE_WEEK:
        choices.append(("Следующая неделя", following_week))
    text = "Выберите неделю расписания:"
    await message.answer(
        text,
        entities=entities_for_text(text, heading_lines=(0,)),
        parse_mode=None,
        reply_markup=_schedule_picker_keyboard(tuple(choices)),
    )


async def schedule_week_callback_handler(callback: CallbackQuery, store: Store) -> None:
    data = callback.data or ""
    week_start = _parse_schedule_week_callback(data)
    target = callback.message
    if week_start is None or not isinstance(target, Message):
        await callback.answer(
            "Не удалось выбрать эту неделю. Нажмите «Расписание» и выберите диапазон из списка.",
            show_alert=True,
        )
        return

    await callback.answer()
    await show_schedule_week(target, store, week_start)
    text = "Переход к соседней неделе:"
    await target.answer(
        text,
        entities=entities_for_text(text),
        parse_mode=None,
        reply_markup=_schedule_navigation_keyboard(week_start),
    )


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
    text = (
        "📊 Состояние бота\n"
        "Автопубликация: "
        f"{automation}.\n"
        f"Время по понедельникам (Europe/Moscow): {publication_time}.\n"
        f"Целевая группа: {group}.\n"
        f"Незавершённые вложения: {status.incomplete_attachments}.\n"
        f"Публикации: {publication_summary}.\n"
        f"Неизвестная доставка публикаций: {unknown_publications}.\n"
        f"Уведомления владельцу: {notifications}."
    )
    await message.answer(
        text,
        entities=entities_for_text(text, heading_lines=(0,)),
        parse_mode=None,
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
    dispatcher.message.register(schedule_menu_handler, F.text == _SCHEDULE_BUTTON)
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
    dispatcher.callback_query.register(
        schedule_week_callback_handler,
        F.data.startswith(_SCHEDULE_WEEK_CALLBACK_PREFIX),
    )
    dispatcher.callback_query.register(card_action_handler)
    return dispatcher
