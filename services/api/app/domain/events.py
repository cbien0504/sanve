import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import decode_cursor, encode_cursor
from app.errors import Conflict, NotFound
from app.models import Event, EventStatus, Role, SeatStatus, TicketTier
from app.schemas import EventCreate, EventOut, EventSummary, Page, TierOut
from app.security import CurrentUser


async def create_event(session: AsyncSession, organizer_id: uuid.UUID, data: EventCreate) -> Event:
    event = Event(
        organizer_id=organizer_id,
        title=data.title,
        description=data.description,
        venue=data.venue,
        city=data.city,
        starts_at=data.starts_at,
        sale_starts_at=data.sale_starts_at,
        max_tickets_per_user=data.max_tickets_per_user,
        status=EventStatus.DRAFT,
    )
    session.add(event)
    await session.flush()

    for tier_in in data.tiers:
        tier = TicketTier(
            event_id=event.id, name=tier_in.name, price=tier_in.price, capacity=tier_in.capacity
        )
        session.add(tier)
        await session.flush()
        # Build the whole seat map inside PostgreSQL: one statement per tier instead of one per seat.
        await session.execute(
            text(
                """
                INSERT INTO seats (event_id, tier_id, label)
                SELECT :event_id, :tier_id, r.row_label || '-' || lpad(n::text, 3, '0')
                FROM unnest(CAST(:rows AS text[])) AS r(row_label)
                CROSS JOIN generate_series(1, :per_row) AS n
                """
            ),
            {
                "event_id": event.id,
                "tier_id": tier.id,
                "rows": tier_in.rows,
                "per_row": tier_in.seats_per_row,
            },
        )
    return event


async def get_event_for_update(session: AsyncSession, event_id: uuid.UUID, user: CurrentUser) -> Event:
    """Organizers only see their own events; answering 404 (not 403) does not reveal that it exists."""
    event = await session.get(Event, event_id)
    if event is None or (user.role != Role.ADMIN and event.organizer_id != user.id):
        raise NotFound("Event not found")
    return event


async def publish_event(session: AsyncSession, event: Event) -> None:
    if event.status != EventStatus.DRAFT:
        raise Conflict("Only draft events can be published", code="event_not_draft")
    event.status = EventStatus.PUBLISHED
    await session.flush()


async def load_event_out(session: AsyncSession, event: Event) -> EventOut:
    tiers = await session.scalars(
        select(TicketTier).where(TicketTier.event_id == event.id).order_by(TicketTier.price.desc())
    )
    return EventOut(
        id=event.id,
        organizer_id=event.organizer_id,
        title=event.title,
        description=event.description,
        venue=event.venue,
        city=event.city,
        starts_at=event.starts_at,
        sale_starts_at=event.sale_starts_at,
        status=event.status,
        max_tickets_per_user=event.max_tickets_per_user,
        version=event.version,
        tiers=[TierOut.model_validate(tier) for tier in tiers],
    )


async def tier_availability(session: AsyncSession, event_id: uuid.UUID) -> dict[str, int]:
    # Correlated count per tier hits the partial index ix_seats_tier_available (index-only scan).
    rows = await session.execute(
        text(
            """
            SELECT t.id::text AS tier_id,
                   (SELECT count(*) FROM seats s
                    WHERE s.tier_id = t.id AND s.status = :available) AS available
            FROM ticket_tiers t
            WHERE t.event_id = :event_id
            """
        ),
        {"event_id": event_id, "available": SeatStatus.AVAILABLE.value},
    )
    return {row.tier_id: row.available for row in rows}


def _published_events_query(city: str | None, q: str | None) -> Select:
    min_price = (
        select(func.min(TicketTier.price))
        .where(TicketTier.event_id == Event.id)
        .correlate(Event)
        .scalar_subquery()
    )
    stmt = select(Event, min_price.label("min_price")).where(
        Event.status == EventStatus.PUBLISHED, Event.starts_at > func.now()
    )
    if city:
        stmt = stmt.where(Event.city == city)
    if q:
        # ILIKE '%q%' is served by the pg_trgm GIN index created in migration 0002.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Event.title.ilike(f"%{escaped}%"))
    return stmt


async def list_published_events(
    session: AsyncSession, *, city: str | None, q: str | None, cursor: str | None, limit: int
) -> Page[EventSummary]:
    stmt = _published_events_query(city, q)
    if cursor:
        last_starts_at, last_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Event.starts_at, Event.id) > tuple_(last_starts_at, last_id))
    rows = (await session.execute(stmt.order_by(Event.starts_at, Event.id).limit(limit + 1))).all()

    items = [
        EventSummary(
            id=event.id,
            title=event.title,
            venue=event.venue,
            city=event.city,
            starts_at=event.starts_at,
            sale_starts_at=event.sale_starts_at,
            min_price=min_price or 0,
        )
        for event, min_price in rows[:limit]
    ]
    next_cursor = encode_cursor(items[-1].starts_at, items[-1].id) if len(rows) > limit else None
    return Page[EventSummary](items=items, next_cursor=next_cursor)


def sale_is_open(event: Event, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return event.status == EventStatus.PUBLISHED and event.sale_starts_at <= now < event.starts_at
