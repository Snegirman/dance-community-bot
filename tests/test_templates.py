from datetime import UTC, date, datetime

import pytest

from dance_community_bot.models import ClassData
from dance_community_bot.templates import (
    TemplateError,
    parse_class_template,
    parse_event_template,
    past_date_warning,
)


def test_minimal_event_preserves_unknown_values() -> None:
    event = parse_event_template("Название: Jam\nДата: 14.11.2026\nТип: Джем")

    assert event.start_time is None
    assert event.price is None
    assert event.end_date is None


def test_event_fields_are_case_insensitive_order_independent_and_trimmed() -> None:
    event = parse_event_template(
        "стоимость :  бесплатно  \n"
        "ДАТА : 14.11.2026 \n"
        "тИп: дЖеМ\n"
        "название:  Popping Jam  \n"
        "вРеМя: 18:30\n"
        "место:  Студия, адрес  "
    )

    assert event.title == "Popping Jam"
    assert event.event_type == "дЖеМ"
    assert event.start_date == date(2026, 11, 14)
    assert event.start_time is not None
    assert (event.start_time.hour, event.start_time.minute) == (18, 30)
    assert event.place == "Студия, адрес"
    assert event.price == "бесплатно"


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("Вечеринка", "Вечеринка"),
        ("MiXeD 🪩   freestyle", "MiXeD 🪩   freestyle"),
        ("  MiXeD 🪩   freestyle  ", "MiXeD 🪩   freestyle"),
    ],
)
def test_event_parser_preserves_arbitrary_type_text(event_type: str, expected: str) -> None:
    event = parse_event_template(f"Название: Jam\nДата: 14.11.2026\nТип: {event_type}")

    assert event.event_type == expected


def test_event_parser_keeps_empty_optional_values_as_none() -> None:
    event = parse_event_template(
        "Название: Jam\nДата: 14.11.2026\nТип: Джем\n"
        "Время:\nДата окончания: \nМесто:  \nСтоимость: \nОписание:"
    )

    assert event.start_time is None
    assert event.end_date is None
    assert event.place is None
    assert event.price is None
    assert event.description is None


def test_event_parser_preserves_indented_continuations_and_emoji() -> None:
    event = parse_event_template(
        "Название: Jam\nДата: 14.11.2026\nТип: Джем\n"
        "Стоимость: До 12 ноября — 500 ₽\n"
        " На входе — 700 ₽; зрителям бесплатно 🎉\n"
        "Описание: Свободные выходы\n"
        " После — общая практика 💃"
    )

    assert event.price == "До 12 ноября — 500 ₽\nНа входе — 700 ₽; зрителям бесплатно 🎉"
    assert event.description == "Свободные выходы\nПосле — общая практика 💃"


def test_cost_continuation_preserves_colon_and_field_like_text() -> None:
    event = parse_event_template(
        "Название: Jam\nДата: 14.11.2026\nТип: Джем\nСтоимость: 500 ₽\n Место: у сцены"
    )

    assert event.price == "500 ₽\nМесто: у сцены"
    assert event.place is None


def test_indented_line_outside_multiline_field_is_rejected() -> None:
    with pytest.raises(TemplateError) as error:
        parse_event_template("Название: Jam\nДата: 14.11.2026\nТип: Джем\n Место: Студия")

    assert any("Место" in message or "отступ" in message for message in error.value.messages)


@pytest.mark.parametrize(
    ("field", "value", "expected_label"),
    [
        ("Дата", "31.02.2026", "Дата"),
        ("Дата", "29.02.2026", "Дата"),
        ("Дата", "14.11", "Дата"),
        ("Дата", "4.11.2026", "Дата"),
        ("Дата", "14.1.2026", "Дата"),
        ("Дата", "в субботу", "Дата"),
        ("Время", "24:00", "Время"),
        ("Время", "9:00", "Время"),
        ("Время", "09:0", "Время"),
        ("Дата окончания", "13.11.2026", "Дата окончания"),
        ("Название", "", "Название"),
        ("Тип", "", "Тип"),
        ("Тип", "  ", "Тип"),
    ],
)
def test_event_parser_reports_invalid_field_values(
    field: str, value: str, expected_label: str
) -> None:
    values = {"Название": "Jam", "Дата": "14.11.2026", "Тип": "Джем"}
    values[field] = value
    text = "\n".join(f"{name}: {item}" for name, item in values.items())

    with pytest.raises(TemplateError) as error:
        parse_event_template(text)

    assert any(expected_label in message for message in error.value.messages)


@pytest.mark.parametrize("invalid_date", ["31.02.2026", "00.01.2026", "01.01.0000"])
def test_impossible_date_error_includes_required_format(invalid_date: str) -> None:
    with pytest.raises(TemplateError) as error:
        parse_event_template(f"Название: Jam\nДата: {invalid_date}\nТип: Джем")

    assert any("Дата" in message and "ДД.ММ.ГГГГ" in message for message in error.value.messages)


def test_long_price_and_description_preserve_all_conditions_and_html_characters() -> None:
    price = "Тариф <500 ₽> & условия: зрителям бесплатно 🎉; " * 150 + "Конец условий"
    description = "Практика <Popping> & Waving 💃\nМесто: у сцены"

    event = parse_event_template(
        f"Название: Jam\nДата: 14.11.2026\nТип: Джем\nСтоимость: {price}\n"
        "Описание: Практика <Popping> & Waving 💃\n Место: у сцены"
    )

    assert event.price == price
    assert event.description == description


@pytest.mark.parametrize(
    ("extra_lines", "expected_label"),
    [
        (["Локация: Студия"], "Локация"),
        (["Место: Студия", "место: Другой зал"], "Место"),
        (["Строка без двоеточия"], "Строка"),
    ],
)
def test_event_parser_rejects_unknown_duplicate_and_malformed_lines(
    extra_lines: list[str], expected_label: str
) -> None:
    text = "\n".join(["Название: Jam", "Дата: 14.11.2026", "Тип: Джем", *extra_lines])

    with pytest.raises(TemplateError) as error:
        parse_event_template(text)

    assert any(expected_label in message for message in error.value.messages)


def test_event_parser_rejects_missing_required_fields() -> None:
    with pytest.raises(TemplateError) as error:
        parse_event_template("Название: Jam\nДата: 14.11.2026")

    assert any("Тип" in message for message in error.value.messages)


def test_maximum_date_does_not_overflow_during_warning_check() -> None:
    event = parse_event_template(
        "Название: Jam\nДата: 31.12.9999\nДата окончания: 31.12.9999\nТип: Джем"
    )

    assert event.start_date == date.max
    assert event.end_date == date.max
    assert past_date_warning(event, datetime(2026, 11, 4, tzinfo=UTC)) is None


def test_class_parser_requires_time_and_preserves_optional_none_values() -> None:
    class_data = parse_class_template("Название: Popping\nПервая дата: 10.11.2026\nВремя: 19:00")

    assert isinstance(class_data, ClassData)
    assert class_data.start_time.hour == 19
    assert class_data.start_time.minute == 0
    assert class_data.last_date is None
    assert class_data.place is None
    assert class_data.price is None
    assert class_data.description is None

    with pytest.raises(TemplateError) as error:
        parse_class_template("Название: Popping\nПервая дата: 10.11.2026")

    assert any("Время" in message for message in error.value.messages)


@pytest.mark.parametrize(
    ("field", "value", "expected_label"),
    [
        ("Первая дата", "31.02.2026", "Первая дата"),
        ("Первая дата", "29.02.2026", "Первая дата"),
        ("Первая дата", "14.11", "Первая дата"),
        ("Первая дата", "4.11.2026", "Первая дата"),
        ("Первая дата", "14.1.2026", "Первая дата"),
        ("Первая дата", "в субботу", "Первая дата"),
        ("Время", "24:00", "Время"),
        ("Время", "9:00", "Время"),
        ("Время", "09:0", "Время"),
        ("Последняя дата", "09.11.2026", "Последняя дата"),
        ("Название", "", "Название"),
    ],
)
def test_class_parser_reports_invalid_field_values(
    field: str, value: str, expected_label: str
) -> None:
    values = {
        "Название": "Popping",
        "Первая дата": "10.11.2026",
        "Время": "19:00",
    }
    values[field] = value
    text = "\n".join(f"{name}: {item}" for name, item in values.items())

    with pytest.raises(TemplateError) as error:
        parse_class_template(text)

    assert any(expected_label in message for message in error.value.messages)


def test_class_parser_accepts_leap_day_and_equal_date_bounds() -> None:
    class_data = parse_class_template(
        "Название: Popping\nПервая дата: 29.02.2028\nВремя: 19:00\nПоследняя дата: 29.02.2028"
    )

    assert class_data.first_date == date(2028, 2, 29)
    assert class_data.last_date == class_data.first_date


def test_warning_uses_moscow_date_for_utc_now() -> None:
    event = parse_event_template("Название: Jam\nДата: 13.11.2026\nТип: Джем")
    now = datetime(2026, 11, 13, 21, 30, tzinfo=UTC)

    warning = past_date_warning(event, now)

    assert warning is not None
    assert "Дата" in warning


def test_warning_compares_only_date_when_event_time_is_unknown() -> None:
    event = parse_event_template("Название: Jam\nДата: 04.11.2026\nТип: Джем")
    now = datetime(2026, 11, 4, 20, 0, tzinfo=UTC)

    assert past_date_warning(event, now) is None


def test_warning_reports_past_time_today_but_not_future_date() -> None:
    past_event = parse_event_template("Название: Jam\nДата: 04.11.2026\nТип: Джем\nВремя: 22:00")
    future_event = parse_event_template("Название: Jam\nДата: 05.11.2026\nТип: Джем\nВремя: 00:01")
    now = datetime(2026, 11, 4, 19, 30, tzinfo=UTC)

    warning = past_date_warning(past_event, now)

    assert warning is not None
    assert "Время" in warning
    assert past_date_warning(future_event, now) is None


def test_warning_rejects_naive_now() -> None:
    event = parse_event_template("Название: Jam\nДата: 14.11.2026\nТип: Джем")

    with pytest.raises(ValueError):
        past_date_warning(event, datetime(2026, 11, 4, 12, 0))


def test_warning_checks_class_start_date_and_time() -> None:
    class_data = parse_class_template("Название: Popping\nПервая дата: 04.11.2026\nВремя: 21:00")

    warning = past_date_warning(class_data, datetime(2026, 11, 4, 19, 30, tzinfo=UTC))

    assert warning is not None
    assert "Время" in warning
