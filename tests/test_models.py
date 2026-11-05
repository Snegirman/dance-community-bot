from datetime import date, time

import pytest
from pydantic import ValidationError

from dance_community_bot.models import ClassData, EventData


def test_event_json_round_trip_preserves_unknown_and_multiline_values() -> None:
    event = EventData(
        title="Popping Jam",
        event_type="  MiXeD 🪩   freestyle  ",
        start_date=date(2026, 11, 14),
        end_date=None,
        start_time=None,
        place=None,
        price="До 12 ноября — 500 ₽\nНа входе — 700 ₽; зрителям бесплатно 🎉",
        description="Свободные выходы\nПосле — общая практика 💃",
    )

    restored = EventData.model_validate_json(event.model_dump_json())

    assert restored == event
    assert restored.event_type == "MiXeD 🪩   freestyle"
    assert restored.end_date is None
    assert restored.start_time is None
    assert restored.place is None
    assert restored.price == "До 12 ноября — 500 ₽\nНа входе — 700 ₽; зрителям бесплатно 🎉"
    assert restored.description == "Свободные выходы\nПосле — общая практика 💃"


def test_class_json_round_trip_preserves_none_values_and_price() -> None:
    class_data = ClassData(
        title="Popping",
        first_date=date(2026, 11, 10),
        start_time=time(19, 0),
        last_date=None,
        place=None,
        price="Разовое — 800 ₽\nАбонемент на 4 занятия — 2 800 ₽",
        description=None,
    )

    restored = ClassData.model_validate_json(class_data.model_dump_json())

    assert restored == class_data
    assert restored.last_date is None
    assert restored.place is None
    assert restored.description is None
    assert restored.price == "Разовое — 800 ₽\nАбонемент на 4 занятия — 2 800 ₽"


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [("2026-11-15", "2026-11-14")],
)
def test_event_json_loading_rejects_end_date_before_start(start_date: str, end_date: str) -> None:
    data = (
        f'{{"title":"Jam","event_type":"Джем","start_date":"{start_date}","end_date":"{end_date}"}}'
    )

    with pytest.raises(ValidationError):
        EventData.model_validate_json(data)


@pytest.mark.parametrize(
    ("first_date", "last_date"),
    [("2026-11-15", "2026-11-14")],
)
def test_class_json_loading_rejects_last_date_before_first(first_date: str, last_date: str) -> None:
    data = (
        f'{{"title":"Popping","first_date":"{first_date}",'
        f'"start_time":"19:00","last_date":"{last_date}"'
        "}"
    )

    with pytest.raises(ValidationError):
        ClassData.model_validate_json(data)


def test_json_loading_rejects_missing_required_fields() -> None:
    with pytest.raises(ValidationError):
        EventData.model_validate_json('{"event_type":"Джем","start_date":"2026-11-14"}')

    with pytest.raises(ValidationError):
        EventData.model_validate_json('{"title":"Jam","start_date":"2026-11-14"}')

    with pytest.raises(ValidationError):
        ClassData.model_validate_json('{"title":"Popping","first_date":"2026-11-10"}')


def test_json_loading_rejects_empty_required_title() -> None:
    with pytest.raises(ValidationError):
        EventData.model_validate_json(
            '{"title":"  ","event_type":"Джем","start_date":"2026-11-14"}'
        )

    with pytest.raises(ValidationError):
        ClassData.model_validate_json(
            '{"title":"  ","first_date":"2026-11-10","start_time":"19:00"}'
        )


@pytest.mark.parametrize("event_type", ["", "   "])
def test_event_rejects_empty_event_type(event_type: str) -> None:
    with pytest.raises(ValidationError) as error:
        EventData(title="Jam", event_type=event_type, start_date=date(2026, 11, 14))

    assert "Тип события обязателен." in str(error.value)


def test_equal_date_ranges_and_maximum_date_are_valid() -> None:
    event = EventData.model_validate_json(
        '{"title":"Последний день","event_type":"Другое",'
        '"start_date":"9999-12-31","end_date":"9999-12-31"}'
    )
    class_data = ClassData.model_validate_json(
        '{"title":"Последнее занятие","first_date":"9999-12-31",'
        '"start_time":"23:59","last_date":"9999-12-31"}'
    )

    assert event.end_date == event.start_date == date.max
    assert class_data.last_date == class_data.first_date == date.max
