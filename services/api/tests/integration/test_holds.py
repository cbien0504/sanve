import asyncio
from collections import Counter

import httpx
from sqlalchemy import func, select

from app.models import Hold, Seat
from app.resources import Resources
from tests.integration.conftest import Account, hold_seats


async def test_flash_sale_never_oversells(
    client: httpx.AsyncClient, create_event, make_user, resources: Resources
) -> None:
    """60 buyers hit the same 20 seats at the same moment."""
    event = await create_event(seats=20)
    buyers = [await make_user() for _ in range(60)]

    responses = await asyncio.gather(*(hold_seats(client, buyer, event) for buyer in buyers))

    assert Counter(r.status_code for r in responses) == {201: 20, 409: 40}
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {"sold_out"}
    seats = [seat for r in responses if r.status_code == 201 for seat in r.json()["seats"]]
    assert len(seats) == len(set(seats)) == 20
    async with resources.sessionmaker() as session:
        held = await session.scalar(
            select(func.count()).where(Seat.status == "held", Seat.hold_id.is_not(None))
        )
    assert held == 20


async def test_per_buyer_limit_survives_parallel_requests(
    client: httpx.AsyncClient, create_event, customer: Account
) -> None:
    """One buyer fires 10 requests in parallel to get around the 4-ticket cap."""
    event = await create_event(seats=50, max_per_user=4)

    responses = await asyncio.gather(*(hold_seats(client, customer, event) for _ in range(10)))

    assert Counter(r.status_code for r in responses) == {201: 4, 409: 6}
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {"ticket_limit_exceeded"}


async def test_idempotency_key_replays_instead_of_reserving_twice(
    client: httpx.AsyncClient, create_event, customer: Account, resources: Resources
) -> None:
    event = await create_event(seats=10)

    first = await hold_seats(client, customer, event, quantity=2, key="checkout-attempt-1")
    retry = await hold_seats(client, customer, event, quantity=2, key="checkout-attempt-1")
    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replayed"] == "true"

    reused = await hold_seats(client, customer, event, quantity=1, key="checkout-attempt-1")
    assert reused.status_code == 422
    assert reused.json()["code"] == "idempotency_key_reused"

    # Same key sent 5 times concurrently (double clicks, client retries): still one hold.
    burst = await asyncio.gather(
        *(hold_seats(client, customer, event, key="checkout-attempt-2") for _ in range(5))
    )
    assert {r.status_code for r in burst} == {201}
    assert len({r.json()["id"] for r in burst}) == 1

    async with resources.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Hold)) == 2


async def test_sale_must_be_open(client: httpx.AsyncClient, create_event, customer: Account) -> None:
    event = await create_event(sale_open=False)
    response = await hold_seats(client, customer, event)
    assert response.status_code == 409
    assert response.json()["code"] == "sale_not_open"


async def test_releasing_a_hold_frees_its_seats(
    client: httpx.AsyncClient, create_event, customer: Account
) -> None:
    event = await create_event(seats=2)
    hold = (await hold_seats(client, customer, event, quantity=2)).json()

    assert (await client.delete(f"/api/v1/holds/{hold['id']}", headers=customer.headers)).status_code == 204
    released = (await client.get(f"/api/v1/holds/{hold['id']}", headers=customer.headers)).json()
    assert released["status"] == "released" and released["seats"] == []

    again = await hold_seats(client, customer, event, quantity=2)
    assert again.status_code == 201
    assert again.json()["seats"] == ["A-001", "A-002"]


async def test_holds_are_private(
    client: httpx.AsyncClient, create_event, customer: Account, make_user
) -> None:
    event = await create_event()
    hold = (await hold_seats(client, customer, event)).json()
    stranger = await make_user()
    assert (await client.get(f"/api/v1/holds/{hold['id']}", headers=stranger.headers)).status_code == 404


async def test_holds_are_rate_limited_per_user(
    client: httpx.AsyncClient, create_event, customer: Account
) -> None:
    event = await create_event(seats=100, max_per_user=10)
    statuses = [(await hold_seats(client, customer, event)).status_code for _ in range(10)]
    assert statuses == [201] * 10

    limited = await hold_seats(client, customer, event)
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    assert "Retry-After" in limited.headers
