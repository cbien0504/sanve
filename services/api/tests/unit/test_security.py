import uuid

import jwt
import pytest

from app.models import Role
from app.security import create_access_token, decode_access_token, hash_password, verify_password
from tests.settings import make_settings

settings = make_settings()


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery staple")
    assert not verify_password(hashed, "wrong password")
    assert not verify_password(None, "anything")  # unknown user: still pays the hashing cost


def test_access_token_roundtrip() -> None:
    user_id = uuid.uuid4()
    user = decode_access_token(
        create_access_token(user_id=user_id, role="organizer", settings=settings), settings
    )
    assert (user.id, user.role) == (user_id, Role.ORGANIZER)


def test_token_signed_with_another_secret_is_rejected() -> None:
    other = make_settings(jwt_secret="another-secret-0123456789abcdef0123456789")
    token = create_access_token(user_id=uuid.uuid4(), role="admin", settings=other)
    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(token, settings)


def test_expired_token_is_rejected() -> None:
    expired = make_settings(access_token_ttl_seconds=-5)
    token = create_access_token(user_id=uuid.uuid4(), role="customer", settings=expired)
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token, settings)


def test_unsigned_alg_none_token_is_rejected() -> None:
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "role": "admin", "jti": "x", "iss": "sanve", "exp": 4102444800},
        key=None,
        algorithm="none",
    )
    with pytest.raises(jwt.InvalidAlgorithmError):
        decode_access_token(forged, settings)


def test_production_refuses_weak_secrets() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET"):
        make_settings(env="production", jwt_secret="dev-jwt-secret-change-me")
