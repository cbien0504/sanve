import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Role(StrEnum):
    CUSTOMER = "customer"
    ORGANIZER = "organizer"
    ADMIN = "admin"


class EventStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class SeatStatus(StrEnum):
    AVAILABLE = "available"
    HELD = "held"
    SOLD = "sold"


class HoldStatus(StrEnum):
    ACTIVE = "active"
    CONVERTED = "converted"
    EXPIRED = "expired"
    RELEASED = "released"


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REFUND_PENDING = "refund_pending"


def _check_in(column: str, enum: type[StrEnum]) -> CheckConstraint:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=f"{column}_valid")


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True), dict[str, Any]: JSONB}
    # Fetch server-generated columns (created_at...) with RETURNING right away: lazy loading them
    # later is not possible under asyncio.
    __mapper_args__ = {"eager_defaults": True}


class User(Base):
    __tablename__ = "users"
    __table_args__ = (_check_in("role", Role),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default=Role.CUSTOMER)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # All tokens produced by rotating one login share a family; reuse of an old token revokes the family.
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        _check_in("status", EventStatus),
        CheckConstraint("sale_starts_at < starts_at", name="sale_before_show"),
        CheckConstraint("max_tickets_per_user BETWEEN 1 AND 10", name="max_tickets_per_user_range"),
        # Created CONCURRENTLY in migration 0002 (see docs/database.md).
        Index(
            "ix_events_published_starts_at", "starts_at", "id", postgresql_where=text("status = 'published'")
        ),
        Index(
            "ix_events_published_city",
            "city",
            "starts_at",
            "id",
            postgresql_where=text("status = 'published'"),
        ),
        Index(
            "ix_events_title_trgm", "title", postgresql_using="gin", postgresql_ops={"title": "gin_trgm_ops"}
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organizer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    venue: Mapped[str] = mapped_column(String(200))
    city: Mapped[str] = mapped_column(String(100))
    starts_at: Mapped[datetime]
    sale_starts_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(20), default=EventStatus.DRAFT)
    max_tickets_per_user: Mapped[int] = mapped_column(Integer, default=4)
    # Optimistic locking: UPDATE ... WHERE version = :seen, exposed to clients as the ETag.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __mapper_args__ = {"version_id_col": version, "eager_defaults": True}


class TicketTier(Base):
    __tablename__ = "ticket_tiers"
    __table_args__ = (
        UniqueConstraint("event_id", "name"),
        CheckConstraint("price >= 0", name="price_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(50))
    price: Mapped[int] = mapped_column(BigInteger)
    capacity: Mapped[int] = mapped_column(Integer)


class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (
        UniqueConstraint("event_id", "label"),
        _check_in("status", SeatStatus),
        # Only seats that can still be sold are indexed, so the index shrinks as the sale progresses.
        Index("ix_seats_tier_available", "tier_id", "id", postgresql_where=text("status = 'available'")),
        Index("ix_seats_hold_id", "hold_id", postgresql_where=text("hold_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    tier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ticket_tiers.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(20))
    # Server default: seat maps are generated by INSERT ... SELECT inside PostgreSQL.
    status: Mapped[str] = mapped_column(String(20), server_default=SeatStatus.AVAILABLE.value)
    hold_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("holds.id"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Hold(Base):
    __tablename__ = "holds"
    __table_args__ = (
        _check_in("status", HoldStatus),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_holds_active_expires_at", "expires_at", postgresql_where=text("status = 'active'")),
        Index("ix_holds_user_event", "user_id", "event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    tier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ticket_tiers.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default=HoldStatus.ACTIVE)
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        _check_in("status", OrderStatus),
        Index("ix_orders_user_created", "user_id", "created_at", "id"),
        Index(
            "ix_orders_pending_expires_at", "expires_at", postgresql_where=text("status = 'pending_payment'")
        ),
        Index("ix_orders_user_event", "user_id", "event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    # One order per hold: this unique constraint is what makes order creation naturally idempotent.
    hold_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("holds.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.PENDING_PAYMENT)
    quantity: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="VND")
    payment_intent_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    checkout_url: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime]
    paid_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    # Last line of defence against double selling: one ticket per seat, enforced by the database.
    seat_id: Mapped[int] = mapped_column(ForeignKey("seats.id"), unique=True)
    seat_label: Mapped[str] = mapped_column(String(20))
    code: Mapped[str] = mapped_column(String(32), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class OutboxMessage(Base):
    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]]
    # W3C trace context of the request that produced the message, so consumers join the same trace.
    trace_context: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    published_at: Mapped[datetime | None]


class ProcessedMessage(Base):
    """Inbox table: lets at-least-once consumers (queue workers, webhooks) drop duplicates."""

    __tablename__ = "processed_messages"

    consumer: Mapped[str] = mapped_column(String(50), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class EventSalesStats(Base):
    """Read model updated asynchronously from order.paid messages (eventually consistent)."""

    __tablename__ = "event_sales_stats"

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    tickets_sold: Mapped[int] = mapped_column(Integer, server_default="0")
    orders_paid: Mapped[int] = mapped_column(Integer, server_default="0")
    revenue: Mapped[int] = mapped_column(BigInteger, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
