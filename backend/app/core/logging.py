"""
Structured JSON logging + correlation ID propagation.

Every log line emitted by the application carries:
  - timestamp (ISO 8601)
  - level
  - logger name
  - request_id (UUID, propagated via contextvars)
  - any extra key=value pairs passed by the caller

The correlation ID is set by RequestIdMiddleware from the incoming
X-Request-Id header (if a gateway sends one) or generated fresh per request.
It is stored in a ContextVar so it flows through async call chains without
explicit parameter threading.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Awaitable, Callable

from fastapi import Request, Response

# ─── Correlation ID context var ───────────────────────────────────────────────
# This is the single source of truth for the current request's correlation ID.
# It is set at the start of each request by RequestIdMiddleware and readable
# anywhere in the call chain (including from background tasks that copy the
# context).
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """Return the current request's correlation ID (empty string outside a request)."""
    return _request_id_var.get()


def set_request_id(request_id: str) -> None:
    """Set the current request's correlation ID."""
    _request_id_var.set(request_id)


# ─── JSON log formatter ───────────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    """Emit one JSON object per log line with standard fields."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Always include correlation ID if set
        request_id = get_request_id()
        if request_id:
            log_data["request_id"] = request_id

        # Include any extra fields passed via logging.extra
        for key, value in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            } and not key.startswith("_"):
                log_data[key] = value

        # Include exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, default=str)


# ─── Setup function ───────────────────────────────────────────────────────────

def setup_logging(log_level: str = "INFO") -> None:
    """Configure root logger with the JSON formatter.

    Call this once at application startup before any other code runs.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Remove any existing handlers (e.g., from uvicorn)
    root.handlers.clear()
    root.addHandler(handler)

    # Quieten noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.  Usage: logger = get_logger(__name__)"""
    return logging.getLogger(name)


# ─── ASGI Middleware ──────────────────────────────────────────────────────────

class RequestIdMiddleware:
    """ASGI middleware that:

    1. Reads X-Request-Id from the incoming request (if a gateway sets one)
       or generates a fresh UUID v4.
    2. Stores it in the _request_id_var ContextVar for the duration of the
       request (async-safe — ContextVar is automatically scoped per asyncio Task).
    3. Adds X-Request-Id to the outgoing response so the client can correlate
       log lines with their request.
    4. Emits a structured access log line on completion.
    """

    def __init__(self, app: Callable) -> None:
        self.app = app
        self._logger = get_logger("http.access")

    async def __call__(
        self,
        scope: dict,
        receive: Callable,
        send: Callable,
    ) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Resolve or generate correlation ID
        headers = dict(scope.get("headers", []))
        incoming_id = headers.get(b"x-request-id", b"").decode("utf-8")
        request_id = incoming_id if incoming_id else str(uuid.uuid4())

        # Store in ContextVar — automatically isolated per async Task
        token = _request_id_var.set(request_id)

        start_time = time.perf_counter()

        async def send_with_header(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers_list = list(message.get("headers", []))
                headers_list.append(
                    (b"x-request-id", request_id.encode("utf-8"))
                )
                message = {**message, "headers": headers_list}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000
            if scope["type"] == "http":
                method = scope.get("method", "")
                path = scope.get("path", "")
                self._logger.info(
                    "%s %s completed in %.1fms",
                    method,
                    path,
                    duration_ms,
                    extra={"method": method, "path": path, "duration_ms": round(duration_ms, 1)},
                )
            _request_id_var.reset(token)
