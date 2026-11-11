"""Общий бюджет HTTP-запросов EXBO в процессе ядра."""

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class RateLimiter:
    """Ограничение параллельности и частоты с общей паузой HTTP 429."""

    def __init__(self, concurrency: int, requests_per_minute: int) -> None:
        """Задать пределы для всех запросов процесса.

        Args:
            concurrency: Одновременные HTTP-запросы.
            requests_per_minute: Допустимое число стартов за минуту.
        """
        self._slots = asyncio.Semaphore(concurrency)
        self._budget = requests_per_minute
        self._starts: deque[float] = deque()
        self._paused_until = 0.0
        self._delay = 0.0
        self._last_start = 0.0
        self.rate_limits = 0

    def requests_last_minute(self) -> int:
        """Получить число фактически отправленных запросов.

        Returns:
            Старты запросов за скользящую минуту.
        """
        since = asyncio.get_running_loop().time() - 60
        return sum(start > since for start in self._starts)

    def throttle(self, retry_after: float | None) -> None:
        """Применить паузу ко всем страницам и увеличить интервал.

        Args:
            retry_after: Задержка из ответа EXBO, если она известна.
        """
        now = asyncio.get_running_loop().time()
        self.rate_limits += 1
        self._delay = min(10, max(1, self._delay * 2))
        self._paused_until = max(
            self._paused_until, now + max(retry_after or 0, self._delay),
        )

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Дождаться бюджета перед отправкой HTTP-запроса.

        Yields:
            Один слот до получения ответа или ошибки.
        """
        loop = asyncio.get_running_loop()
        async with self._slots:
            while True:
                now = loop.time()
                while self._starts and self._starts[0] <= now - 60:
                    self._starts.popleft()
                ready = max(self._paused_until, self._last_start + self._delay)
                if len(self._starts) >= self._budget:
                    ready = max(ready, self._starts[0] + 60)
                if ready <= now:
                    self._starts.append(now)
                    self._last_start = now
                    break
                await asyncio.sleep(ready - now)
            yield
            self._delay = max(0, self._delay - 0.1)
