"""Operational commands: `python -m app.cli <command>`."""

import argparse
import asyncio
import json
import sys
import uuid

from sqlalchemy import select

from app.config import get_settings
from app.db import create_engine, create_sessionmaker
from app.models import Role, User
from app.security import create_access_token, hash_password


async def bootstrap_admin() -> None:
    """Create the first admin from BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD (idempotent)."""
    settings = get_settings()
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        print("bootstrap-admin: BOOTSTRAP_ADMIN_EMAIL/PASSWORD not set, skipping")
        return
    engine = create_engine(settings)
    async with create_sessionmaker(engine)() as session:
        email = settings.bootstrap_admin_email.lower()
        if await session.scalar(select(User).where(User.email == email)) is None:
            session.add(
                User(
                    email=email,
                    password_hash=hash_password(settings.bootstrap_admin_password),
                    full_name="SanVe Admin",
                    role=Role.ADMIN,
                )
            )
            await session.commit()
            print(f"bootstrap-admin: created {email}")
        else:
            print(f"bootstrap-admin: {email} already exists")
    await engine.dispose()


async def seed_buyers(count: int) -> None:
    """Create buyer accounts for load tests and print one access token per line (local only)."""
    settings = get_settings()
    if settings.env == "production":
        sys.exit("seed-buyers is disabled in production")
    engine = create_engine(settings)
    shared_hash = hash_password(uuid.uuid4().hex)  # nobody needs to log in with these accounts
    run_id = uuid.uuid4().hex[:6]
    users = [
        User(email=f"buyer-{run_id}-{i}@loadtest.local", password_hash=shared_hash, full_name=f"Buyer {i}")
        for i in range(count)
    ]
    async with create_sessionmaker(engine)() as session:
        session.add_all(users)
        await session.commit()
    for user in users:
        print(
            json.dumps(
                {
                    "email": user.email,
                    "token": create_access_token(user_id=user.id, role=user.role, settings=settings),
                }
            )
        )
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap-admin")
    seed = commands.add_parser("seed-buyers")
    seed.add_argument("--count", type=int, default=100)
    args = parser.parse_args()

    if args.command == "bootstrap-admin":
        asyncio.run(bootstrap_admin())
    else:
        asyncio.run(seed_buyers(args.count))


if __name__ == "__main__":
    main()
