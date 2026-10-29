from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, update

from app.domain import booking
from app.models import Hold, Order, OutboxMessage
from app.resources import Resources
from tests.integration.conftest import (
    Account,
    FakePaymentGateway,
    hold_seats,
    payment_event,
    signed_webhook,
)


async def place_order(
    client: httpx.AsyncClient, user: Account, event: dict, quantity: int = 2
) -> dict[str, Any]:
    hold = (await hold_seats(client, user, event, quantity=quantity)).json()
    response = await client.post("/api/v1/orders", json={"hold_id": hold["id"]}, headers=user.headers)
    assert response.status_code == 201, response.text
    return response.json()


async def stats(client: httpx.AsyncClient, organizer: Account, event: dict) -> dict[str, Any]:
    response = await client.get(f"/api/v1/events/{event['id']}/stats", headers=organizer.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def outbox_types(resources: Resources) -> list[str]:
    async with resources.sessionmaker() as session:
        return list(await session.scalars(select(OutboxMessage.event_type).order_by(OutboxMessage.id)))


async def test_paid_order_issues_tickets_exactly_once(
    client: httpx.AsyncClient,
    create_event,
    customer: Account,
    organizer: Account,
    payment_gateway: FakePaymentGateway,
    resources: Resources,
) -> None:
    event = await create_event(seats=10, price=750_000)
    order = await place_order(client, customer, event, quantity=2)
    assert order["status"] == "pending_payment"
    assert order["amount"] == 1_500_000
    assert order["checkout_url"].startswith("http://pay.test/checkout/")

    # Retrying order creation is safe: same order, and no second payment intent at the gateway.
    retry = await client.post("/api/v1/orders", json={"hold_id": order["hold_id"]}, headers=customer.headers)
    assert retry.status_code == 200
    assert retry.json()["id"] == order["id"]
    assert len(payment_gateway.intents) == 1

    intent_id = payment_gateway.intents[order["id"]]["id"]
    webhook = signed_webhook(payment_event(order, intent_id))
    first = await client.post("/api/v1/webhooks/payments", **webhook)
    duplicate = await client.post("/api/v1/webhooks/payments", **webhook)  # gateways deliver at least once
    assert first.json() == {"outcome": "paid"}
    assert duplicate.json() == {"outcome": "duplicate"}

    paid = (await client.get(f"/api/v1/orders/{order['id']}", headers=customer.headers)).json()
    assert paid["status"] == "paid" and paid["paid_at"]
    tickets = (await client.get(f"/api/v1/orders/{order['id']}/tickets", headers=customer.headers)).json()
    assert [t["seat_label"] for t in tickets] == ["A-001", "A-002"]
    assert len({t["code"] for t in tickets}) == 2

    live = await stats(client, organizer, event)
    assert (live["seats_sold"], live["seats_available"]) == (2, 8)
    assert await outbox_types(resources) == ["order.created", "order.paid"]


async def test_gateway_outage_returns_503_and_the_same_request_recovers(
    client: httpx.AsyncClient, create_event, customer: Account, payment_gateway: FakePaymentGateway
) -> None:
    event = await create_event()
    hold = (await hold_seats(client, customer, event, quantity=1)).json()

    payment_gateway.fail_next = 3  # every retry fails
    outage = await client.post("/api/v1/orders", json={"hold_id": hold["id"]}, headers=customer.headers)
    assert outage.status_code == 503
    assert outage.json()["code"] == "payment_unavailable"
    assert outage.headers["Retry-After"] == "5"

    recovered = await client.post("/api/v1/orders", json={"hold_id": hold["id"]}, headers=customer.headers)
    assert recovered.status_code == 200  # the order was created by the first call
    assert recovered.json()["checkout_url"]


async def test_failed_payment_releases_the_seats(
    client: httpx.AsyncClient,
    create_event,
    customer: Account,
    organizer: Account,
    payment_gateway: FakePaymentGateway,
) -> None:
    event = await create_event(seats=4)
    order = await place_order(client, customer, event, quantity=2)
    intent_id = payment_gateway.intents[order["id"]]["id"]

    result = await client.post(
        "/api/v1/webhooks/payments", **signed_webhook(payment_event(order, intent_id, "failed"))
    )
    assert result.json() == {"outcome": "failed"}
    assert (await stats(client, organizer, event))["seats_available"] == 4


async def test_webhooks_must_be_signed_recently_with_our_secret(
    client: httpx.AsyncClient, create_event, customer: Account, payment_gateway: FakePaymentGateway
) -> None:
    event = await create_event()
    order = await place_order(client, customer, event, quantity=1)
    payload = payment_event(order, payment_gateway.intents[order["id"]]["id"])

    forged = await client.post(
        "/api/v1/webhooks/payments", **signed_webhook(payload, secret="guessed-secret")
    )
    replayed = await client.post("/api/v1/webhooks/payments", **signed_webhook(payload, at=1_700_000_000))
    assert forged.status_code == replayed.status_code == 401

    tampered = signed_webhook(payload)
    tampered["content"] = tampered["content"].replace(b'"amount": 500000', b'"amount": 1')
    assert (await client.post("/api/v1/webhooks/payments", **tampered)).status_code == 401


async def test_cancel_and_object_level_authorization(
    client: httpx.AsyncClient, create_event, customer: Account, make_user
) -> None:
    event = await create_event()
    order = await place_order(client, customer, event, quantity=1)
    stranger = await make_user()

    # Another customer cannot read or cancel it: 404, not 403, so ids cannot be probed.
    assert (await client.get(f"/api/v1/orders/{order['id']}", headers=stranger.headers)).status_code == 404
    assert (
        await client.post(f"/api/v1/orders/{order['id']}/cancel", headers=stranger.headers)
    ).status_code == 404

    cancelled = await client.post(f"/api/v1/orders/{order['id']}/cancel", headers=customer.headers)
    assert cancelled.json()["status"] == "cancelled"
    again = await client.post(f"/api/v1/orders/{order['id']}/cancel", headers=customer.headers)
    assert again.status_code == 409


async def test_my_orders_are_paginated_newest_first(
    client: httpx.AsyncClient, create_event, customer: Account
) -> None:
    event = await create_event(seats=10, max_per_user=4)
    created = [(await place_order(client, customer, event, quantity=1))["id"] for _ in range(3)]

    first = (await client.get("/api/v1/orders", params={"limit": 2}, headers=customer.headers)).json()
    second = (
        await client.get(
            "/api/v1/orders", params={"limit": 2, "cursor": first["next_cursor"]}, headers=customer.headers
        )
    ).json()
    assert [o["id"] for o in first["items"] + second["items"]] == list(reversed(created))
    assert second["next_cursor"] is None


async def test_janitor_expires_holds_and_late_payments_need_a_refund(
    client: httpx.AsyncClient,
    create_event,
    customer: Account,
    organizer: Account,
    payment_gateway: FakePaymentGateway,
    resources: Resources,
) -> None:
    event = await create_event(seats=5)
    abandoned = (await hold_seats(client, customer, event, quantity=1)).json()
    order = await place_order(client, customer, event, quantity=2)

    past = datetime.now(UTC) - timedelta(minutes=1)
    async with resources.sessionmaker() as session, session.begin():
        await session.execute(update(Hold).where(Hold.id == abandoned["id"]).values(expires_at=past))
        await session.execute(update(Order).where(Order.id == order["id"]).values(expires_at=past))
    async with resources.sessionmaker() as session, session.begin():
        assert await booking.expire_holds(session) == 1
        assert await booking.expire_orders(session) == 1
    assert (await stats(client, organizer, event))["seats_available"] == 5

    late = await client.post("/api/v1/orders", json={"hold_id": abandoned["id"]}, headers=customer.headers)
    assert late.status_code == 409 and late.json()["code"] == "hold_not_active"

    # The customer paid after the order expired and the seats went back on sale: refund, no tickets.
    intent_id = payment_gateway.intents[order["id"]]["id"]
    result = await client.post("/api/v1/webhooks/payments", **signed_webhook(payment_event(order, intent_id)))
    assert result.json() == {"outcome": "refund_pending"}
    assert (await client.get(f"/api/v1/orders/{order['id']}/tickets", headers=customer.headers)).json() == []
    assert (await outbox_types(resources))[-2:] == ["order.expired", "order.refund_pending"]
