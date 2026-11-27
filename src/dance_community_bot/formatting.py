"""Telegram entities для оформления неизменённого пользовательского текста."""

from __future__ import annotations

from aiogram.enums import MessageEntityType
from aiogram.types import MessageEntity

_FIELD_LABELS = (
    "Обязательные поля",
    "Необязательные поля",
    "Время по понедельникам (Europe/Moscow)",
    "Время публикации",
    "Дата окончания",
    "Дата начала",
    "Фактическая дата",
    "Последняя дата",
    "Первая дата",
    "Исходная дата",
    "Неизвестная доставка публикаций",
    "Незавершённые вложения",
    "Уведомления владельцу",
    "Новое проведение",
    "Следующий шаг",
    "Целевая группа",
    "Неделя",
    "Части",
    "Автопубликация",
    "Название",
    "Направление",
    "Описание",
    "Стоимость",
    "Публикация",
    "Публикации",
    "Причина",
    "Состояние",
    "Статус",
    "Предупреждение",
    "Решение",
    "Афиша",
    "Часть",
    "Период",
    "Время",
    "Место",
    "Дата",
    "Цена",
    "Тип",
    "День",
)


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def entities_for_text(text: str, *, heading_lines: tuple[int, ...] = ()) -> list[MessageEntity]:
    """Оформить заданные заголовки и подписи полей в исходном тексте."""
    heading_indexes = set(heading_lines)
    entities: list[MessageEntity] = []
    line_start = 0

    for line_index, line in enumerate(text.split("\n")):
        visible_line = line[:-1] if line.endswith("\r") else line
        start = len(visible_line) - len(visible_line.lstrip())
        end = len(visible_line.rstrip())
        if start < end:
            if line_index in heading_indexes:
                entity_start = start
                entity_end = end
            else:
                field_label = next(
                    (
                        label
                        for label in _FIELD_LABELS
                        if visible_line.startswith(f"{label}:", start)
                    ),
                    None,
                )
                if field_label is None:
                    field_label = ""
                entity_start = start
                entity_end = start + len(field_label) + 1 if field_label else start

            if entity_start < entity_end:
                entities.append(
                    MessageEntity(
                        type=MessageEntityType.BOLD,
                        offset=line_start + _utf16_length(visible_line[:entity_start]),
                        length=_utf16_length(visible_line[entity_start:entity_end]),
                    )
                )
        line_start += _utf16_length(line) + 1

    return entities
