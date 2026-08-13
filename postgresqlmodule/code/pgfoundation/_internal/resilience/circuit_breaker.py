"""Per-data-source Circuit Breaker (doc 10 §10.3). Fails fast when a DB is down."""
from __future__ import annotations

from enum import Enum

from ..clock import SystemClock


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 30.0,
        clock=None,
    ) -> None:
        self._threshold = failure_threshold
        self._reset = reset_timeout_seconds
        self._clock = clock or SystemClock()
        self._failures = 0
        self._opened_at = 0.0
        self._state = CircuitState.CLOSED

    @property
    def state(self) -> CircuitState:
        # Lazily transition OPEN -> HALF_OPEN once the reset window elapses.
        if self._state is CircuitState.OPEN:
            if self._clock.now() - self._opened_at >= self._reset:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        """May a call proceed? Open circuit fast-fails until the reset window."""
        return self.state is not CircuitState.OPEN

    def on_success(self) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED

    def on_failure(self) -> None:
        # A failure in HALF_OPEN re-opens immediately.
        if self._state is CircuitState.HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock.now()


__all__ = ["CircuitBreaker", "CircuitState"]
