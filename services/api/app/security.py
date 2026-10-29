import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import Settings
from app.models import Role

_hasher = PasswordHasher()  # argon2id with the library's RFC 9106 defaults

# Verified against when the email does not exist, so login time does not reveal registered emails.
_DUMMY_HASH = _hasher.hash("timing-attack-dummy-password")

_ALGORITHM = "HS256"


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    role: Role
    jti: str
    expires_at: int


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(*, user_id: uuid.UUID, role: str, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "jti": uuid.uuid4().hex,
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> CurrentUser:
    """Raises jwt.PyJWTError (or ValueError for unknown roles) when the token is not acceptable."""
    claims: dict[str, Any] = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[_ALGORITHM],  # pinned: never let the token choose its own algorithm
        issuer=settings.jwt_issuer,
        options={"require": ["sub", "role", "jti", "exp", "iss"]},
    )
    return CurrentUser(
        id=uuid.UUID(claims["sub"]), role=Role(claims["role"]), jti=claims["jti"], expires_at=claims["exp"]
    )


def new_refresh_token() -> tuple[str, str]:
    """Refresh tokens are opaque random strings; only their SHA-256 is stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
