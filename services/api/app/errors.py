"""Errors are returned as RFC 9457 Problem Details (application/problem+json)."""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import LOCK_NOT_AVAILABLE, QUERY_CANCELED, sqlstate

log = structlog.get_logger(__name__)


class AppError(Exception):
    status_code = 400
    code = "bad_request"
    title = "Bad request"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.code = code or self.code
        self.headers = headers
        self.extra = extra or {}


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"
    title = "Authentication required"

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(detail, **kwargs)


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"
    title = "Forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    title = "Resource not found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    title = "Conflict"


class PreconditionFailed(AppError):
    status_code = 412
    code = "precondition_failed"
    title = "Precondition failed"


class UnprocessableEntity(AppError):
    status_code = 422
    code = "unprocessable_entity"
    title = "Unprocessable entity"


class PreconditionRequired(AppError):
    status_code = 428
    code = "precondition_required"
    title = "Precondition required"


class TooManyRequests(AppError):
    status_code = 429
    code = "rate_limited"
    title = "Too many requests"


class BadGateway(AppError):
    status_code = 502
    code = "bad_gateway"
    title = "Upstream service rejected the request"


class ServiceUnavailable(AppError):
    status_code = 503
    code = "service_unavailable"
    title = "Service temporarily unavailable"


def problem(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body = {
        "type": f"urn:sanve:problem:{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
        "request_id": getattr(request.state, "request_id", None),
        **(extra or {}),
    }
    return JSONResponse(body, status_code=status, headers=headers, media_type="application/problem+json")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return problem(
            request,
            status=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            headers=exc.headers,
            extra=exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]} for err in exc.errors()]
        return problem(
            request,
            status=422,
            code="validation_error",
            title="Request validation failed",
            detail="One or more fields are invalid",
            extra={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(
            request,
            status=exc.status_code,
            code=f"http_{exc.status_code}",
            title=str(exc.detail),
            detail=str(exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(DBAPIError)
    async def _db_error(request: Request, exc: DBAPIError) -> JSONResponse:
        state = sqlstate(exc)
        if state == LOCK_NOT_AVAILABLE:
            return problem(
                request,
                status=409,
                code="resource_busy",
                title="Resource is busy",
                detail="Another request is updating the same data, please retry",
                headers={"Retry-After": "1"},
            )
        if state == QUERY_CANCELED:
            log.warning("db_statement_timeout", path=request.url.path)
            return problem(
                request,
                status=503,
                code="db_timeout",
                title="Database timeout",
                detail="The request took too long, please retry",
                headers={"Retry-After": "2"},
            )
        log.exception("db_error", sqlstate=state)
        return _internal_error(request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error")
        return _internal_error(request)


def _internal_error(request: Request) -> JSONResponse:
    # Never leak exception details to clients; the request_id links the response to the logs.
    return problem(
        request,
        status=500,
        code="internal_error",
        title="Internal server error",
        detail="Something went wrong on our side",
    )
