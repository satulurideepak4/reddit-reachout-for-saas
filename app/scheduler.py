import logging
import threading
from typing import Callable, Protocol

from app.util import log_event

log = logging.getLogger(__name__)


class JobScheduler(Protocol):
    """Swap this for Quartz/Celery/Kafka-driven triggers later; the job is just a callable."""

    def start(self) -> None: ...

    def stop(self) -> None: ...


class IntervalScheduler:
    """In-process scheduler: runs `job` after `initial_delay`, then every `interval` seconds."""

    def __init__(self, job: Callable[[], object], interval_seconds: float, initial_delay_seconds: float = 0):
        self.job = job
        self.interval = interval_seconds
        self.initial_delay = initial_delay_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="reddit-monitor", daemon=True)
        self._thread.start()
        log_event(log, "scheduler.start", interval_s=self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        log_event(log, "scheduler.stop")

    def _loop(self) -> None:
        wait = self.initial_delay
        while not self._stop.wait(wait):
            try:
                self.job()
            except Exception as exc:  # noqa: BLE001 - a bad cycle must never kill the scheduler
                log_event(log, "scheduler.job_error", level=logging.ERROR, error=f"{type(exc).__name__}: {exc}"[:300])
            wait = self.interval
