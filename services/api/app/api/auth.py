import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.security import OAuth2PasswordRequestForm

from app.api import API_V1
from app.api.deps import (
    AdminDep,
    CurrentUserDep,
    ResourcesDep,
    SessionDep,
    client_ip,
    enforce_rate_limit,
)
from app.core.rate_limit import RateLimit
from app.domain import auth as auth_service
from app.errors import NotFound, Unauthorized
from app.models import User
from app.schemas import RefreshIn, RegisterIn, RoleUpdateIn, TokenPair, UserOut

router = APIRouter(prefix=API_V1, tags=["auth"])

# Brute-force protection: a tight budget per account and a looser one per IP (NAT, offices...).
LOGIN_PER_EMAIL = RateLimit("login_email", capacity=5, per_seconds=60)
LOGIN_PER_IP = RateLimit("login_ip", capacity=30, per_seconds=60)
REGISTER_PER_IP = RateLimit("register_ip", capacity=10, per_seconds=3600)


@router.post("/auth/register", status_code=201, response_model=UserOut)
async def register(body: RegisterIn, request: Request, session: SessionDep, resources: ResourcesDep) -> User:
    await enforce_rate_limit(resources, REGISTER_PER_IP, client_ip(request))
    user = await auth_service.register(
        session, email=str(body.email), password=body.password, full_name=body.full_name
    )
    await session.commit()
    return user


@router.post("/auth/token", response_model=TokenPair)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    request: Request,
    session: SessionDep,
    resources: ResourcesDep,
) -> TokenPair:
    """OAuth2 password flow (the Authorize button in /docs uses it). `username` is the email."""
    await enforce_rate_limit(resources, LOGIN_PER_IP, client_ip(request))
    await enforce_rate_limit(resources, LOGIN_PER_EMAIL, form.username.lower())
    user = await auth_service.authenticate(session, email=form.username, password=form.password)
    tokens = auth_service.issue_tokens(session, user, resources.settings)
    await session.commit()
    return tokens


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(body: RefreshIn, session: SessionDep, resources: ResourcesDep) -> TokenPair:
    outcome = await auth_service.rotate_refresh_token(session, body.refresh_token, resources.settings)
    await session.commit()  # also persists a family revocation when token reuse was detected
    if isinstance(outcome, str):
        raise Unauthorized(outcome, code="invalid_refresh_token")
    return outcome


@router.post("/auth/logout", status_code=204)
async def logout(body: RefreshIn, user: CurrentUserDep, session: SessionDep, resources: ResourcesDep) -> None:
    token = await auth_service.find_refresh_token(session, body.refresh_token)
    if token is not None and token.user_id == user.id:
        await auth_service.revoke_family(session, token.family_id)
        await session.commit()
    # Access tokens are stateless JWTs: deny-list this one until it would have expired anyway.
    ttl = user.expires_at - int(time.time())
    if ttl > 0:
        await resources.redis.set(f"sanve:denylist:{user.jti}", "1", ex=ttl)


@router.get("/users/me", response_model=UserOut, tags=["users"])
async def me(user: CurrentUserDep, session: SessionDep) -> User:
    account = await session.get(User, user.id)
    if account is None:
        raise NotFound("User not found")
    return account


@router.patch("/admin/users/{user_id}/role", response_model=UserOut, tags=["admin"])
async def change_role(user_id: uuid.UUID, body: RoleUpdateIn, _: AdminDep, session: SessionDep) -> User:
    """Takes effect on the user's next token refresh (access tokens carry the role for ≤ 15 minutes)."""
    account = await session.get(User, user_id)
    if account is None:
        raise NotFound("User not found")
    account.role = body.role
    await session.commit()
    return account
