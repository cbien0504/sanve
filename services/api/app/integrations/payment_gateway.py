import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.core.resilience import CircuitBreaker, backoff_delay
from app.observability.metrics import PAYMENT_CALLS

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PaymentIntent:
    id: str
    status: str
    checkout_url: str


class PaymentGatewayError(Exception):
    """The gateway rejected the request (4xx): retrying will not help."""


class PaymentGatewayUnavailable(Exception):
    """The gateway is down, slow or the circuit is open: safe to retry later."""


class PaymentGatewayClient:
    """HTTP client for the payment gateway with the usual protections for a remote dependency:

    - per-attempt timeouts (connect/read) so a slow gateway cannot hold our workers;
    - retries with exponential backoff + jitter, only for transient failures (network, 5xx, 429);
    - an Idempotency-Key derived from the order id, which makes those retries safe;
    - a semaphore (bulkhead) capping concurrent calls, and a circuit breaker to fail fast.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float,
        max_attempts: int,
        backoff_base: float,
        max_concurrency: int,
        breaker: CircuitBreaker | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=min(1.0, timeout)),
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._bulkhead = asyncio.Semaphore(max_concurrency)
        self.breaker = breaker or CircuitBreaker()

    async def create_intent(
        self, *, order_id: uuid.UUID, amount: int, currency: str, description: str
    ) -> PaymentIntent:
        response = await self._request(
            "POST",
            "/v1/payment-intents",
            json={
                "merchant_ref": str(order_id),
                "amount": amount,
                "currency": currency,
                "description": description,
            },
            headers={"Idempotency-Key": f"order-{order_id}"},
        )
        data = response.json()
        return PaymentIntent(id=data["id"], status=data["status"], checkout_url=data["checkout_url"])

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if not self.breaker.allow_request():
            PAYMENT_CALLS.labels("circuit_open").inc()
            raise PaymentGatewayUnavailable("circuit breaker is open")

        last_error: Exception | None = None
        async with self._bulkhead:
            for attempt in range(1, self._max_attempts + 1):
                try:
                    response = await self._client.request(method, url, **kwargs)
                except httpx.TransportError as exc:  # includes timeouts
                    last_error = exc
                else:
                    if response.status_code < 500 and response.status_code != 429:
                        self.breaker.record_success()
                        if response.is_error:
                            PAYMENT_CALLS.labels("rejected").inc()
                            raise PaymentGatewayError(f"gateway rejected request: {response.status_code}")
                        PAYMENT_CALLS.labels("success").inc()
                        return response
                    last_error = PaymentGatewayUnavailable(f"gateway returned {response.status_code}")

                if attempt < self._max_attempts:
                    PAYMENT_CALLS.labels("retry").inc()
                    delay = backoff_delay(attempt, base=self._backoff_base)
                    log.warning(
                        "payment_gateway_retry", attempt=attempt, delay=round(delay, 3), error=str(last_error)
                    )
                    await asyncio.sleep(delay)

        self.breaker.record_failure()
        PAYMENT_CALLS.labels("error").inc()
        raise PaymentGatewayUnavailable(str(last_error)) from last_error
