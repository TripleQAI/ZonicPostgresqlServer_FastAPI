"""Injectable clock (ClockPort). Keeps backoff/timeout logic deterministic in tests."""
from __future__ import annotations

import asyncio
import time


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class ManualClock:
    """Test double — advances only when told; ``sleep`` records instead of waiting."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self._t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._t += seconds


__all__ = ["SystemClock", "ManualClock"]
