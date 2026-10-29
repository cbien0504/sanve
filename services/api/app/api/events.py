import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm.exc import StaleDataError

from app.api import API_V1
from app.api.deps import OptionalUserDep, OrganizerDep, ResourcesDep, SessionDep
from app.domain import events as event_service
from app.errors import NotFound, PreconditionFailed, PreconditionRequired, UnprocessableEntity
from app.models import Event, EventSalesStats, EventStatus, Role, Seat
from app.schemas import EventCreate, EventOut, EventSummary, EventUpdate, Page, ProjectionOut, SalesStatsOut

router = APIRouter(prefix=f"{API_V1}/events", tags=["events"])

LIST_NAMESPACE = "events:list"
LIST_TTL = 30
DETAIL_TTL = 300
AVAILABILITY_TTL = 2  # micro-cache: a hot event page costs one count query per 2 s, not per request


def _detail_key(event_id: uuid.UUID) -> str:
    return f"events:detail:{event_id}"


def _etag(version: int) -> str:
    return f'"{version}"'


@router.post("", status_code=201, response_model=EventOut)
async def create_event(
    body: EventCreate, user: OrganizerDep, session: SessionDep, response: Response
) -> EventOut:
    event = await event_service.create_event(session, user.id, body)
    out = await event_service.load_event_out(session, event)
    await session.commit()
    response.headers["Location"] = f"{API_V1}/events/{event.id}"
    response.headers["ETag"] = _etag(event.version)
    return out


@router.get("", response_model=Page[EventSummary])
async def list_events(
    session: SessionDep,
    resources: ResourcesDep,
    city: Annotated[str | None, Query(max_length=100)] = None,
    q: Annotated[str | None, Query(min_length=2, max_length=100, description="Search in titles")] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Page[EventSummary]:
    """Published upcoming events, ordered by start time. Cached for 30 s per distinct query."""
    version = await resources.cache.namespace_version(LIST_NAMESPACE)
    params = hashlib.sha1(f"{city}|{q}|{cursor}|{limit}".encode(), usedforsecurity=False).hexdigest()

    async def load() -> dict:
        page = await event_service.list_published_events(session, city=city, q=q, cursor=cursor, limit=limit)
        return page.model_dump(mode="json")

    data = await resources.cache.get_or_load(
        "events_list", f"{LIST_NAMESPACE}:v{version}:{params}", LIST_TTL, load
    )
    return Page[EventSummary].model_validate(data)


@router.get("/{event_id}", response_model=EventOut)
async def get_event(
    event_id: uuid.UUID,
    session: SessionDep,
    resources: ResourcesDep,
    user: OptionalUserDep,
    response: Response,
) -> EventOut:
    async def load_published() -> dict | None:
        event = await session.get(Event, event_id)
        if event is None or event.status != EventStatus.PUBLISHED:
            return None  # not cached: drafts must never leak through the shared cache
        return (await event_service.load_event_out(session, event)).model_dump(mode="json")

    async def load_availability() -> dict:
        return await event_service.tier_availability(session, event_id)

    cached = await resources.cache.get_or_load(
        "event_detail", _detail_key(event_id), DETAIL_TTL, load_published
    )
    if cached is None:
        # Drafts are only visible to their organizer (and admins).
        event = await session.get(Event, event_id)
        if event is None or user is None or (user.role != Role.ADMIN and user.id != event.organizer_id):
            raise NotFound("Event not found")
        out = await event_service.load_event_out(session, event)
    else:
        out = EventOut.model_validate(cached)
        available = await resources.cache.get_or_load(
            "event_availability", f"events:availability:{event_id}", AVAILABILITY_TTL, load_availability
        )
        for tier in out.tiers:
            tier.available = available.get(str(tier.id), 0)
    response.headers["ETag"] = _etag(out.version)
    return out


@router.patch("/{event_id}", response_model=EventOut)
async def update_event(
    event_id: uuid.UUID,
    body: EventUpdate,
    user: OrganizerDep,
    session: SessionDep,
    resources: ResourcesDep,
    response: Response,
    if_match: Annotated[str | None, Header(description='ETag from a previous GET, e.g. "3"')] = None,
) -> EventOut:
    """Optimistic concurrency: the client must send the ETag it read. If someone changed the event in
    between, the update is rejected with 412 instead of silently overwriting their change."""
    if if_match is None:
        raise PreconditionRequired("If-Match header is required to update an event")
    event = await event_service.get_event_for_update(session, event_id, user)
    if if_match.removeprefix("W/").strip('"') != str(event.version):
        raise PreconditionFailed("The event was modified by someone else; reload and retry")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(event, field, value)
    if event.sale_starts_at >= event.starts_at:
        raise UnprocessableEntity("sale_starts_at must be before starts_at")
    try:
        await session.flush()  # UPDATE ... WHERE id = :id AND version = :seen_version
    except StaleDataError as exc:
        raise PreconditionFailed("The event was modified by someone else; reload and retry") from exc
    out = await event_service.load_event_out(session, event)
    await session.commit()

    await resources.cache.delete(_detail_key(event_id))
    await resources.cache.bump_namespace(LIST_NAMESPACE)
    response.headers["ETag"] = _etag(event.version)
    return out


@router.post("/{event_id}/publish", response_model=EventOut)
async def publish_event(
    event_id: uuid.UUID, user: OrganizerDep, session: SessionDep, resources: ResourcesDep, response: Response
) -> EventOut:
    event = await event_service.get_event_for_update(session, event_id, user)
    await event_service.publish_event(session, event)
    out = await event_service.load_event_out(session, event)
    await session.commit()
    await resources.cache.delete(_detail_key(event_id))
    await resources.cache.bump_namespace(LIST_NAMESPACE)
    response.headers["ETag"] = _etag(event.version)
    return out


@router.get("/{event_id}/stats", response_model=SalesStatsOut)
async def event_stats(event_id: uuid.UUID, user: OrganizerDep, session: SessionDep) -> SalesStatsOut:
    """Live seat counts next to the asynchronous read model, to make eventual consistency visible."""
    # Both reads must see the same snapshot, so this transaction runs at REPEATABLE READ.
    await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    event = await event_service.get_event_for_update(session, event_id, user)
    counts = dict(
        (
            await session.execute(
                select(Seat.status, func.count()).where(Seat.event_id == event.id).group_by(Seat.status)
            )
        ).all()
    )
    projection = await session.get(EventSalesStats, event.id)
    return SalesStatsOut(
        event_id=event.id,
        capacity=sum(counts.values()),
        seats_available=counts.get("available", 0),
        seats_held=counts.get("held", 0),
        seats_sold=counts.get("sold", 0),
        projection=ProjectionOut(
            tickets_sold=projection.tickets_sold if projection else 0,
            orders_paid=projection.orders_paid if projection else 0,
            revenue=projection.revenue if projection else 0,
            updated_at=projection.updated_at if projection else None,
        ),
    )
