import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import Conflict, Unauthorized
from app.models import RefreshToken, Role, User
from app.schemas import TokenPair
from app.security import create_access_token, hash_password, hash_token, new_refresh_token, verify_password

log = structlog.get_logger(__name__)


async def register(session: AsyncSession, *, email: str, password: str, full_name: str) -> User:
    # argon2 is CPU-heavy (~50 ms) but releases the GIL, so a thread keeps the event loop responsive.
    password_hash = await asyncio.to_thread(hash_password, password)
    user = User(email=email.lower(), password_hash=password_hash, full_name=full_name, role=Role.CUSTOMER)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("Email is already registered", code="email_taken") from exc
    return user


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User:
    user = await session.scalar(select(User).where(User.email == email.lower()))
    password_ok = await asyncio.to_thread(verify_password, user.password_hash if user else None, password)
    if user is None or not password_ok or not user.is_active:
        raise Unauthorized("Invalid email or password", code="invalid_credentials")
    return user


def issue_tokens(
    session: AsyncSession, user: User, settings: Settings, *, family_id: uuid.UUID | None = None
) -> TokenPair:
    raw, token_hash = new_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            family_id=family_id or uuid.uuid4(),
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    return TokenPair(
        access_token=create_access_token(user_id=user.id, role=user.role, settings=settings),
        refresh_token=raw,
        expires_in=settings.access_token_ttl_seconds,
    )


async def rotate_refresh_token(session: AsyncSession, raw_token: str, settings: Settings) -> TokenPair | str:
    """Exchange a refresh token for a new pair. Returns an error message instead of raising, because a
    detected reuse must still commit the family revocation."""
    token = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)).with_for_update()
    )
    if token is None or token.expires_at <= datetime.now(UTC):
        return "Invalid or expired refresh token"

    if token.revoked_at is not None:
        # A rotated token came back: someone else holds a copy. Kill every token of this login.
        await revoke_family(session, token.family_id)
        log.warning(
            "refresh_token_reuse_detected", user_id=str(token.user_id), family_id=str(token.family_id)
        )
        return "Refresh token was already used; please sign in again"

    user = await session.get(User, token.user_id)
    if user is None or not user.is_active:
        return "Account is disabled"
    token.revoked_at = datetime.now(UTC)
    return issue_tokens(session, user, settings, family_id=token.family_id)


async def revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )


async def find_refresh_token(session: AsyncSession, raw_token: str) -> RefreshToken | None:
    return await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)))
