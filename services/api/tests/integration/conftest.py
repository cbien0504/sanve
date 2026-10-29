"""Integration tests run against a real PostgreSQL and Redis (see docker-compose `tests` service)."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.signatures import sign
from app.integrations.payment_gateway import PaymentGatewayClient
from app.main import create_app
from app.models import Base, Role, User
from app.resources import Resources
from app.security import create_access_token, hash_password
from tests.settings import TEST_DATABASE_URL, WEBHOOK_SECRET, make_settings

API_ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "correct-horse-battery-staple"
_PASSWORD_HASH = hash_password(PASSWORD)  # argon2 is slow on purpose: hash once per session


async def _recreate_schema() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def database() -> None:
    asyncio.run(_recreate_schema())
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    # Every migration is exercised in both directions before the tests use the final schema.
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app(make_settings())
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def resources(app: FastAPI) -> Resources:
    res: Resources = app.state.resources
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with res.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await res.redis.flushdb()
    return res


class FakePaymentGateway:
    """In-process stand-in for the Go gateway, plugged in through httpx.MockTransport."""

    def __init__(self) -> None:
        self.intents: dict[str, dict[str, Any]] = {}
        self.fail_next = 0
        self.calls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(503, json={"error": "temporarily_unavailable"})
        body = json.loads(request.content)
        intent = self.intents.get(body["merchant_ref"])
        if intent is not None:
            return httpx.Response(200, json=intent)
        intent_id = f"pi_{len(self.intents) + 1}"
        intent = {
            "id": intent_id,
            "status": "requires_payment",
            "checkout_url": f"http://pay.test/checkout/{intent_id}",
            **body,
        }
        self.intents[body["merchant_ref"]] = intent
        return httpx.Response(201, json=intent)


@pytest.fixture
async def payment_gateway(resources: Resources) -> FakePaymentGateway:
    fake = FakePaymentGateway()
    await resources.payment.aclose()
    resources.payment = PaymentGatewayClient(
        base_url="http://payment.test",
        api_key="test",
        timeout=1.0,
        max_attempts=3,
        backoff_base=0.001,
        max_concurrency=10,
        transport=httpx.MockTransport(fake.handle),
    )
    return fake


@pytest.fixture
async def client(
    app: FastAPI, resources: Resources, payment_gateway: FakePaymentGateway
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@dataclass(frozen=True)
class Account:
    id: uuid.UUID
    email: str
    role: Role
    headers: dict[str, str]


@pytest.fixture
def make_user(resources: Resources) -> Callable[..., Awaitable[Account]]:
    async def _make(role: Role = Role.CUSTOMER) -> Account:
        async with resources.sessionmaker() as session:
            user = User(
                email=f"{role}-{uuid.uuid4().hex[:10]}@test.local",
                password_hash=_PASSWORD_HASH,
                full_name="Nguyễn Văn Test",
                role=role,
            )
            session.add(user)
            await session.commit()
        token = create_access_token(user_id=user.id, role=role, settings=resources.settings)
        return Account(user.id, user.email, role, {"Authorization": f"Bearer {token}"})

    return _make


@pytest.fixture
async def organizer(make_user: Callable[..., Awaitable[Account]]) -> Account:
    return await make_user(Role.ORGANIZER)


@pytest.fixture
async def customer(make_user: Callable[..., Awaitable[Account]]) -> Account:
    return await make_user(Role.CUSTOMER)


def event_payload(
    *,
    seats: int = 10,
    price: int = 500_000,
    sale_open: bool = True,
    max_per_user: int = 4,
    title: str = "Live Concert",
) -> dict[str, Any]:
    now = datetime.now(UTC)
    sale_starts_at = now - timedelta(hours=1) if sale_open else now + timedelta(days=1)
    return {
        "title": title,
        "venue": "Sân vận động Mỹ Đình",
        "city": "Hà Nội",
        "starts_at": (now + timedelta(days=30)).isoformat(),
        "sale_starts_at": sale_starts_at.isoformat(),
        "max_tickets_per_user": max_per_user,
        "tiers": [{"name": "GA", "price": price, "rows": ["A"], "seats_per_row": seats}],
    }


@pytest.fixture
def create_event(client: httpx.AsyncClient, organizer: Account) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def _create(*, publish: bool = True, **kwargs: Any) -> dict[str, Any]:
        response = await client.post(
            "/api/v1/events", json=event_payload(**kwargs), headers=organizer.headers
        )
        assert response.status_code == 201, response.text
        event = response.json()
        if publish:
            response = await client.post(f"/api/v1/events/{event['id']}/publish", headers=organizer.headers)
            assert response.status_code == 200, response.text
            event = response.json()
        return event

    return _create


async def hold_seats(
    client: httpx.AsyncClient, user: Account, event: dict[str, Any], quantity: int = 1, key: str | None = None
) -> httpx.Response:
    return await client.post(
        "/api/v1/holds",
        json={"event_id": event["id"], "tier_id": event["tiers"][0]["id"], "quantity": quantity},
        headers={**user.headers, "Idempotency-Key": key or uuid.uuid4().hex},
    )


def signed_webhook(
    payload: dict[str, Any], secret: str = WEBHOOK_SECRET, at: int | None = None
) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    timestamp = int(time.time()) if at is None else at
    return {
        "content": body,
        "headers": {"X-PayMock-Signature": sign(body, secret, timestamp), "Content-Type": "application/json"},
    }


def payment_event(
    order: dict[str, Any], intent_id: str, kind: str = "succeeded", event_id: str = "evt_1"
) -> dict:
    return {
        "id": event_id,
        "type": f"payment.{kind}",
        "created": int(time.time()),
        "data": {
            "payment_intent_id": intent_id,
            "merchant_ref": order["id"],
            "amount": order["amount"],
            "currency": order["currency"],
        },
    }
