import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.pagination import decode_cursor, encode_cursor
from app.core.resilience import BreakerState, CircuitBreaker, backoff_delay
from app.domain.state_machine import HOLD_FLOW, ORDER_FLOW, InvalidTransition
from app.errors import UnprocessableEntity
from app.models import HoldStatus, OrderStatus
from app.schemas import EventCreate


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.PENDING_PAYMENT, OrderStatus.PAID),
        (OrderStatus.PENDING_PAYMENT, OrderStatus.EXPIRED),
        (OrderStatus.EXPIRED, OrderStatus.REFUND_PENDING),
    ],
)
def test_allowed_order_transitions(current: OrderStatus, target: OrderStatus) -> None:
    ORDER_FLOW.ensure(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.PAID, OrderStatus.CANCELLED),
        (OrderStatus.PAID, OrderStatus.PENDING_PAYMENT),
        (OrderStatus.EXPIRED, OrderStatus.PAID),
        (OrderStatus.REFUND_PENDING, OrderStatus.PAID),
    ],
)
def test_forbidden_order_transitions(current: OrderStatus, target: OrderStatus) -> None:
    with pytest.raises(InvalidTransition):
        ORDER_FLOW.ensure(current, target)


def test_hold_can_only_leave_active() -> None:
    assert HOLD_FLOW.can("active", HoldStatus.CONVERTED)
    assert not HOLD_FLOW.can("expired", HoldStatus.CONVERTED)


def test_cursor_roundtrip_and_tampering() -> None:
    moment, row_id = datetime(2026, 12, 31, 20, 0, tzinfo=UTC), uuid.uuid4()
    assert decode_cursor(encode_cursor(moment, row_id)) == (moment, row_id)
    with pytest.raises(UnprocessableEntity):
        decode_cursor("not-a-cursor")


def test_circuit_breaker_opens_then_recovers() -> None:
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=30, clock=lambda: now[0])
    breaker.record_failure()
    assert breaker.allow_request()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allow_request()

    now[0] = 31.0
    assert breaker.allow_request()  # one trial call goes through
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN  # trial failed: open again immediately

    now[0] = 62.0
    assert breaker.allow_request()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_backoff_is_jittered_and_capped() -> None:
    delays = [backoff_delay(attempt, base=0.2, cap=1.0) for attempt in range(1, 20) for _ in range(20)]
    assert all(0 <= delay <= 1.0 for delay in delays)
    assert len(set(delays)) > 1


def _event(**overrides: object) -> dict:
    now = datetime.now(UTC)
    payload = {
        "title": "Đêm nhạc mùa thu",
        "venue": "Nhà hát Lớn",
        "city": "Hà Nội",
        "starts_at": now + timedelta(days=10),
        "sale_starts_at": now,
        "tiers": [{"name": "VIP", "price": 2_000_000, "rows": ["A"], "seats_per_row": 10}],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "overrides",
    [
        {"sale_starts_at": datetime.now(UTC) + timedelta(days=11)},
        {"starts_at": datetime.now(UTC) - timedelta(days=1)},
        {
            "tiers": [
                {"name": "VIP", "price": 1, "rows": ["A"], "seats_per_row": 5},
                {"name": "GA", "price": 1, "rows": ["A"], "seats_per_row": 5},
            ]
        },
        {"tiers": [{"name": "VIP", "price": 1, "rows": ["a"], "seats_per_row": 5}]},
        {"starts_at": "2030-01-01T20:00:00"},
    ],
    ids=["sale-after-show", "show-in-past", "row-in-two-tiers", "lowercase-row", "naive-datetime"],
)
def test_event_validation_rejects_bad_input(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        EventCreate.model_validate(_event(**overrides))
