"""JSON-lines logging with redaction. Message text, names, and raw patient ids never reach the logs."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
REDACTED_KEYS = {"message", "content", "name", "display_name", "patient_id", "token", "authorization",
                 "summary", "args", "arguments", "text"}
_STANDARD_ATTRS = set(vars(logging.LogRecord("x", 0, "x", 0, "", (), None))) | {"message", "asctime"}


def patient_hash(key: str, patient_id: str) -> str:
    return hmac.new(key.encode(), patient_id.encode(), hashlib.sha256).hexdigest()[:16]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            if key in REDACTED_KEYS:
                payload.setdefault("redacted", []).append(key)
                continue
            payload[key] = value
        if record.exc_info and record.exc_info[0]:
            payload["exc"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def log_event(event: str, **fields) -> None:
    # "message" is reserved by logging itself, so strip redacted keys before they reach LogRecord.
    clean = {k: v for k, v in fields.items() if k not in REDACTED_KEYS}
    redacted = sorted(k for k in fields if k in REDACTED_KEYS)
    if redacted:
        clean["redacted"] = redacted
    logging.getLogger("assistant").info(event, extra=clean)
