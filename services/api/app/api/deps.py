import math
from collections.abc import AsyncIterator
from typing import Annotated

import jwt
import structlog
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import API_V1
from app.config import Settings
from app.core.rate_limit import RateLimit
from app.errors import Forbidden, TooManyRequests, Unauthorized
from app.models import Role
from app.observability.metrics import RATE_LIMITED
from app.resources import Resources
from app.security import CurrentUser, decode_access_token

log = structlog.get_logger(__name__)

_oauth2 = OAuth2PasswordBearer(tokenUrl=f"{API_V1}/auth/token", auto_error=False)


def get_resources(request: Request) -> Resources:
    return request.app.state.resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


def get_settings(resources: ResourcesDep) -> Settings:
    return resources.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_session(resources: ResourcesDep) -> AsyncIterator[AsyncSession]:
    """One unit of work per request: routes commit explicitly, anything uncommitted is rolled back."""
    async with resources.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_optional_user(
    resources: ResourcesDep, token: Annotated[str | None, Depends(_oauth2)]
) -> CurrentUser | None:
    if not token:
        return None
    try:
        user = decode_access_token(token, resources.settings)
    except (jwt.PyJWTError, ValueError) as exc:
        raise Unauthorized("Invalid or expired access token", code="invalid_token") from exc
    try:
        revoked = await resources.redis.exists(f"sanve:denylist:{user.jti}")
    except RedisError:
        # Tokens live 15 minutes; we accept that a logged-out token keeps working while Redis is down.
        log.warning("denylist_unavailable")
        revoked = 0
    if revoked:
        raise Unauthorized("Access token has been revoked", code="token_revoked")
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    return user


async def get_current_user(user: Annotated[CurrentUser | None, Depends(get_optional_user)]) -> CurrentUser:
    if user is None:
        raise Unauthorized("Not authenticated")
    return user


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
OptionalUserDep = Annotated[CurrentUser | None, Depends(get_optional_user)]


def require_roles(*roles: Role):
    async def dependency(user: CurrentUserDep) -> CurrentUser:
        if user.role not in roles:
            raise Forbidden("You do not have permission to perform this action")
        return user

    return dependency


OrganizerDep = Annotated[CurrentUser, Depends(require_roles(Role.ORGANIZER, Role.ADMIN))]
AdminDep = Annotated[CurrentUser, Depends(require_roles(Role.ADMIN))]


async def enforce_rate_limit(resources: Resources, limit: RateLimit, identity: str) -> None:
    result = await resources.rate_limiter.hit(limit, identity)
    if not result.allowed:
        RATE_LIMITED.labels(limit.name).inc()
        raise TooManyRequests(
            "Too many requests, slow down",
            headers={
                "Retry-After": str(max(1, math.ceil(result.retry_after))),
                "RateLimit-Limit": str(result.limit),
                "RateLimit-Remaining": "0",
            },
        )


def rate_limit_per_user(limit: RateLimit):
    async def dependency(resources: ResourcesDep, user: CurrentUserDep) -> None:
        await enforce_rate_limit(resources, limit, str(user.id))

    return Depends(dependency)


def client_ip(request: Request) -> str:
    # Behind nginx, uvicorn --proxy-headers already resolved X-Forwarded-For into request.client.
    return request.client.host if request.client else "unknown"
