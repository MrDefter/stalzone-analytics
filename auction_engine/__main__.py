"""Запуск одного фонового ядра с обработкой сигналов остановки."""

import asyncio
import logging
import signal
import sys
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import gettempdir

from auction_engine.backend.engine_settings import AuctionEngineSettings

from .backend.engine import AuctionEngine


@contextmanager
def _lock(settings: AuctionEngineSettings):
    """Удерживать блокировку писателя на локальном компьютере.

    Args:
        settings: Адрес и база, общие для экземпляров ядра.

    Yields:
        Право запуска единственного локального писателя.
    """
    name = f"{settings.host}:{settings.port}/{settings.snapshots_database}"
    digest = sha256(name.encode()).hexdigest()[:20]
    path = Path(gettempdir()) / f"stalzone-auction-{digest}.lock"
    with path.open("a+b") as handle:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


async def _run(settings: AuctionEngineSettings) -> None:
    """Запустить планировщик и завершить его при системном сигнале.

    Args:
        settings: Конфигурация запуска ядра.
    """
    task = asyncio.create_task(AuctionEngine(settings).run())
    signals = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32":
        signals.append(signal.SIGBREAK)

    def stop(signum: int, frame: object) -> None:
        """Передать отмену задаче ядра.

        Args:
            signum: Полученный системный сигнал.
            frame: Кадр выполнения обработчика.
        """
        if not task.cancelling():
            task.cancel()

    for signum in signals:
        signal.signal(signum, stop)
    try:
        await task
    except asyncio.CancelledError:
        pass


def main() -> int:
    """Запустить ядро или сообщить о занятой блокировке.

    Returns:
        Код завершения дочернего процесса.
    """
    logging.basicConfig(level=logging.INFO)
    settings = AuctionEngineSettings()
    try:
        with _lock(settings):
            asyncio.run(_run(settings))
    except OSError:
        logging.error("Не удалось запустить писатель Auction Engine.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
