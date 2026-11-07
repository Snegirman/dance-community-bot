from __future__ import annotations

from pathlib import Path

import pytest

from dance_community_bot.__main__ import main
from dance_community_bot.config import SettingsError, load_settings


def test_webhook_settings_are_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:dotenvsecret")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "111")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    with pytest.raises(SettingsError):
        load_settings()


@pytest.mark.parametrize("source", ["dotenv", "environment"])
def test_required_settings_load_from_dotenv_and_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> None:
    monkeypatch.chdir(tmp_path)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_ID",
        "TELEGRAM_WEBHOOK_URL",
        "TELEGRAM_WEBHOOK_SECRET",
        "WEBHOOK_PORT",
        "TELEGRAM_GROUP_ID",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "WEEKLY_PUBLICATION_TIME",
        "DATABASE_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    if source == "dotenv":
        (tmp_path / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=123456:dotenvsecret\n"
            "TELEGRAM_OWNER_ID=111\n"
            "TELEGRAM_WEBHOOK_URL=https://dance.example.org/telegram/webhook\n"
            "TELEGRAM_WEBHOOK_SECRET=dotenv_hook_secret\n"
            "OPENAI_API_KEY=\nTELEGRAM_GROUP_ID=\nWEEKLY_PUBLICATION_TIME=\n"
            "DATABASE_PATH=data/from-dotenv.sqlite3\n",
            encoding="utf-8",
        )
    else:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:dotenvsecret")
        monkeypatch.setenv("TELEGRAM_OWNER_ID", "111")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://dance.example.org/telegram/webhook")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "dotenv_hook_secret")
        monkeypatch.setenv("DATABASE_PATH", "data/from-environment.sqlite3")

    settings = load_settings()

    assert settings.telegram_owner_id == 111
    assert settings.telegram_bot_token.get_secret_value() == "123456:dotenvsecret"
    assert settings.telegram_webhook_url == "https://dance.example.org/telegram/webhook"
    assert settings.telegram_webhook_secret.get_secret_value() == "dotenv_hook_secret"
    assert settings.webhook_port == 8080
    assert settings.database_path == Path(
        "data/from-dotenv.sqlite3" if source == "dotenv" else "data/from-environment.sqlite3"
    )


def test_load_settings_reads_dotenv_and_environment_overrides_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "TELEGRAM_BOT_TOKEN=123456:dotenvsecret\n"
        "TELEGRAM_OWNER_ID=111\n"
        "TELEGRAM_WEBHOOK_URL=https://dance.example.org/from-dotenv\n"
        "TELEGRAM_WEBHOOK_SECRET=dotenv_hook_secret\n"
        "OPENAI_API_KEY=future-key\n"
        "TELEGRAM_GROUP_ID=-100123\n"
        "WEEKLY_PUBLICATION_TIME=09:00\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "987654:environmentsecret")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "222")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://dance.example.org/from-environment")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "environment_hook_secret")

    settings = load_settings()

    assert settings.telegram_owner_id == 222
    assert settings.telegram_bot_token.get_secret_value() == "987654:environmentsecret"
    assert settings.telegram_webhook_url == "https://dance.example.org/from-environment"
    assert settings.telegram_webhook_secret.get_secret_value() == "environment_hook_secret"
    assert "environmentsecret" not in repr(settings)
    assert "environment_hook_secret" not in repr(settings)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("TELEGRAM_WEBHOOK_URL", "http://dance.example.org/webhook"),
        ("TELEGRAM_WEBHOOK_URL", "https://user:password@dance.example.org/webhook"),
        ("TELEGRAM_WEBHOOK_URL", "https://dance.example.org/webhook?mode=1"),
        ("TELEGRAM_WEBHOOK_URL", "https://dance.example.org/webhook#fragment"),
        ("TELEGRAM_WEBHOOK_URL", "https://dance.example.org/telegram/{oops"),
        ("TELEGRAM_WEBHOOK_URL", "https://dance.example.org/hook\n"),
        ("TELEGRAM_WEBHOOK_URL", "https://dance.example.org/hook with space"),
        ("TELEGRAM_WEBHOOK_URL", "https://dance.example.org"),
        ("TELEGRAM_WEBHOOK_URL", "https://dance.example.org:70000/webhook"),
        ("TELEGRAM_WEBHOOK_SECRET", "invalid.secret"),
        ("TELEGRAM_WEBHOOK_SECRET", ""),
        ("TELEGRAM_WEBHOOK_SECRET", "s" * 257),
        ("WEBHOOK_PORT", "0"),
        ("WEBHOOK_PORT", "65536"),
        ("WEBHOOK_PORT", "not-a-port"),
    ],
)
def test_invalid_webhook_settings_fail_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:validtoken")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "111")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://dance.example.org/telegram/webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "valid_hook_secret")
    monkeypatch.setenv("WEBHOOK_PORT", "8090")
    monkeypatch.setenv(key, value)

    with pytest.raises(SettingsError) as error:
        load_settings()

    assert not value or value not in str(error.value)


@pytest.mark.parametrize(
    ("token", "owner_id"),
    [
        ("", "42"),
        ("invalid-token-secret", "42"),
        ("123456:secret with space", "42"),
        ("123456:validtoken", ""),
        ("123456:validtoken", "0"),
        ("123456:validtoken", "-7"),
        ("123456:validtoken", "4.5"),
        ("123456:validtoken", "not-an-id"),
    ],
)
def test_invalid_settings_fail_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str, owner_id: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_OWNER_ID", owner_id)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://dance.example.org/telegram/webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "valid_hook_secret")

    with pytest.raises(SettingsError) as error:
        load_settings()

    message = str(error.value)
    assert "ошибка настроек" in message.lower()
    assert not token or token not in message
    assert not owner_id or owner_id not in message


def test_missing_required_settings_fail_with_a_safe_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_ID", raising=False)

    with pytest.raises(SettingsError) as error:
        load_settings()

    assert "TELEGRAM_BOT_TOKEN" in str(error.value)
    assert "TELEGRAM_OWNER_ID" in str(error.value)


def test_main_reports_configuration_failure_without_traceback_or_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "SECRET_TOKEN_SHOULD_NOT_APPEAR"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "3.5")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://dance.example.org/telegram/webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "valid_hook_secret")

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Traceback" not in captured.err
    assert token not in captured.err
    assert "ошибка настроек" in captured.err.lower()
