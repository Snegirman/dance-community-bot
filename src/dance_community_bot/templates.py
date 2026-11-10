import re
from collections.abc import Mapping
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from dance_community_bot.models import ClassData, EventData


class TemplateError(ValueError):
    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        super().__init__("\n".join(self.messages))


_MOSCOW = ZoneInfo("Europe/Moscow")
_DATE_PATTERN = re.compile(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}\Z")
_TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_MULTILINE_FIELDS = {"стоимость", "описание"}
_EVENT_FIELDS = {
    "название": "Название",
    "дата": "Дата",
    "дата окончания": "Дата окончания",
    "тип": "Тип",
    "время": "Время",
    "место": "Место",
    "стоимость": "Стоимость",
    "описание": "Описание",
}
_CLASS_FIELDS = {
    "название": "Название",
    "первая дата": "Первая дата",
    "время": "Время",
    "последняя дата": "Последняя дата",
    "место": "Место",
    "стоимость": "Стоимость",
    "описание": "Описание",
}


def _parse_fields(text: str, known_fields: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    errors: list[str] = []
    current_field: str | None = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if raw_line.startswith((" ", "\t")):
            if current_field in _MULTILINE_FIELDS and current_field in values:
                values[current_field] += f"\n{raw_line[1:]}"
            else:
                contents = raw_line.strip()
                if contents:
                    errors.append(
                        f"Строка {line_number} с отступом вне продолжения поля: «{contents}»."
                    )
                else:
                    errors.append(f"Пустая строка {line_number} не является полем шаблона.")
            continue

        if not raw_line.strip():
            errors.append(f"Пустая строка {line_number} не является полем шаблона.")
            current_field = None
            continue

        label, separator, raw_value = raw_line.partition(":")
        if not separator:
            errors.append(f"Строка {line_number}: ожидалось поле в формате «Название: значение».")
            current_field = None
            continue

        key = label.strip().casefold()
        if key not in known_fields:
            errors.append(f"Неизвестное поле «{label.strip()}» в строке {line_number}.")
            current_field = None
            continue

        current_field = key
        if key in values:
            errors.append(f"Поле «{known_fields[key]}» указано несколько раз.")
            continue

        values[key] = raw_value.strip()

    return values, errors


def _add_missing_errors(
    values: Mapping[str, str], required_fields: Mapping[str, str], errors: list[str]
) -> None:
    for key, label in required_fields.items():
        if not values.get(key):
            errors.append(f"Обязательное поле «{label}» должно содержать значение.")


def parse_strict_date(value: str, *, format_error: str, invalid_error: str) -> date:
    if _DATE_PATTERN.fullmatch(value) is None:
        raise ValueError(format_error)
    try:
        return date(int(value[6:10]), int(value[3:5]), int(value[0:2]))
    except ValueError as error:
        raise ValueError(invalid_error) from error


def _parse_date(value: str, label: str, errors: list[str], *, required: bool) -> date | None:
    if not value:
        if required:
            errors.append(f"Поле «{label}»: укажите дату в формате ДД.ММ.ГГГГ.")
        return None

    try:
        return parse_strict_date(
            value,
            format_error=f"Поле «{label}»: укажите дату в формате ДД.ММ.ГГГГ.",
            invalid_error=f"Поле «{label}»: укажите существующую дату в формате ДД.ММ.ГГГГ.",
        )
    except ValueError as error:
        errors.append(str(error))
        return None


def _parse_time(value: str, label: str, errors: list[str], *, required: bool) -> time | None:
    if not value:
        if required:
            errors.append(f"Поле «{label}»: укажите время в формате ЧЧ:ММ.")
        return None

    if _TIME_PATTERN.fullmatch(value) is None:
        errors.append(f"Поле «{label}»: укажите время в формате ЧЧ:ММ.")
        return None

    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _optional_value(values: Mapping[str, str], key: str) -> str | None:
    return values.get(key) or None


def parse_event_template(text: str) -> EventData:
    values, errors = _parse_fields(text, _EVENT_FIELDS)
    _add_missing_errors(
        values,
        {"название": "Название", "дата": "Дата", "тип": "Тип"},
        errors,
    )
    if errors:
        raise TemplateError(errors)

    start_date_value = _parse_date(values["дата"], "Дата", errors, required=True)
    end_date_value = _parse_date(
        values.get("дата окончания", ""), "Дата окончания", errors, required=False
    )
    start_time_value = _parse_time(values.get("время", ""), "Время", errors, required=False)
    event_type = values["тип"]

    if start_date_value is not None and end_date_value is not None:
        if end_date_value < start_date_value:
            errors.append("Поле «Дата окончания» не может быть раньше поля «Дата».")

    if errors:
        raise TemplateError(errors)

    if start_date_value is None:
        raise TemplateError(["Не удалось проверить обязательные поля события."])

    return EventData(
        title=values["название"],
        event_type=event_type,
        start_date=start_date_value,
        end_date=end_date_value,
        start_time=start_time_value,
        place=_optional_value(values, "место"),
        price=_optional_value(values, "стоимость"),
        description=_optional_value(values, "описание"),
    )


def parse_class_template(text: str) -> ClassData:
    values, errors = _parse_fields(text, _CLASS_FIELDS)
    _add_missing_errors(
        values,
        {"название": "Название", "первая дата": "Первая дата", "время": "Время"},
        errors,
    )
    if errors:
        raise TemplateError(errors)

    first_date_value = _parse_date(values["первая дата"], "Первая дата", errors, required=True)
    last_date_value = _parse_date(
        values.get("последняя дата", ""), "Последняя дата", errors, required=False
    )
    start_time_value = _parse_time(values["время"], "Время", errors, required=True)

    if first_date_value is not None and last_date_value is not None:
        if last_date_value < first_date_value:
            errors.append("Поле «Последняя дата» не может быть раньше поля «Первая дата».")

    if errors:
        raise TemplateError(errors)

    if first_date_value is None or start_time_value is None:
        raise TemplateError(["Не удалось проверить обязательные поля занятия."])

    return ClassData(
        title=values["название"],
        first_date=first_date_value,
        start_time=start_time_value,
        last_date=last_date_value,
        place=_optional_value(values, "место"),
        price=_optional_value(values, "стоимость"),
        description=_optional_value(values, "описание"),
    )


def past_date_warning(data: EventData | ClassData, now: datetime) -> str | None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Текущее время должно содержать часовой пояс.")

    moscow_now = now.astimezone(_MOSCOW)
    if isinstance(data, EventData):
        start_date = data.start_date
        start_time = data.start_time
        date_label = "Дата"
        event_label = "события"
    else:
        start_date = data.first_date
        start_time = data.start_time
        date_label = "Первая дата"
        event_label = "занятия"

    if start_date < moscow_now.date():
        return f"Поле «{date_label}» {event_label} уже прошло."

    if start_date == moscow_now.date() and start_time is not None:
        start_datetime = datetime.combine(start_date, start_time, tzinfo=_MOSCOW)
        if start_datetime < moscow_now:
            return f"Время начала {event_label} уже прошло."

    return None
