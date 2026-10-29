import httpx

from app.models import Role
from tests.integration.conftest import Account


async def register_and_login(client: httpx.AsyncClient, email: str = "An.Nguyen@Example.com") -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "very-secret-pass", "full_name": "Nguyễn An"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["email"] == email.lower()
    assert "password_hash" not in response.json()
    login = await client.post("/api/v1/auth/token", data={"username": email, "password": "very-secret-pass"})
    assert login.status_code == 200, login.text
    return login.json()


async def test_register_rejects_duplicate_email(client: httpx.AsyncClient) -> None:
    await register_and_login(client)
    duplicate = await client.post(
        "/api/v1/auth/register",
        json={"email": "an.nguyen@example.com", "password": "very-secret-pass", "full_name": "Copy"},
    )
    assert duplicate.status_code == 409
    assert duplicate.headers["content-type"] == "application/problem+json"
    assert duplicate.json()["code"] == "email_taken"


async def test_refresh_rotation_detects_token_reuse(client: httpx.AsyncClient) -> None:
    tokens = await register_and_login(client)

    rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200
    new_tokens = rotated.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    # The old token shows up again: somebody copied it. The whole token family is revoked...
    reused = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 401
    # ...including the token the legitimate client received a moment ago.
    after = await client.post("/api/v1/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]})
    assert after.status_code == 401


async def test_logout_revokes_the_access_token(client: httpx.AsyncClient) -> None:
    tokens = await register_and_login(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await client.get("/api/v1/users/me", headers=headers)).status_code == 200

    logout = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
    )
    assert logout.status_code == 204

    me = await client.get("/api/v1/users/me", headers=headers)
    assert me.status_code == 401
    assert me.json()["code"] == "token_revoked"
    refresh = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh.status_code == 401


async def test_login_is_rate_limited_per_account(client: httpx.AsyncClient) -> None:
    await register_and_login(client, "target@example.com")  # uses 1 of the 5 attempts
    for _ in range(4):
        wrong = await client.post(
            "/api/v1/auth/token", data={"username": "target@example.com", "password": "nope"}
        )
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "invalid_credentials"

    blocked = await client.post(
        "/api/v1/auth/token", data={"username": "target@example.com", "password": "nope"}
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


async def test_unknown_email_and_wrong_password_look_the_same(client: httpx.AsyncClient) -> None:
    await register_and_login(client, "real@example.com")
    unknown = await client.post(
        "/api/v1/auth/token", data={"username": "ghost@example.com", "password": "x" * 12}
    )
    wrong = await client.post(
        "/api/v1/auth/token", data={"username": "real@example.com", "password": "x" * 12}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_only_admins_change_roles(client: httpx.AsyncClient, make_user, customer: Account) -> None:
    admin = await make_user(Role.ADMIN)
    url = f"/api/v1/admin/users/{customer.id}/role"
    forbidden = await client.patch(url, json={"role": "admin"}, headers=customer.headers)
    assert forbidden.status_code == 403

    promoted = await client.patch(url, json={"role": "organizer"}, headers=admin.headers)
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "organizer"
