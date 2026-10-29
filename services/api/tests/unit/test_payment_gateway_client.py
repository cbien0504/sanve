import uuid
from collections.abc import Callable

import httpx
import pytest

from app.core.resilience import CircuitBreaker
from app.integrations.payment_gateway import (
    PaymentGatewayClient,
    PaymentGatewayError,
    PaymentGatewayUnavailable,
)

INTENT = {"id": "pi_1", "status": "requires_payment", "checkout_url": "http://pay.test/checkout/pi_1"}


def make_client(
    handler: Callable[[httpx.Request], httpx.Response], breaker: CircuitBreaker | None = None
) -> PaymentGatewayClient:
    return PaymentGatewayClient(
        base_url="http://pay.test",
        api_key="key",
        timeout=1.0,
        max_attempts=3,
        backoff_base=0.001,
        max_concurrency=5,
        breaker=breaker,
        transport=httpx.MockTransport(handler),
    )


async def create(client: PaymentGatewayClient) -> object:
    return await client.create_intent(order_id=uuid.uuid4(), amount=500_000, currency="VND", description="t")


async def test_retries_transient_failures_with_the_same_idempotency_key() -> None:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(503) if len(keys) < 3 else httpx.Response(201, json=INTENT)

    intent = await create(make_client(handler))
    assert intent.id == "pi_1"  # type: ignore[attr-defined]
    assert len(keys) == 3
    assert len(set(keys)) == 1  # retries are only safe because the key never changes


async def test_client_errors_are_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(422, json={"error": "invalid_request"})

    with pytest.raises(PaymentGatewayError):
        await create(make_client(handler))
    assert calls == 1


async def test_timeouts_are_retried_then_reported_as_unavailable() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow gateway", request=request)

    with pytest.raises(PaymentGatewayUnavailable):
        await create(make_client(handler))
    assert calls == 3


async def test_open_circuit_fails_fast_without_calling_the_gateway() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = make_client(handler, breaker=CircuitBreaker(failure_threshold=2, reset_timeout=60))
    for _ in range(2):
        with pytest.raises(PaymentGatewayUnavailable):
            await create(client)
    calls_before = calls
    with pytest.raises(PaymentGatewayUnavailable, match="circuit"):
        await create(client)
    assert calls == calls_before
