from aiogram.enums import MessageEntityType

from dance_community_bot.formatting import entities_for_text


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def test_entities_preserve_text_and_use_utf16_offsets_for_emoji_and_field_labels() -> None:
    text = (
        "🎉 Событие\n"
        "  Название: Встреча 💃\n"
        "Стоимость: 500 ₽\n"
        "Описание: <b>важно</b> & пары > 3\n"
        "*не markdown* _тоже нет_"
    )

    entities = entities_for_text(text, heading_lines=(0,))

    assert [(entity.type, entity.offset, entity.length) for entity in entities] == [
        (MessageEntityType.BOLD, 0, _utf16_length("🎉 Событие")),
        (
            MessageEntityType.BOLD,
            _utf16_length("🎉 Событие\n  "),
            _utf16_length("Название:"),
        ),
        (
            MessageEntityType.BOLD,
            _utf16_length("🎉 Событие\n  Название: Встреча 💃\n"),
            _utf16_length("Стоимость:"),
        ),
        (
            MessageEntityType.BOLD,
            _utf16_length("🎉 Событие\n  Название: Встреча 💃\nСтоимость: 500 ₽\n"),
            _utf16_length("Описание:"),
        ),
    ]
    assert text == (
        "🎉 Событие\n"
        "  Название: Встреча 💃\n"
        "Стоимость: 500 ₽\n"
        "Описание: <b>важно</b> & пары > 3\n"
        "*не markdown* _тоже нет_"
    )


def test_only_explicit_heading_lines_and_known_labels_are_formatted() -> None:
    text = "Занятия\n\nПриходите на джем: *не разметка*\nЦена в афише: 500 & бесплатно"

    entities = entities_for_text(text, heading_lines=(0, 100, -1))

    assert [(entity.offset, entity.length) for entity in entities] == [(0, 7)]


def test_entities_cover_labels_used_by_cards_schedule_and_status_messages() -> None:
    labels = (
        "Первая дата",
        "Последняя дата",
        "Исходная дата",
        "Фактическая дата",
        "Новое проведение",
        "Предупреждение",
        "Решение",
        "Афиша",
        "Автопубликация",
        "Незавершённые вложения",
        "Публикации",
        "Уведомления владельцу",
        "Неизвестная доставка публикаций",
        "Время по понедельникам (Europe/Moscow)",
    )
    lines = [f"{label}: значение" for label in labels]
    text = "\n".join(lines)

    entities = entities_for_text(text)

    assert [(entity.offset, entity.length) for entity in entities] == [
        (
            _utf16_length("\n".join(lines[:index]) + ("\n" if index else "")),
            _utf16_length(f"{label}:"),
        )
        for index, label in enumerate(labels)
    ]
