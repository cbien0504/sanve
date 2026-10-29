"""Small helpers shared by demo.py and flash_sale.py (they run on the host with `uv run`)."""

import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
API_URL = os.environ.get("SANVE_URL", "http://localhost:8080")


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return {**values, **os.environ}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: httpx.Client, email: str, password: str) -> str:
    response = client.post("/api/v1/auth/token", data={"username": email, "password": password})
    response.raise_for_status()
    return response.json()["access_token"]


def admin_token(client: httpx.Client) -> str:
    env = load_env()
    return login(client, env["BOOTSTRAP_ADMIN_EMAIL"], env["BOOTSTRAP_ADMIN_PASSWORD"])


def create_published_event(client: httpx.Client, token: str, title: str, tiers: list[dict[str, Any]]) -> dict:
    now = datetime.now(UTC)
    response = client.post(
        "/api/v1/events",
        json={
            "title": title,
            "description": "Sự kiện tạo bởi script demo",
            "venue": "Sân vận động Quốc gia Mỹ Đình",
            "city": "Hà Nội",
            "starts_at": (now + timedelta(days=30)).isoformat(),
            "sale_starts_at": (now - timedelta(minutes=1)).isoformat(),
            "max_tickets_per_user": 4,
            "tiers": tiers,
        },
        headers=bearer(token),
    )
    response.raise_for_status()
    event = response.json()
    published = client.post(f"/api/v1/events/{event['id']}/publish", headers=bearer(token))
    published.raise_for_status()
    return published.json()


def post_with_retry(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    """Retry 503s the way a well-behaved client should: honour Retry-After."""
    for _ in range(5):
        response = client.post(url, **kwargs)
        if response.status_code != 503:
            return response
        time.sleep(float(response.headers.get("Retry-After", "1")))
    return response


def new_key() -> str:
    return uuid.uuid4().hex
