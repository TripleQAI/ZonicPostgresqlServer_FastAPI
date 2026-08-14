"""L4 — an in-process ``MetricsPort`` adapter with Prometheus rendering (P5.5).

ADR-014 says the foundation **emits** and an external project **collects**. That
stays true: this is a thin adapter behind the existing port, not a telemetry
pipeline. It exists so a deployment has something to scrape on day one, and so
the emission points can be tested for real rather than against a spy.

Binding a different backend — OTLP, a vendor agent — means registering a
different `MetricsPort` at the composition root and changing nothing else.

**Cardinality is enforced here, not hoped for** (doc 11 §11.9). A label value
outside the declared allow-list is folded into ``"other"`` rather than minted as
a new series, because one unbounded label can cost more than the service it
measures.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from typing import Any

#: Buckets in seconds. Chosen around the SLOs in doc 07 §7.9: sub-millisecond
#: pool acquires, single-digit-millisecond point reads, and a long tail for
#: streams and batches.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

#: Labels we accept, and the closed set of values each may take (doc 11 §11.9).
#: `datasource` and `route` are bounded by config and by the route table, so they
#: carry no explicit list; everything else is a small enum.
BOUNDED_LABELS: dict[str, frozenset[str] | None] = {
    "datasource": None,
    "route": None,
    "op": None,
    "method": frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}),
    "status": None,
    "version": None,
    "mode": frozenset({"parallel", "sequential", "transactional"}),
    "result": frozenset({"allow", "deny", "unauthenticated"}),
    "action": frozenset({"read", "write", "list"}),
    "scope": frozenset({"global", "datasource", "batch"}),
    "reason": frozenset({"complete", "client_disconnect", "error", "timeout"}),
    "type": None,
    "state": frozenset({"idle", "in_use", "total"}),
    "outcome": frozenset({"commit", "rollback"}),
}

#: Labels that must NEVER become a metric series (doc 11 §11.9). Passing one is a
#: programming error, not a runtime condition — it fails loudly in tests.
FORBIDDEN_LABELS = frozenset({"request_id", "item_id", "sql", "params", "subject", "object"})


def _sanitize(labels: dict[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in labels.items():
        if key in FORBIDDEN_LABELS:
            raise ValueError(
                f"{key!r} must not be a metric label — it is unbounded. Put it in a "
                f"log field or span attribute instead (doc 11 §11.9)."
            )
        allowed = BOUNDED_LABELS.get(key, ...)
        if allowed is ...:
            raise ValueError(f"unknown metric label {key!r}; declare it in BOUNDED_LABELS")
        text = str(value)
        clean[key] = text if allowed is None or text in allowed else "other"
    return clean


def _key(name: str, labels: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    return name, tuple(sorted(labels.items()))


class MetricsCollector:
    """A ``MetricsPort`` that keeps counts in memory and renders Prometheus text.

    Guarded by a lock: uvicorn may run the app under multiple threads for sync
    endpoints, and a torn read while rendering would be indistinguishable from a
    real metric change.
    """

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._buckets = buckets
        self._lock = threading.Lock()
        self._counters: dict[Any, float] = defaultdict(float)
        self._gauges: dict[Any, float] = {}
        self._hist_sum: dict[Any, float] = defaultdict(float)
        self._hist_count: dict[Any, int] = defaultdict(int)
        self._hist_buckets: dict[Any, list[int]] = {}

    # --- MetricsPort ------------------------------------------------------
    def incr(self, name: str, value: int = 1, **labels: str) -> None:
        with self._lock:
            self._counters[_key(name, _sanitize(labels))] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = _key(name, _sanitize(labels))
        with self._lock:
            self._hist_sum[key] += value
            self._hist_count[key] += 1
            counts = self._hist_buckets.setdefault(key, [0] * len(self._buckets))
            for i, edge in enumerate(self._buckets):
                if value <= edge:
                    counts[i] += 1

    def gauge(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[_key(name, _sanitize(labels))] = value

    # --- rendering --------------------------------------------------------
    def render(self) -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name}{_fmt(labels)} {value:g}")
            for (name, labels), value in sorted(self._gauges.items()):
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name}{_fmt(labels)} {value:g}")
            for key in sorted(self._hist_count):
                name, labels = key
                lines.append(f"# TYPE {name} histogram")
                counts = self._hist_buckets[key]
                for edge, count in zip(self._buckets, counts, strict=True):
                    lines.append(f"{name}_bucket{_fmt(labels, le=str(edge))} {count}")
                lines.append(f"{name}_bucket{_fmt(labels, le='+Inf')} {self._hist_count[key]}")
                lines.append(f"{name}_sum{_fmt(labels)} {self._hist_sum[key]:g}")
                lines.append(f"{name}_count{_fmt(labels)} {self._hist_count[key]}")
        return "\n".join(lines) + "\n"

    # --- test/introspection helpers --------------------------------------
    def counter_value(self, name: str, **labels: str) -> float:
        return self._counters.get(_key(name, _sanitize(labels)), 0.0)

    def histogram_count(self, name: str, **labels: str) -> int:
        return self._hist_count.get(_key(name, _sanitize(labels)), 0)

    def gauge_value(self, name: str, **labels: str) -> float:
        return self._gauges.get(_key(name, _sanitize(labels)), math.nan)

    def series_count(self) -> int:
        """Total distinct series — the number cardinality rules protect."""
        return len(self._counters) + len(self._gauges) + len(self._hist_count)


def _fmt(labels: tuple[tuple[str, str], ...], **extra: str) -> str:
    pairs = list(labels) + sorted(extra.items())
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in pairs)
    return "{" + inner + "}"


__all__ = ["BOUNDED_LABELS", "DEFAULT_BUCKETS", "FORBIDDEN_LABELS", "MetricsCollector"]
