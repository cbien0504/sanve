"""Vacuum the hot booking tables much earlier than PostgreSQL's default.

Every hold/sale moves seats out of the partial index ix_seats_tier_available, leaving dead index
entries behind. Measured on 10,000 seats right after 6,000 were sold: finding two free seats read
129 buffers before VACUUM and 8 after (docs/database.md). The default thresholds (vacuum once 20%
of the table changed) let that bloat grow for the whole sale, so trigger it at 2% instead.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("seats", "holds", "outbox")
_SETTINGS = "autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02"


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} SET ({_SETTINGS})")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor)"
        )
