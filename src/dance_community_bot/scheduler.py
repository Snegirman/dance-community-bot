"""Фоновая проверка понедельничного выпуска и сохранённых уведомлений."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Literal
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from aiogram import Bot

from .config import Settings
from .models import PublicationChangeNotification, SchedulerNotice, WeeklyPublicationSetting
from .publishing import publish_week
from .storage import Store
from .weekly import _split_text, build_weekly_post

_MOSCOW = ZoneInfo("Europe/Moscow")
_CHECK_INTERVAL = timedelta(seconds=10)
_PUBLICATION_WINDOW = timedelta(minutes=1)
_previous_checks: WeakKeyDictionary[Store, datetime] = WeakKeyDictionary()
_check_locks: WeakKeyDictionary[Store, asyncio.Lock] = WeakKeyDictionary()
_logger = logging.getLogger(__name__)
_NotificationOutcome = Literal["sent", "retry"]


def _lock_for(store: Store) -> asyncio.Lock:
    lock = _check_locks.get(store)
    if lock is None:
        lock = asyncio.Lock()
        _check_locks[store] = lock
    return lock


def publication_schedule_lock(store: Store) -> asyncio.Lock:
    """Получить тот же процессный lock для смены срока и фонового tick."""
    return _lock_for(store)


async def _finish_scheduler_notice_safely(
    store: Store, notice: SchedulerNotice, outcome: _NotificationOutcome
) -> None:
    completion = asyncio.create_task(
        store.finish_scheduler_notice(
            notice.week_start,
            notice.kind,
            outcome,
            setting_revision=notice.setting_revision,
        )
    )
    try:
        await asyncio.shield(completion)
    except asyncio.CancelledError:
        await completion
        raise


async def _finish_publication_notification_safely(
    store: Store,
    notification: PublicationChangeNotification,
    outcome: _NotificationOutcome,
) -> None:
    completion = asyncio.create_task(
        store.finish_publication_notification(
            notification.publication_id,
            notification.revision,
            outcome,
        )
    )
    try:
        await asyncio.shield(completion)
    except asyncio.CancelledError:
        await completion
        raise


async def _deliver_scheduler_notices(settings: Settings, store: Store, bot: Bot) -> None:
    while (notice := await store.claim_next_scheduler_notice()) is not None:
        try:
            await bot.send_message(
                settings.telegram_owner_id,
                notice.text,
                disable_web_page_preview=True,
            )
        except asyncio.CancelledError:
            await _finish_scheduler_notice_safely(store, notice, "retry")
            raise
        except Exception:
            await _finish_scheduler_notice_safely(store, notice, "retry")
            return
        await _finish_scheduler_notice_safely(store, notice, "sent")


async def _deliver_publication_notifications(settings: Settings, store: Store, bot: Bot) -> None:
    while (notification := await store.claim_next_publication_notification()) is not None:
        try:
            for chunk in _split_text(notification.text, 4096):
                await bot.send_message(
                    settings.telegram_owner_id,
                    chunk,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
        except asyncio.CancelledError:
            await _finish_publication_notification_safely(store, notification, "retry")
            raise
        except Exception:
            await _finish_publication_notification_safely(store, notification, "retry")
            return
        await _finish_publication_notification_safely(store, notification, "sent")


async def _has_publication_attempt(store: Store, group_id: int, week_start: date) -> bool:
    publication = await store.get_publication_for_week(group_id, week_start)
    return publication is not None and publication.status != "prepared"


async def _check_schedule(
    current: datetime,
    previous: datetime | None,
    settings: Settings,
    store: Store,
    bot: Bot,
    setting: WeeklyPublicationSetting,
) -> None:
    if setting.weekly_publication_time is None or settings.telegram_group_id is None:
        return
    current_week = current.date() - timedelta(days=current.weekday())
    first_week = current_week
    if previous is not None and previous < current:
        first_week = previous.date() - timedelta(days=previous.weekday())
    if setting.changed_at is not None:
        changed_week = setting.changed_at.astimezone(_MOSCOW).date()
        changed_week -= timedelta(days=changed_week.weekday())
        first_week = max(first_week, changed_week)
    for ordinal in range(first_week.toordinal(), current_week.toordinal() + 1, 7):
        week_start = date.fromordinal(ordinal)
        target = datetime.combine(week_start, setting.weekly_publication_time, _MOSCOW)
        if setting.changed_at is not None and setting.changed_at.astimezone(_MOSCOW) >= target:
            continue
        if current < target:
            continue
        if await _has_publication_attempt(store, settings.telegram_group_id, week_start):
            continue
        if await store.has_scheduler_notice(week_start, setting.revision):
            continue
        # Предыдущая проверка должна быть непосредственно перед целевой минутой,
        # а не в другом дне или неделе до длительного простоя цикла.
        crossed_target = (
            previous is not None
            and timedelta(0) < target - previous <= _PUBLICATION_WINDOW
            and current - target < _PUBLICATION_WINDOW
        )
        if crossed_target:
            post = await build_weekly_post(week_start, store)
            if not post.parts:
                await store.save_scheduler_notice(
                    week_start,
                    "empty",
                    f"На неделю с {week_start:%d.%m.%Y} записей нет; публикация не отправлена.",
                    setting_revision=setting.revision,
                )
            else:
                await publish_week(post, settings.telegram_group_id, store, bot, automatic=True)
        else:
            await store.save_scheduler_notice(
                week_start,
                "missed",
                f"Пропущено время автопубликации недели с {week_start:%d.%m.%Y}. "
                "Автоматическая отправка не выполнялась; проверьте выпуск командой /publish.",
                setting_revision=setting.revision,
            )


async def check_publication(now: datetime, settings: Settings, store: Store, bot: Bot) -> None:
    """Проверить пересечённые сроки и отправить ожидающие уведомления."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Время проверки должно содержать часовой пояс.")
    async with _lock_for(store):
        current = now.astimezone(_MOSCOW)
        previous = _previous_checks.get(store)
        _previous_checks[store] = current
        setting = await store.get_weekly_publication_setting()
        await _check_schedule(current, previous, settings, store, bot, setting)
        await _deliver_scheduler_notices(settings, store, bot)
        await _deliver_publication_notifications(settings, store, bot)


async def run_scheduler(settings: Settings, store: Store, bot: Bot) -> None:
    """Проверять планировщик каждые десять секунд до отмены задачи."""
    _previous_checks.pop(store, None)
    while True:
        try:
            await check_publication(datetime.now(_MOSCOW), settings, store, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.error("Ошибка фоновой проверки публикаций.")
        await asyncio.sleep(_CHECK_INTERVAL.total_seconds())
