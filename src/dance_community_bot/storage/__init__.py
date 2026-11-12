"""Публичный интерфейс хранилища SQLite."""

from .cards import CardsStorage
from .publications import PublicationsStorage
from .records import RecordsStorage
from .schedule import ScheduleStorage
from .sources import SourcesStorage


class Store(SourcesStorage, RecordsStorage, ScheduleStorage, PublicationsStorage, CardsStorage):
    """Объединённый интерфейс SQLite-хранилища."""
