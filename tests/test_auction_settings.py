"""Проверки загрузки настроек аукциона."""

from pathlib import Path

import pytest

from auction_engine.backend import settings as auction
from auction_engine.backend.settings import (
    AuctionSettings,
    DemoAuctionSettings,
)


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Направить загрузку настроек на временный файл.

    Args:
        tmp_path: Временный каталог pytest.
        monkeypatch: Фикстура для подмены пути и окружения.

    Returns:
        Путь к .env внутри временного каталога.
    """
    path = tmp_path / ".env"
    monkeypatch.setattr(auction, "_ENV_FILE", path)
    monkeypatch.delenv("STALZONE_CLIENT_ID", raising=False)
    monkeypatch.delenv("STALZONE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("STALZONE_DEMO_TOKEN", raising=False)
    return path


def test_dotenv_preserves_literal_secret(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить чтение секрета без подстановки переменных.

    Args:
        env_file: Путь к временному .env.
        monkeypatch: Фикстура для задания значения подстановки.
    """
    secret = "literal-${TOKEN_SUFFIX}"
    monkeypatch.setenv("TOKEN_SUFFIX", "expanded")
    env_file.write_text(
        f"STALZONE_CLIENT_ID=file-id\nSTALZONE_CLIENT_SECRET='{secret}'\n",
        encoding="utf-8-sig",
    )

    settings = AuctionSettings.from_env()

    assert settings.client_id == "file-id"
    assert settings.client_secret.get_secret_value() == secret
    assert secret not in repr(settings)


def test_environment_takes_precedence(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить приоритет переменных окружения над .env.

    Args:
        env_file: Путь к временному .env.
        monkeypatch: Фикстура для задания значений в окружении.
    """
    env_file.write_text(
        "STALZONE_CLIENT_ID=file-id\nSTALZONE_CLIENT_SECRET=file-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("STALZONE_CLIENT_ID", "process-id")
    monkeypatch.setenv("STALZONE_CLIENT_SECRET", "process-secret")

    settings = AuctionSettings.from_env()

    assert settings.client_id == "process-id"
    assert settings.client_secret.get_secret_value() == "process-secret"


def test_missing_id_reports_variable_without_exposing_secret(
    env_file: Path,
) -> None:
    """Проверить сообщение об отсутствующем идентификаторе.

    Args:
        env_file: Путь к .env, содержащему только секрет.
    """
    env_file.write_text(
        "STALZONE_CLIENT_SECRET=file-secret\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="STALZONE_CLIENT_ID") as caught:
        AuctionSettings.from_env()

    assert "file-secret" not in str(caught.value)


def test_demo_loads_separate_token(env_file: Path) -> None:
    """Проверить загрузку токена demo из отдельной настройки.

    Args:
        env_file: Путь к .env с одним токеном demo.
    """
    env_file.write_text(
        "STALZONE_DEMO_TOKEN=demo-test-token\n", encoding="utf-8"
    )

    settings = DemoAuctionSettings.from_env()

    assert settings.access_token.get_secret_value() == "demo-test-token"
    assert "demo-test-token" not in repr(settings)
