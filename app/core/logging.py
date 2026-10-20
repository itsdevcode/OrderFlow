"""Privacy-first JSON logs for application events and the ASGI error boundary."""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import re
import sys
from uuid import UUID


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    method: str
    path: str | None = None
    status: int | None = None
    duration_ms: float | None = None


request_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def validated_request_id(value: str) -> str | None:
    if len(value) != 36:
        return None
    try:
        canonical = str(UUID(value))
    except ValueError:
        return None
    return canonical if canonical == value.lower() else None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = request_context.get()
        event = getattr(record, "event", "application.log")
        if not isinstance(event, str) or event not in {"application.log", "http.request_completed", "http.request_failed", "audit.login_failure_unavailable"}:
            event = "application.log"
        # Legacy messages, args, extras, exc_info, and stack_info may contain
        # credentials/SQL/PII. Never serialize them or call getMessage().
        return json.dumps({
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname,
            "logger": record.name if re.fullmatch(r"(?:app|uvicorn)(?:\.[a-z_][a-z_0-9]*)*", record.name) and len(record.name) <= 128 else "app",
            "event": event,
            "request_id": context.request_id if context else None,
            "http_method": context.method if context else None,
            "path": context.path if context else None,
            "status": context.status if context else None,
            "duration_ms": context.duration_ms if context else None,
        }, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    application = logging.getLogger("app")
    application.setLevel(level)
    application.propagate = False
    handler = next((h for h in application.handlers if h.name == "orderflow.json"), None)
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler.name = "orderflow.json"
        application.addHandler(handler)
    handler.setFormatter(JsonFormatter())

    # Uvicorn's default access records include raw query strings; its error
    # formatter includes raw exception payloads. The middleware supplies safe
    # access records, and this narrowly scoped error handler suppresses payloads.
    # Root/other library loggers and their handlers remain unchanged.
    logging.getLogger("uvicorn.access").disabled = True
    server_errors = logging.getLogger("uvicorn.error")
    server_errors.handlers = [handler]
    server_errors.propagate = False
