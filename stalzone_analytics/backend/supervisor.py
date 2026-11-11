"""Контроль жизненного цикла дочернего процесса ядра."""

import logging
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread, current_thread, main_thread
from time import monotonic

_LOGGER = logging.getLogger(__name__)


def _stop(process: subprocess.Popen) -> None:
    """Запросить штатную остановку и дождаться завершения.

    Args:
        process: Дочерний процесс Auction Engine.
    """
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        process.wait()


def _monitor(stop: Event) -> None:
    """Поднимать дочерний процесс с ограниченной задержкой повторов.

    Args:
        stop: Сигнал завершения от основного процесса.
    """
    delay = 1
    while not stop.is_set():
        started = monotonic()
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "auction_engine"],
                cwd=Path(__file__).resolve().parents[2],
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32" else 0
                ),
                start_new_session=sys.platform != "win32",
            )
            while process.poll() is None and not stop.wait(0.25):
                pass
            if not stop.is_set():
                _LOGGER.warning("Auction Engine exited code=%s",
                                process.returncode)
        except OSError as error:
            _LOGGER.warning(
                "Auction Engine start error=%s", type(error).__name__,
            )
        finally:
            if process is not None:
                _stop(process)
        if monotonic() - started >= 60:
            delay = 1
        stop.wait(delay)
        delay = min(30, delay * 2)


@contextmanager
def supervise_engine():
    """Запустить контроль дочернего процесса в основном процессе.

    Yields:
        Время работы веб-сервера.
    """
    stop = Event()
    monitor = Thread(target=_monitor, args=(stop,), name="auction-supervisor")
    signals = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32":
        signals.append(signal.SIGBREAK)

    def stop_on_signal(signum: int, frame: object) -> None:
        """Остановить дочерний процесс при завершении Uvicorn.

        Args:
            signum: Полученный сигнал.
            frame: Кадр обработчика.
        """
        stop.set()

    handlers = {}
    if current_thread() is main_thread():
        handlers = {sig: signal.signal(sig, stop_on_signal) for sig in signals}
    monitor.start()
    try:
        yield
    finally:
        stop.set()
        monitor.join()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
