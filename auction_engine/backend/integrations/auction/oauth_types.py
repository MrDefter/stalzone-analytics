"""Токен и исходная модель ответа OAuth EXBO."""

from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field

BEARER_TOKEN_PATTERN = r"^[!-~]+$"


@dataclass(frozen=True, slots=True, repr=False)
class _AccessToken:
    """Запись кэша с токеном и временем его обновления.

    Attributes:
        value: Значение токена для заголовка Authorization.
        refresh_at_ns: Монотонный момент обновления в наносекундах
            или None для demo.
    """

    value: str
    refresh_at_ns: int | None = None


class OAuthResponse(BaseModel):
    """Токен приложения и срок его действия в секундах."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    access_token: str = Field(pattern=BEARER_TOKEN_PATTERN, repr=False)
    expires_in: int = Field(gt=0)
