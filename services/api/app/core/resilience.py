import random
import time
from collections.abc import Callable
from enum import StrEnum


def backoff_delay(attempt: int, *, base: float, cap: float = 5.0) -> float:
    """Exponential backoff with full jitter: spreads retries so clients do not retry in lockstep."""
    return random.uniform(0, min(cap, base * 2 ** (attempt - 1)))  # noqa: S311 - not security sensitive


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Stops calling a failing dependency for a while instead of stacking up timeouts.

    closed --(N consecutive failures)--> open --(reset_timeout)--> half_open --(success)--> closed
                                                                            \\--(failure)--> open
    State is per process; every API replica protects itself independently.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at = 0.0
        self.state = BreakerState.CLOSED

    def allow_request(self) -> bool:
        if self.state is BreakerState.OPEN:
            if self._clock() - self._opened_at < self.reset_timeout:
                return False
            self.state = BreakerState.HALF_OPEN
        return True

    def record_success(self) -> None:
        self._failures = 0
        self.state = BreakerState.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        if self.state is BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
            self.state = BreakerState.OPEN
            self._opened_at = self._clock()
