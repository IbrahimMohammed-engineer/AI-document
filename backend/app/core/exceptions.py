"""
Application exception hierarchy and FastAPI exception handlers.

All exceptions the application raises are subclasses of AppException.
FastAPI exception handlers catch these and convert them to a standard
error envelope:

    {
        "error": {
            "code":      "MACHINE_READABLE_CODE",
            "message":   "Human-readable description (safe to show to users)",
            "field":     "field_name",   // optional — for validation errors
            "requestId": "uuid"          // always present
        }
    }

Stack traces are NEVER included in responses — they go to logs only.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_request_id

logger = logging.getLogger(__name__)


# ─── Base exception ───────────────────────────────────────────────────────────

class AppException(Exception):
    """Base for all application-level exceptions.

    Subclasses define a default HTTP status code and error code so callers
    only need to pass a message (and optionally a field name for validation
    errors).
    """

    http_status: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str = "An unexpected error occurred.",
        *,
        field: str | None = None,
        headers: dict[str, str] | None = None,
        detail: str | None = None,  # internal-only, never sent to client
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.headers = headers or {}
        self._internal_detail = detail  # logged but NOT returned to client


# ─── HTTP 400 ─────────────────────────────────────────────────────────────────

class ValidationError(AppException):
    """Request body / parameter validation failed."""
    http_status = status.HTTP_400_BAD_REQUEST
    error_code = "VALIDATION_ERROR"


class InvalidFileTypeError(AppException):
    """Uploaded file type is not allowed."""
    http_status = status.HTTP_400_BAD_REQUEST
    error_code = "INVALID_FILE_TYPE"


class FileTooLargeError(AppException):
    """Uploaded file exceeds the configured size limit."""
    http_status = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    error_code = "FILE_TOO_LARGE"


class DuplicateVersionError(AppException):
    """Exact duplicate file within the same document's history."""
    http_status = status.HTTP_409_CONFLICT
    error_code = "DUPLICATE_VERSION"


class ConflictError(AppException):
    """Generic conflict (e.g., unique constraint)."""
    http_status = status.HTTP_409_CONFLICT
    error_code = "CONFLICT"


# ─── HTTP 401 ─────────────────────────────────────────────────────────────────

class AuthenticationError(AppException):
    """User is not authenticated or credentials are invalid.

    Message is always generic to prevent user enumeration.
    """
    http_status = status.HTTP_401_UNAUTHORIZED
    error_code = "INVALID_CREDENTIALS"

    def __init__(self, message: str = "Invalid credentials.", **kwargs):  # type: ignore[override]
        super().__init__(message, **kwargs)


class TokenExpiredError(AppException):
    """Access token has expired — client should refresh."""
    http_status = status.HTTP_401_UNAUTHORIZED
    error_code = "TOKEN_EXPIRED"


class TokenInvalidError(AppException):
    """Access token is malformed or has an invalid signature."""
    http_status = status.HTTP_401_UNAUTHORIZED
    error_code = "TOKEN_INVALID"


# ─── HTTP 403 ─────────────────────────────────────────────────────────────────

class ForbiddenError(AppException):
    """Authenticated user lacks permission for this operation."""
    http_status = status.HTTP_403_FORBIDDEN
    error_code = "FORBIDDEN"


class InsufficientPermissionsError(AppException):
    """User's role does not include the required permission key."""
    http_status = status.HTTP_403_FORBIDDEN
    error_code = "INSUFFICIENT_PERMISSIONS"


# ─── HTTP 404 ─────────────────────────────────────────────────────────────────

class NotFoundError(AppException):
    """Requested resource does not exist or is not visible to the caller."""
    http_status = status.HTTP_404_NOT_FOUND
    error_code = "NOT_FOUND"


# ─── HTTP 429 ─────────────────────────────────────────────────────────────────

class RateLimitExceededError(AppException):
    """Request rate limit exceeded."""
    http_status = status.HTTP_429_TOO_MANY_REQUESTS
    error_code = "RATE_LIMIT_EXCEEDED"


# ─── HTTP 503 ─────────────────────────────────────────────────────────────────

class ExternalServiceError(AppException):
    """An external dependency (DB, storage, AI provider) is unavailable."""
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "EXTERNAL_SERVICE_UNAVAILABLE"


class StorageUnavailableError(ExternalServiceError):
    """Object storage is unreachable or returned an error."""
    error_code = "STORAGE_UNAVAILABLE"


class DatabaseUnavailableError(ExternalServiceError):
    """PostgreSQL is unreachable or returned an error."""
    error_code = "DATABASE_UNAVAILABLE"


# ─── Error envelope builder ───────────────────────────────────────────────────

def _error_envelope(
    code: str,
    message: str,
    field: str | None = None,
) -> dict:
    """Build the standard error response envelope."""
    error: dict = {
        "code": code,
        "message": message,
        "requestId": get_request_id(),
    }
    if field is not None:
        error["field"] = field
    return {"error": error}


# ─── Exception handlers ───────────────────────────────────────────────────────

def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the FastAPI app instance."""

    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        # Log the internal detail if present; never return it to the client
        if exc._internal_detail:
            logger.warning(
                "AppException: %s | detail: %s",
                exc.message,
                exc._internal_detail,
                extra={"error_code": exc.error_code},
            )
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_envelope(exc.error_code, exc.message, exc.field),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Extract the first validation error's field + message for the envelope
        errors = exc.errors()
        first = errors[0] if errors else {}
        field = ".".join(str(loc) for loc in first.get("loc", [])[1:]) or None
        message = first.get("msg", "Request validation failed.")
        logger.debug("Validation error: %s", errors)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_envelope("VALIDATION_ERROR", message, field),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # Log the full traceback server-side; return a safe generic message
        logger.exception(
            "Unhandled exception on %s %s",
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_envelope(
                "INTERNAL_ERROR",
                "An unexpected error occurred. Please try again or contact support.",
            ),
        )
