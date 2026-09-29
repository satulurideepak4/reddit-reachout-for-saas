import threading
from collections import Counter

_lock = threading.Lock()
_counters: Counter = Counter()


def inc(name: str, n: int = 1) -> None:
    with _lock:
        _counters[name] += n


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counters)
