import re
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

log = structlog.get_logger("sanve.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-]{1,64}$")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
_UNTRACKED_PATHS = {"/healthz", "/readyz"}


class RequestContextMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware overhead) that, for every request:

    - reuses or creates an X-Request-ID and binds it to every log line of the request;
    - records Prometheus metrics labelled by route *template* (bounded cardinality);
    - writes one structured access log line and adds baseline security headers.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        # Only accept a safe request id from outside: it ends up in logs (log injection).
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                headers.setdefault("Cache-Control", "no-store")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope["path"]
            if path not in _UNTRACKED_PATHS:
                route = scope.get("route")
                route_path = getattr(route, "path", "unmatched")
                duration = time.perf_counter() - started
                HTTP_REQUESTS.labels(scope["method"], route_path, str(status_code)).inc()
                HTTP_LATENCY.labels(scope["method"], route_path).observe(duration)
                log.info(
                    "http_request",
                    method=scope["method"],
                    path=path,
                    route=route_path,
                    status=status_code,
                    duration_ms=round(duration * 1000, 1),
                )
