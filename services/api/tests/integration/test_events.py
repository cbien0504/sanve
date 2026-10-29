import httpx

from app.models import Role
from tests.integration.conftest import Account, event_payload, hold_seats


async def test_customers_cannot_create_events(client: httpx.AsyncClient, customer: Account) -> None:
    response = await client.post("/api/v1/events", json=event_payload(), headers=customer.headers)
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


async def test_event_creation_builds_the_seat_map(client: httpx.AsyncClient, organizer: Account) -> None:
    payload = event_payload()
    payload["tiers"] = [
        {"name": "VIP", "price": 3_000_000, "rows": ["A", "B"], "seats_per_row": 10},
        {"name": "GA", "price": 800_000, "rows": ["C", "D", "E"], "seats_per_row": 20},
    ]
    response = await client.post("/api/v1/events", json=payload, headers=organizer.headers)
    assert response.status_code == 201
    assert response.headers["Location"].endswith(response.json()["id"])
    tiers = {tier["name"]: tier["capacity"] for tier in response.json()["tiers"]}
    assert tiers == {"VIP": 20, "GA": 60}


async def test_drafts_are_private_to_their_organizer(
    client: httpx.AsyncClient, create_event, organizer: Account, make_user
) -> None:
    draft = await create_event(publish=False)
    other = await make_user(Role.ORGANIZER)

    assert (await client.get(f"/api/v1/events/{draft['id']}")).status_code == 404
    assert (await client.get(f"/api/v1/events/{draft['id']}", headers=organizer.headers)).status_code == 200
    # Another organizer cannot even tell that the event exists.
    publish = await client.post(f"/api/v1/events/{draft['id']}/publish", headers=other.headers)
    assert publish.status_code == 404


async def test_updates_use_optimistic_locking(
    client: httpx.AsyncClient, create_event, organizer: Account
) -> None:
    event = await create_event()
    url = f"/api/v1/events/{event['id']}"

    missing = await client.patch(url, json={"title": "New title"}, headers=organizer.headers)
    assert missing.status_code == 428

    first = await client.patch(
        url, json={"title": "New title"}, headers={**organizer.headers, "If-Match": f'"{event["version"]}"'}
    )
    assert first.status_code == 200
    assert first.headers["ETag"] == f'"{event["version"] + 1}"'

    # A second editor still holding the old version must not overwrite the first change.
    stale = await client.patch(
        url, json={"title": "Other title"}, headers={**organizer.headers, "If-Match": f'"{event["version"]}"'}
    )
    assert stale.status_code == 412
    assert (await client.get(url)).json()["title"] == "New title"


async def test_listing_paginates_filters_and_sees_new_events(client: httpx.AsyncClient, create_event) -> None:
    for index in range(3):
        await create_event(title=f"Rock show {index}")

    first = (await client.get("/api/v1/events", params={"limit": 2})).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = (await client.get("/api/v1/events", params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert {e["id"] for e in first["items"]}.isdisjoint({e["id"] for e in second["items"]})

    # The list is cached, but publishing bumps the cache namespace so the new event shows up at once.
    await create_event(title="Jazz night")
    everything = (await client.get("/api/v1/events", params={"limit": 50})).json()
    assert len(everything["items"]) == 4
    search = (await client.get("/api/v1/events", params={"q": "jazz"})).json()
    assert [e["title"] for e in search["items"]] == ["Jazz night"]
    assert search["items"][0]["min_price"] == 500_000

    bad_cursor = await client.get("/api/v1/events", params={"cursor": "garbage"})
    assert bad_cursor.status_code == 422


async def test_event_detail_shows_live_availability(
    client: httpx.AsyncClient, create_event, customer: Account
) -> None:
    event = await create_event(seats=5)
    assert (await hold_seats(client, customer, event, quantity=2)).status_code == 201
    detail = (await client.get(f"/api/v1/events/{event['id']}")).json()
    assert detail["tiers"][0]["available"] == 3
