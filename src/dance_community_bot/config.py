"""Загрузка и проверка обязательных настроек Telegram-бота."""

import re
from datetime import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: SecretStr
    telegram_owner_id: int
    telegram_webhook_url: str
    telegram_webhook_secret: SecretStr
    webhook_port: int = 8080
    database_path: Path = Path("data/dance-community-bot.sqlite3")
    telegram_group_id: int | None = Field(default=None, gt=-(2**52), lt=0)
    weekly_publication_time: time | None = None

    @field_validator("telegram_group_id", mode="before")
    @classmethod
    def normalize_telegram_group_id(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("weekly_publication_time", mode="before")
    @classmethod
    def normalize_weekly_publication_time(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return None
            if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", normalized) is None:
                raise ValueError("Время автопубликации должно быть в формате ЧЧ:ММ.")
            return normalized
        if isinstance(value, time) and value.utcoffset() is not None:
            raise ValueError("Для автопубликации нельзя задавать часовой пояс.")
        return value

    @field_validator("telegram_bot_token")
    @classmethod
    def validate_telegram_bot_token(cls, token: SecretStr) -> SecretStr:
        if re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token.get_secret_value()) is None:
            raise ValueError("Некорректный токен Telegram")
        return token

    @field_validator("telegram_owner_id")
    @classmethod
    def validate_telegram_owner_id(cls, owner_id: int) -> int:
        if owner_id <= 0:
            raise ValueError("ID владельца должен быть положительным целым числом")
        return owner_id

    @field_validator("telegram_webhook_url")
    @classmethod
    def validate_telegram_webhook_url(cls, url: str) -> str:
        try:
            parts = urlsplit(url)
            hostname = parts.hostname
            port = parts.port
        except ValueError:
            raise ValueError("Некорректный URL webhook") from None

        if (
            parts.scheme != "https"
            or not parts.netloc
            or not hostname
            or port == 0
            or any(character.isspace() for character in url)
            or any(character in "{}" for character in unquote(parts.path))
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or "?" in url
            or "#" in url
            or not parts.path
        ):
            raise ValueError("Некорректный URL webhook")
        return url

    @field_validator("telegram_webhook_secret")
    @classmethod
    def validate_telegram_webhook_secret(cls, secret: SecretStr) -> SecretStr:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret.get_secret_value()) is None:
            raise ValueError("Некорректный секрет webhook")
        return secret

    @field_validator("webhook_port")
    @classmethod
    def validate_webhook_port(cls, port: int) -> int:
        if not 1 <= port <= 65535:
            raise ValueError("Порт webhook должен быть от 1 до 65535")
        return port


class SettingsError(Exception):
    """Безопасная ошибка конфигурации без введённых пользователем значений."""


def load_settings() -> Settings:
    """Загрузить настройки из окружения и .env из текущего каталога."""
    try:
        return Settings.model_validate({})
    except ValidationError:
        raise SettingsError(
            "Ошибка настроек: проверьте TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, "
            "TELEGRAM_WEBHOOK_URL, TELEGRAM_WEBHOOK_SECRET, WEBHOOK_PORT "
            "TELEGRAM_GROUP_ID и WEEKLY_PUBLICATION_TIME."
        ) from None
