import re
import uuid
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models import Role

_ROW_LABEL = re.compile(r"^[A-Z]{1,3}$")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to fetch the next page; null on the last page"
    )


# --- auth -------------------------------------------------------------------------------------------


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - not a secret
    expires_in: int


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=200)


class RoleUpdateIn(BaseModel):
    role: Role


# --- events -----------------------------------------------------------------------------------------


class TierIn(BaseModel):
    name: str = Field(min_length=1, max_length=50, examples=["VIP"])
    price: int = Field(ge=0, le=100_000_000, description="Price in VND", examples=[2_500_000])
    rows: list[str] = Field(min_length=1, max_length=50, examples=[["A", "B"]])
    seats_per_row: int = Field(ge=1, le=500, examples=[20])

    @field_validator("rows")
    @classmethod
    def _valid_rows(cls, rows: list[str]) -> list[str]:
        if any(not _ROW_LABEL.match(row) for row in rows):
            raise ValueError("row labels must be 1-3 uppercase letters")
        if len(set(rows)) != len(rows):
            raise ValueError("row labels must be unique")
        return rows

    @property
    def capacity(self) -> int:
        return len(self.rows) * self.seats_per_row


class EventCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=5000)
    venue: str = Field(min_length=2, max_length=200)
    city: str = Field(min_length=2, max_length=100)
    starts_at: AwareDatetime
    sale_starts_at: AwareDatetime
    max_tickets_per_user: int = Field(default=4, ge=1, le=10)
    tiers: list[TierIn] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.starts_at <= datetime.now(UTC):
            raise ValueError("starts_at must be in the future")
        if self.sale_starts_at >= self.starts_at:
            raise ValueError("sale_starts_at must be before starts_at")
        names = [tier.name for tier in self.tiers]
        if len(set(names)) != len(names):
            raise ValueError("tier names must be unique")
        rows = [row for tier in self.tiers for row in tier.rows]
        if len(set(rows)) != len(rows):
            raise ValueError("a row label can only belong to one tier")
        if sum(tier.capacity for tier in self.tiers) > 50_000:
            raise ValueError("an event can have at most 50,000 seats")
        return self


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    venue: str | None = Field(default=None, min_length=2, max_length=200)
    city: str | None = Field(default=None, min_length=2, max_length=100)
    starts_at: AwareDatetime | None = None
    sale_starts_at: AwareDatetime | None = None
    max_tickets_per_user: int | None = Field(default=None, ge=1, le=10)


class TierOut(ORMModel):
    id: uuid.UUID
    name: str
    price: int
    capacity: int
    available: int | None = Field(default=None, description="Seats still on sale (cached for ~2 seconds)")


class EventOut(ORMModel):
    id: uuid.UUID
    organizer_id: uuid.UUID
    title: str
    description: str
    venue: str
    city: str
    starts_at: datetime
    sale_starts_at: datetime
    status: str
    max_tickets_per_user: int
    version: int
    tiers: list[TierOut]


class EventSummary(ORMModel):
    id: uuid.UUID
    title: str
    venue: str
    city: str
    starts_at: datetime
    sale_starts_at: datetime
    min_price: int


class ProjectionOut(BaseModel):
    tickets_sold: int
    orders_paid: int
    revenue: int
    updated_at: datetime | None = Field(description="When the asynchronous read model last changed")


class SalesStatsOut(BaseModel):
    event_id: uuid.UUID
    capacity: int
    seats_available: int = Field(description="Live count from the seats table (source of truth)")
    seats_held: int
    seats_sold: int
    projection: ProjectionOut = Field(
        description="Eventually consistent read model fed by order.paid messages"
    )


# --- holds & orders ---------------------------------------------------------------------------------


class HoldCreate(BaseModel):
    event_id: uuid.UUID
    tier_id: uuid.UUID
    quantity: int = Field(ge=1, le=10)


class HoldOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    tier_id: uuid.UUID
    quantity: int
    status: str
    seats: list[str]
    expires_at: datetime


class OrderCreate(BaseModel):
    hold_id: uuid.UUID


class OrderOut(ORMModel):
    id: uuid.UUID
    event_id: uuid.UUID
    hold_id: uuid.UUID
    status: str
    quantity: int
    amount: int
    currency: str
    checkout_url: str | None
    expires_at: datetime
    paid_at: datetime | None
    created_at: datetime


class TicketOut(ORMModel):
    id: uuid.UUID
    seat_label: str
    code: str


# --- payment webhook --------------------------------------------------------------------------------


class PaymentWebhookData(BaseModel):
    payment_intent_id: str
    merchant_ref: uuid.UUID
    amount: int
    currency: str


class PaymentWebhook(BaseModel):
    id: str = Field(max_length=100)
    type: Literal["payment.succeeded", "payment.failed"]
    created: int
    data: PaymentWebhookData
