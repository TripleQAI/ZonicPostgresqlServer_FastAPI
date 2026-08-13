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
        # True while a single HALF_OPEN probe is in flight (P4.1).
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        # Lazily transition OPEN -> HALF_OPEN once the reset window elapses.
        if self._state is CircuitState.OPEN:
            if self._clock.now() - self._opened_at >= self._reset:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        """May a call proceed? Open circuit fast-fails until the reset window.

        **P4.1 — admit exactly one probe in HALF_OPEN.**

        The caller's shape is::

            if not breaker.allow(): raise
            conn = await pool.acquire()      # <-- suspends
            breaker.on_success()

        There is an ``await`` between the check and the result, so every
        coroutine that arrives while the first probe is in flight also saw
        ``HALF_OPEN`` and was also admitted. A database that has just been
        declared unhealthy would then be hit by the entire backlog at once —
        precisely the stampede the breaker exists to prevent.

        (The failure *counter* was never at risk: these methods contain no
        ``await``, so on a single event loop they run to completion without
        interleaving. Probe admission is the real defect, because it spans one.)
        """
        state = self.state
        if state is CircuitState.OPEN:
            return False
        if state is CircuitState.HALF_OPEN:
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
        return True

    def on_success(self) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._probe_in_flight = False

    def on_failure(self) -> None:
        self._probe_in_flight = False
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
