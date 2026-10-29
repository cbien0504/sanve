import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response
from sqlalchemy import select, tuple_

from app.api import API_V1
from app.api.deps import CurrentUserDep, ResourcesDep, SessionDep
from app.core.pagination import decode_cursor, encode_cursor
from app.domain import booking
from app.models import Order, Ticket
from app.schemas import OrderCreate, OrderOut, Page, TicketOut

router = APIRouter(prefix=f"{API_V1}/orders", tags=["orders"])


@router.post(
    "",
    status_code=201,
    response_model=OrderOut,
    responses={
        200: {"description": "An order already exists for this hold (safe retry)", "model": OrderOut},
        503: {"description": "Payment gateway unavailable; retry the same request"},
    },
)
async def create_order(
    body: OrderCreate, user: CurrentUserDep, resources: ResourcesDep, response: Response
) -> Order:
    """Turn an active hold into an order and get a `checkout_url` from the payment gateway.

    Naturally idempotent: one order per hold, so retrying returns the same order (200).
    """
    order, created = await booking.create_order(resources, user, body.hold_id)
    if not created:
        response.status_code = 200
    response.headers["Location"] = f"{API_V1}/orders/{order.id}"
    return order


@router.get("", response_model=Page[OrderOut])
async def list_my_orders(
    user: CurrentUserDep,
    session: SessionDep,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Page[OrderOut]:
    # Newest first; served by the (user_id, created_at, id) index.
    stmt = select(Order).where(Order.user_id == user.id)
    if cursor:
        created_at, order_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Order.created_at, Order.id) < tuple_(created_at, order_id))
    orders = list(
        await session.scalars(stmt.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit + 1))
    )
    page = orders[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(orders) > limit else None
    return Page[OrderOut](items=[OrderOut.model_validate(o) for o in page], next_cursor=next_cursor)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: uuid.UUID, user: CurrentUserDep, session: SessionDep) -> Order:
    return await booking.get_order(session, user, order_id)


@router.post("/{order_id}/cancel", response_model=OrderOut)
async def cancel_order(order_id: uuid.UUID, user: CurrentUserDep, session: SessionDep) -> Order:
    order = await booking.cancel_order(session, user, order_id)
    await session.commit()
    return order


@router.get("/{order_id}/tickets", response_model=list[TicketOut])
async def list_tickets(order_id: uuid.UUID, user: CurrentUserDep, session: SessionDep) -> list[Ticket]:
    order = await booking.get_order(session, user, order_id)
    return list(
        await session.scalars(select(Ticket).where(Ticket.order_id == order.id).order_by(Ticket.seat_label))
    )
