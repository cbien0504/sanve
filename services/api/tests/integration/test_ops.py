import uuid

import httpx
import jwt
from prometheus_client import generate_latest


async def test_liveness_and_readiness(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": "ok", "redis": "ok"}


async def test_request_id_and_security_headers(client: httpx.AsyncClient) -> None:
    generated = await client.get("/healthz")
    assert len(generated.headers["X-Request-ID"]) == 32
    assert generated.headers["X-Content-Type-Options"] == "nosniff"
    assert generated.headers["Cache-Control"] == "no-store"

    echoed = await client.get("/healthz", headers={"X-Request-ID": "trace-me-123"})
    assert echoed.headers["X-Request-ID"] == "trace-me-123"
    # Anything that could forge log lines is replaced, not echoed.
    injected = await client.get("/healthz", headers={"X-Request-ID": 'x" level=error fake="1'})
    assert injected.headers["X-Request-ID"] != 'x" level=error fake="1'


async def test_errors_are_problem_details(client: httpx.AsyncClient) -> None:
    invalid = await client.get("/api/v1/events/not-a-uuid")
    assert invalid.status_code == 422
    assert invalid.headers["content-type"] == "application/problem+json"
    body = invalid.json()
    assert body["code"] == "validation_error" and body["errors"][0]["loc"] == ["path", "event_id"]
    assert body["request_id"] == invalid.headers["X-Request-ID"]

    missing = await client.get(f"/api/v1/events/{uuid.uuid4()}")
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


async def test_bad_tokens_are_rejected(client: httpx.AsyncClient) -> None:
    anonymous = await client.get("/api/v1/users/me")
    assert anonymous.status_code == 401
    assert anonymous.headers["WWW-Authenticate"] == "Bearer"

    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "role": "admin"}, "not-our-secret-but-long-enough-0123456789"
    )
    response = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401 and response.json()["code"] == "invalid_token"


async def test_metrics_are_labelled_by_route_template(client: httpx.AsyncClient) -> None:
    await client.get(f"/api/v1/events/{uuid.uuid4()}")
    metrics = generate_latest().decode()
    assert (await client.get("/metrics")).status_code == 404  # not on the public port
    # One series per route template, not one per event id: label cardinality stays bounded.
    assert 'route="/api/v1/events/{event_id}"' in metrics
    assert "sanve_cache_requests_total" in metrics
