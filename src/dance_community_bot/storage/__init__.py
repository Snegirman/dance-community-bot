"""Публичный интерфейс хранилища SQLite."""

from datetime import time

from .automation import AutomationStorage
from .cards import CardsStorage
from .publications import PublicationsStorage
from .records import RecordsStorage
from .schedule import ScheduleStorage
from .settings import SettingsStorage
from .sources import SourcesStorage


class Store(
    SourcesStorage,
    RecordsStorage,
    ScheduleStorage,
    PublicationsStorage,
    SettingsStorage,
    AutomationStorage,
    CardsStorage,
):
    """Объединённый интерфейс SQLite-хранилища."""

    async def initialize(self, initial_weekly_publication_time: time | None = None) -> None:
        await super().initialize()
        await self.initialize_weekly_publication_setting(initial_weekly_publication_time)
