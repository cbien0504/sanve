"""Idempotency keys stored in the same transaction as the business write.

The key row is inserted first. A concurrent request with the same key blocks on the unique index
until the first transaction finishes:
- first one commits  -> the duplicate sees the stored response and replays it;
- first one rolls back (e.g. sold out) -> the duplicate claims the key and runs normally.
Because the key and the business rows commit atomically, a crash can never leave a key that says
"done" without the work, or the work without the key.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import LOCK_NOT_AVAILABLE, sqlstate
from app.errors import Conflict, PreconditionRequired, UnprocessableEntity
from app.models import IdempotencyKey

HEADER = "Idempotency-Key"
_VALID_KEY = re.compile(r"^[A-Za-z0-9_\-:.]{8,100}$")


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]


def fingerprint(endpoint: str, payload: dict[str, Any]) -> str:
    raw = json.dumps({"endpoint": endpoint, "payload": payload}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


async def claim(
    session: AsyncSession, *, user_id: uuid.UUID, key: str | None, request_fingerprint: str
) -> StoredResponse | None:
    """Claim the key inside the caller's transaction, or return the response stored for it."""
    if not key:
        raise PreconditionRequired(f"{HEADER} header is required for this endpoint")
    if not _VALID_KEY.match(key):
        raise UnprocessableEntity(f"{HEADER} must be 8-100 characters of [A-Za-z0-9_-:.]")

    stmt = (
        insert(IdempotencyKey)
        .values(user_id=user_id, key=key, fingerprint=request_fingerprint)
        .on_conflict_do_nothing(index_elements=[IdempotencyKey.user_id, IdempotencyKey.key])
        .returning(IdempotencyKey.key)
    )
    try:
        claimed = (await session.execute(stmt)).scalar_one_or_none()
    except DBAPIError as exc:
        if sqlstate(exc) == LOCK_NOT_AVAILABLE:
            raise Conflict(
                "A request with this Idempotency-Key is still being processed",
                code="idempotency_in_progress",
                headers={"Retry-After": "1"},
            ) from exc
        raise
    if claimed is not None:
        return None

    existing = await session.get(IdempotencyKey, (user_id, key))
    if existing is None or existing.status_code is None or existing.response_body is None:
        raise Conflict(
            "Idempotency-Key is in an unexpected state, please retry", code="idempotency_in_progress"
        )
    if existing.fingerprint != request_fingerprint:
        raise UnprocessableEntity(
            "Idempotency-Key was already used with a different request", code="idempotency_key_reused"
        )
    return StoredResponse(existing.status_code, existing.response_body)


async def store(
    session: AsyncSession, *, user_id: uuid.UUID, key: str, status_code: int, body: dict[str, Any]
) -> None:
    await session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
        .values(status_code=status_code, response_body=body)
    )
