"""Загрузка учётных данных для API EXBO."""

from pathlib import Path
from typing import Self

from dotenv import dotenv_values
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
_CLIENT_ID = "STALZONE_CLIENT_ID"
_CLIENT_SECRET = "STALZONE_CLIENT_SECRET"
_DEMO_TOKEN = "STALZONE_DEMO_TOKEN"


class _AuctionCredentials(BaseSettings):
    """Общие источники и проверка учётных данных EXBO."""

    model_config = SettingsConfigDict(
        case_sensitive=True,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Задать приоритет источников и читать значения .env буквально.

        Args:
            settings_cls: Класс настроек, создаваемый Pydantic.
            init_settings: Значения, переданные в конструктор.
            env_settings: Переменные окружения процесса.
            dotenv_settings: Стандартный источник Pydantic для .env.
            file_secret_settings: Источник файлов секретов Pydantic.

        Returns:
            Источники в порядке убывания приоритета.
        """
        file_values = dotenv_values(
            _ENV_FILE, interpolate=False, encoding="utf-8-sig"
        )
        aliases = (
            field.validation_alias
            for field in settings_cls.model_fields.values()
        )
        credentials = {
            name: file_values[name]
            for name in aliases
            if isinstance(name, str) and name in file_values
        }
        file_source = InitSettingsSource(settings_cls, credentials)
        return init_settings, env_settings, file_source

    @field_validator("*")
    @classmethod
    def _validate_nonblank(cls, value: str | SecretStr) -> str | SecretStr:
        """Проверить, что в учётном значении есть непробельный символ.

        Args:
            value: Идентификатор, защищённый секрет или токен.

        Returns:
            Исходное значение после проверки.

        Raises:
            ValueError: Значение пустое или состоит из пробелов.
        """
        text = (
            value.get_secret_value() if isinstance(value, SecretStr) else value
        )
        if not text.strip():
            raise ValueError("credential must not be blank")
        return value

    @classmethod
    def from_env(cls) -> Self:
        """Загрузить настройки с приоритетом окружения над .env.

        Returns:
            Настройки с проверенными данными доступа.

        Raises:
            ValueError: Обязательное значение отсутствует или пусто.
        """
        try:
            return cls()
        except ValidationError as exc:
            errors = exc.errors(
                include_input=False, include_context=False, include_url=False
            )
            names = ", ".join(str(error["loc"][0]) for error in errors)
            raise ValueError(
                f"Set {names} in the project .env or the process environment"
            ) from None


class AuctionSettings(_AuctionCredentials):
    """Настройки приложения для доступа к production API EXBO."""

    client_id: str = Field(validation_alias=_CLIENT_ID)
    client_secret: SecretStr = Field(validation_alias=_CLIENT_SECRET)


class DemoAuctionSettings(_AuctionCredentials):
    """Настройки доступа к демонстрационному API EXBO."""

    access_token: SecretStr = Field(validation_alias=_DEMO_TOKEN)
