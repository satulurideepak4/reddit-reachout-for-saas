import json
import logging
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite-friendly)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def from_epoch(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    """Structured (JSON) log line. Never pass secrets or full Reddit content here."""
    logger.log(level, json.dumps({"event": event, **fields}, default=str))
