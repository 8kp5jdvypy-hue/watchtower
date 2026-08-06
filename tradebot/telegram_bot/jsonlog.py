"""Structured JSON logging for the outbox worker — one JSON object per
line, so delivery events can be grepped/correlated by alert_id across a
worker's whole lifetime instead of parsed out of a free-text sentence.

Usage: configure a handler with JsonFormatter(), then log with
`logger.info("delivered", extra={"alert_id": ..., "chat_id": ...})` —
any extra field on the LogRecord gets included in the JSON object.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

# Attributes every stdlib LogRecord already carries — anything else set
# via `extra=` is assumed to be a field this project actually wants in
# the structured output (alert_id, chat_id, event, attempts, ...).
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_json_logging(logger: logging.Logger, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False
