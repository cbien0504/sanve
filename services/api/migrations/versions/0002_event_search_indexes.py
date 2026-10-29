"""Indexes for the public event listing and title search, built without locking the table.

CREATE INDEX CONCURRENTLY does not block writes, which matters once `events` is busy in
production, but it cannot run inside a transaction: hence the autocommit block. If it fails half
way it leaves an INVALID index behind, so the statements are idempotent (IF [NOT] EXISTS).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PUBLISHED = sa.text("status = 'published'")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_events_published_starts_at",
            "events",
            ["starts_at", "id"],
            postgresql_where=_PUBLISHED,
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.create_index(
            "ix_events_published_city",
            "events",
            ["city", "starts_at", "id"],
            postgresql_where=_PUBLISHED,
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        # Trigram GIN index: makes `title ILIKE '%keyword%'` an index lookup instead of a full scan.
        op.create_index(
            "ix_events_title_trgm",
            "events",
            ["title"],
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in ("ix_events_title_trgm", "ix_events_published_city", "ix_events_published_starts_at"):
            op.drop_index(name, table_name="events", postgresql_concurrently=True, if_exists=True)
