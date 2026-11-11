"""Управление токенами доступа к аукциону EXBO."""

import asyncio
from time import monotonic_ns

import httpx

from .oauth_types import OAuthResponse, _AccessToken

from .http import check_status, decode_response, send_request

_OAUTH_URL = httpx.URL("https://exbo.net/oauth/token")


class AuctionAuth:
    """Кэш токенов с блокировкой параллельных обменов OAuth."""

    def __init__(
        self, access_token: str | None, oauth_form: dict[str, str] | None
    ) -> None:
        """Подготовить токен demo или данные для обмена OAuth.

        Args:
            access_token: Готовый токен demo или None для production.
            oauth_form: Данные OAuth для production или None для demo.
        """
        self._oauth_form = oauth_form
        self._token = (
            _AccessToken(access_token) if access_token is not None else None
        )
        self._lock = asyncio.Lock()

    @property
    def can_refresh(self) -> bool:
        """Определить, доступен ли обмен OAuth.

        Returns:
            True для production с формой OAuth, иначе False.
        """
        return self._oauth_form is not None

    def _cached_token(self) -> _AccessToken | None:
        """Проверить актуальность записи кэша.

        Returns:
            Запись с пригодным токеном или None для обновления.
        """
        token = self._token
        if token is not None and (
            token.refresh_at_ns is None or monotonic_ns() < token.refresh_at_ns
        ):
            return token
        return None

    async def get_token(self, client: httpx.AsyncClient) -> _AccessToken:
        """Взять токен из кэша или выполнить обмен OAuth.

        Args:
            client: HTTP-клиент для запроса токена.

        Returns:
            Пригодный токен и его срок обновления.
        """
        token = self._cached_token()
        if token is not None:
            return token
        async with self._lock:
            token = self._cached_token()
            if token is not None:
                return token
            assert self._oauth_form is not None
            response, received_at = await send_request(
                client, "POST", _OAUTH_URL, form=self._oauth_form
            )
            check_status(response, received_at, oauth=True)
            payload, _ = decode_response(response, OAuthResponse)
            lifetime_ns = payload.expires_in * 1_000_000_000
            margin_ns = min(lifetime_ns // 10, 60_000_000_000)
            refresh_at_ns = monotonic_ns() + lifetime_ns - margin_ns
            token = _AccessToken(payload.access_token, refresh_at_ns)
            self._token = token
            return token

    def discard_token(self, token: _AccessToken) -> None:
        """Удалить запись кэша, если она всё ещё текущая.

        Сравнение по идентичности сохраняет новую запись, даже если
        параллельное обновление вернуло ту же строку токена.

        Args:
            token: Запись токена, который использовался в запросе.
        """
        if self._token is token:
            self._token = None
