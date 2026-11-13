"""Управление временем автопубликации через личный чат владельца."""

from __future__ import annotations

import re
from datetime import time

from aiogram.filters import BaseFilter
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .models import WeeklyPublicationSetting
from .scheduler import publication_schedule_lock
from .storage import Store

_TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def _settings_text(setting: WeeklyPublicationSetting) -> str:
    if setting.weekly_publication_time is None:
        value = "автопубликация отключена"
    else:
        value = f"время по понедельникам — {setting.weekly_publication_time:%H:%M} (Москва)"
    return f"Настройки автопубликации: {value}."


def _settings_keyboard(setting: WeeklyPublicationSetting) -> InlineKeyboardMarkup:
    revision = setting.revision
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Изменить время",
                    callback_data=f"settings:time:{revision}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отключить автопубликацию",
                    callback_data=f"settings:disable:{revision}",
                )
            ],
        ]
    )


async def settings_command_handler(message: Message, store: Store) -> None:
    setting = await store.get_weekly_publication_setting()
    await message.answer(_settings_text(setting), reply_markup=_settings_keyboard(setting))


async def settings_callback_handler(callback: CallbackQuery, store: Store) -> None:
    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "settings" or not parts[2].isdecimal():
        await callback.answer("Кнопка настройки устарела.", show_alert=True)
        return
    action, revision_text = parts[1], parts[2]
    revision = int(revision_text)
    target = callback.message
    if not isinstance(target, Message):
        await callback.answer("Откройте /settings в личном чате.", show_alert=True)
        return

    setting = await store.get_weekly_publication_setting()
    if setting.revision != revision:
        await callback.answer(
            "Настройка уже изменилась. Откройте /settings ещё раз.", show_alert=True
        )
        return

    if action == "disable":
        async with publication_schedule_lock(store):
            updated = await store.set_weekly_publication_time(None, expected_revision=revision)
        if updated is None:
            await callback.answer(
                "Настройка уже изменилась. Откройте /settings ещё раз.", show_alert=True
            )
            return
        await callback.answer("Автопубликация отключена.")
        await target.answer(_settings_text(updated))
        return

    if action != "time":
        await callback.answer("Кнопка настройки устарела.", show_alert=True)
        return

    prompt = await target.answer(
        "Ответьте на это сообщение временем по Москве в формате ЧЧ:ММ.",
        reply_markup=ForceReply(selective=True),
    )
    bound = await store.bind_weekly_publication_prompt(target.chat.id, prompt.message_id, revision)
    if not bound:
        await callback.answer(
            "Настройка уже изменилась. Откройте /settings ещё раз.", show_alert=True
        )
        return
    await callback.answer()


class WeeklyPublicationReplyFilter(BaseFilter):
    async def __call__(self, message: Message, store: Store) -> bool:
        reply = message.reply_to_message
        if reply is None:
            return False
        prompt = await store.get_weekly_publication_prompt(message.chat.id, reply.message_id)
        return prompt is not None


async def _handle_time_reply(message: Message, store: Store, *, edited: bool) -> None:
    reply = message.reply_to_message
    if reply is None:
        return
    prompt = await store.get_weekly_publication_prompt(message.chat.id, reply.message_id)
    if prompt is None:
        return
    setting = await store.get_weekly_publication_setting()
    if prompt.status != "pending" or prompt.revision != setting.revision:
        await message.answer("Этот запрос времени уже обработан или устарел. Откройте /settings.")
        return
    if edited:
        await message.answer(
            "Изменённое сообщение не применено. Отправьте новое время ответом на запрос."
        )
        return

    value_text = (message.text or "").strip()
    if _TIME_PATTERN.fullmatch(value_text) is None:
        await message.answer("Введите время строго в формате ЧЧ:ММ, например 19:30.")
        return
    hour_text, minute_text = value_text.split(":")
    value = time(int(hour_text), int(minute_text))
    async with publication_schedule_lock(store):
        updated = await store.complete_weekly_publication_prompt(
            message.chat.id, reply.message_id, value
        )
    if updated is None:
        await message.answer("Этот запрос времени уже обработан или устарел. Откройте /settings.")
        return
    await message.answer(_settings_text(updated))


async def settings_time_reply_handler(message: Message, store: Store) -> None:
    await _handle_time_reply(message, store, edited=False)


async def edited_settings_time_reply_handler(message: Message, store: Store) -> None:
    await _handle_time_reply(message, store, edited=True)
