import uuid
from typing import Annotated

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from app.api import API_V1
from app.api.deps import CurrentUserDep, ResourcesDep, SessionDep, rate_limit_per_user
from app.core import idempotency
from app.core.rate_limit import RateLimit
from app.domain import booking
from app.schemas import HoldCreate, HoldOut

router = APIRouter(prefix=f"{API_V1}/holds", tags=["holds"])

HOLDS_PER_USER = RateLimit("holds_user", capacity=10, per_seconds=60)


@router.post(
    "",
    status_code=201,
    response_model=HoldOut,
    dependencies=[rate_limit_per_user(HOLDS_PER_USER)],
    responses={409: {"description": "Sold out, sale not open or per-buyer limit reached"}},
)
async def create_hold(
    body: HoldCreate,
    user: CurrentUserDep,
    session: SessionDep,
    resources: ResourcesDep,
    idempotency_key: Annotated[
        str, Header(alias=idempotency.HEADER, description="Unique per attempt, reused on retries")
    ],
) -> JSONResponse:
    """Reserve seats for `HOLD_TTL_SECONDS`. Retrying with the same Idempotency-Key never reserves twice."""
    stored = await idempotency.claim(
        session,
        user_id=user.id,
        key=idempotency_key,
        request_fingerprint=idempotency.fingerprint("POST /holds", body.model_dump(mode="json")),
    )
    if stored is not None:
        return JSONResponse(
            stored.body, status_code=stored.status_code, headers={"Idempotent-Replayed": "true"}
        )

    hold = await booking.create_hold(session, user, body, resources.settings)
    payload = hold.model_dump(mode="json")
    await idempotency.store(session, user_id=user.id, key=idempotency_key, status_code=201, body=payload)
    await session.commit()
    return JSONResponse(payload, status_code=201, headers={"Location": f"{API_V1}/holds/{hold.id}"})


@router.get("/{hold_id}", response_model=HoldOut)
async def get_hold(hold_id: uuid.UUID, user: CurrentUserDep, session: SessionDep) -> HoldOut:
    return await booking.get_hold(session, user, hold_id)


@router.delete("/{hold_id}", status_code=204)
async def release_hold(hold_id: uuid.UUID, user: CurrentUserDep, session: SessionDep) -> None:
    await booking.release_hold(session, user, hold_id)
    await session.commit()
