"""Initial schema.

Statuses are VARCHAR + CHECK constraints rather than PostgreSQL ENUM types: adding a value to an ENUM
cannot run inside a transaction on older versions and removing one is painful, while a CHECK
constraint is replaced in one short migration.

Revision ID: 0001
Revises:
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _updated_at() -> sa.Column:
    return sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _created_at(),
        sa.CheckConstraint("role IN ('customer', 'organizer', 'admin')", name="role_valid"),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organizer_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("venue", sa.String(200), nullable=False),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sale_starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("max_tickets_per_user", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint("status IN ('draft', 'published')", name="status_valid"),
        sa.CheckConstraint("sale_starts_at < starts_at", name="sale_before_show"),
        sa.CheckConstraint("max_tickets_per_user BETWEEN 1 AND 10", name="max_tickets_per_user_range"),
    )
    op.create_index("ix_events_organizer_id", "events", ["organizer_id"])

    op.create_table(
        "ticket_tiers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.UniqueConstraint("event_id", "name"),
        sa.CheckConstraint("price >= 0", name="price_non_negative"),
    )
    op.create_index("ix_ticket_tiers_event_id", "ticket_tiers", ["event_id"])

    op.create_table(
        "holds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("tier_id", sa.Uuid(), sa.ForeignKey("ticket_tiers.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.CheckConstraint("status IN ('active', 'converted', 'expired', 'released')", name="status_valid"),
        sa.CheckConstraint("quantity > 0", name="quantity_positive"),
    )
    op.create_index(
        "ix_holds_active_expires_at", "holds", ["expires_at"], postgresql_where=sa.text("status = 'active'")
    )
    op.create_index("ix_holds_user_event", "holds", ["user_id", "event_id"])

    op.create_table(
        "seats",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tier_id", sa.Uuid(), sa.ForeignKey("ticket_tiers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), server_default="available", nullable=False),
        sa.Column("hold_id", sa.Uuid(), sa.ForeignKey("holds.id"), nullable=True),
        _updated_at(),
        sa.UniqueConstraint("event_id", "label"),
        sa.CheckConstraint("status IN ('available', 'held', 'sold')", name="status_valid"),
    )
    # Partial index: only sellable seats are indexed, so it shrinks as the sale goes on and the
    # "next free seat" lookup stays an index range scan.
    op.create_index(
        "ix_seats_tier_available",
        "seats",
        ["tier_id", "id"],
        postgresql_where=sa.text("status = 'available'"),
    )
    op.create_index("ix_seats_hold_id", "seats", ["hold_id"], postgresql_where=sa.text("hold_id IS NOT NULL"))

    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("hold_id", sa.Uuid(), sa.ForeignKey("holds.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("payment_intent_id", sa.String(64), nullable=True, unique=True),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "status IN ('pending_payment', 'paid', 'failed', 'cancelled', 'expired', 'refund_pending')",
            name="status_valid",
        ),
    )
    op.create_index("ix_orders_user_created", "orders", ["user_id", "created_at", "id"])
    op.create_index(
        "ix_orders_pending_expires_at",
        "orders",
        ["expires_at"],
        postgresql_where=sa.text("status = 'pending_payment'"),
    )
    op.create_index("ix_orders_user_event", "orders", ["user_id", "event_id"])

    op.create_table(
        "tickets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("order_id", sa.Uuid(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("seat_id", sa.BigInteger(), sa.ForeignKey("seats.id"), nullable=False, unique=True),
        sa.Column("seat_label", sa.String(20), nullable=False),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        _created_at(),
    )
    op.create_index("ix_tickets_order_id", "tickets", ["order_id"])

    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        _created_at(),
    )
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("trace_context", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        _created_at(),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_outbox_unpublished", "outbox", ["id"], postgresql_where=sa.text("published_at IS NULL")
    )

    op.create_table(
        "processed_messages",
        sa.Column("consumer", sa.String(50), primary_key=True),
        sa.Column("message_id", sa.String(100), primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_processed_messages_processed_at", "processed_messages", ["processed_at"])

    op.create_table(
        "event_sales_stats",
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tickets_sold", sa.Integer(), server_default="0", nullable=False),
        sa.Column("orders_paid", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revenue", sa.BigInteger(), server_default="0", nullable=False),
        _updated_at(),
    )


def downgrade() -> None:
    for table in (
        "event_sales_stats",
        "processed_messages",
        "outbox",
        "idempotency_keys",
        "tickets",
        "orders",
        "seats",
        "holds",
        "ticket_tiers",
        "events",
        "refresh_tokens",
        "users",
    ):
        op.drop_table(table)
