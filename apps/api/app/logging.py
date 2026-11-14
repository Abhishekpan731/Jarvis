"""Structured logging that cannot leak secrets.

Records are emitted as single-line JSON. A filter drops any field whose name
suggests sensitive content, so a caller cannot accidentally log a token by
passing it as context.
"""

import json
import logging
import sys
from datetime import UTC, datetime

_REDACTED_FIELDS = frozenset(
    {
        "password",
        "password_hash",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "secret",
        "api_key",
        "content",
        "email",
    }
)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in getattr(record, "context", {}).items():
            payload[key] = "[redacted]" if key in _REDACTED_FIELDS else value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def log(logger: logging.Logger, level: int, message: str, **context: object) -> None:
    logger.log(level, message, extra={"context": context})
