from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import start_http_server

from app.api import auth, events, health, holds, orders, webhooks
from app.api.middleware import RequestContextMiddleware
from app.config import Settings, get_settings
from app.errors import register_exception_handlers
from app.observability.logging import configure_logging
from app.observability.tracing import configure_sentry, configure_tracing, instrument_app
from app.resources import Resources

DESCRIPTION = """
SanVe sells concert tickets in flash sales: thousands of buyers race for a few thousand seats.

Conventions:
* Errors are RFC 9457 Problem Details (`application/problem+json`) with a stable `code`.
* Lists use cursor pagination: pass `next_cursor` back as `cursor`.
* `POST /holds` requires an `Idempotency-Key` header; `PATCH /events/{id}` requires `If-Match`.
* Rate-limited endpoints answer 429 with `Retry-After`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    metrics_server = start_http_server(settings.metrics_port)[0] if settings.metrics_port else None
    resources = Resources.create(settings)
    app.state.resources = resources
    try:
        yield
    finally:
        await resources.close()
        if metrics_server is not None:
            metrics_server.shutdown()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    configure_tracing(settings)
    configure_sentry(settings)

    app = FastAPI(title="SanVe API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)
    app.state.settings = settings
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match"],
            expose_headers=["ETag", "Location", "Retry-After", "X-Request-ID"],
        )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    for module in (health, auth, events, holds, orders, webhooks):
        app.include_router(module.router)
    instrument_app(app)
    return app
