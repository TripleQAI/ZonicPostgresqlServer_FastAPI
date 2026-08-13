"""No-op observability (Null Object). Zero cost when no external log project is

bound (doc 10 §10.1 / ADR-014). Real adapters bind these ports at the
composition root.
"""
from __future__ import annotations

import contextlib
from typing import Any


class NoopLogger:
    def log(self, level: str, event: str, **fields: Any) -> None:  # noqa: D401
        return None


class NoopMetrics:
    def incr(self, name: str, value: int = 1, **labels: str) -> None:
        return None

    def observe(self, name: str, value: float, **labels: str) -> None:
        return None

    def gauge(self, name: str, value: float, **labels: str) -> None:
        return None


class NoopTracer:
    @contextlib.contextmanager
    def span(self, name: str, **attrs: Any):
        yield None


__all__ = ["NoopLogger", "NoopMetrics", "NoopTracer"]
