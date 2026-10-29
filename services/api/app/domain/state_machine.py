from enum import StrEnum

from app.models import HoldStatus, OrderStatus


class InvalidTransition(Exception):
    pass


class StateMachine[S: StrEnum]:
    """Whitelist of allowed status changes; anything else is a bug or a race and must not be persisted."""

    def __init__(self, name: str, transitions: dict[S, set[S]]) -> None:
        self.name = name
        self._transitions = transitions

    def can(self, current: S | str, target: S) -> bool:
        return target in self._transitions.get(type(target)(current), set())

    def ensure(self, current: S | str, target: S) -> None:
        if not self.can(current, target):
            raise InvalidTransition(f"{self.name}: {current} -> {target} is not allowed")


HOLD_FLOW = StateMachine[HoldStatus](
    "hold",
    {
        HoldStatus.ACTIVE: {HoldStatus.CONVERTED, HoldStatus.EXPIRED, HoldStatus.RELEASED},
    },
)

ORDER_FLOW = StateMachine[OrderStatus](
    "order",
    {
        OrderStatus.PENDING_PAYMENT: {
            OrderStatus.PAID,
            OrderStatus.FAILED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        },
        # The money arrived after we gave the seats away: the customer must be refunded.
        OrderStatus.EXPIRED: {OrderStatus.REFUND_PENDING},
        OrderStatus.CANCELLED: {OrderStatus.REFUND_PENDING},
        OrderStatus.FAILED: {OrderStatus.REFUND_PENDING},
    },
)
