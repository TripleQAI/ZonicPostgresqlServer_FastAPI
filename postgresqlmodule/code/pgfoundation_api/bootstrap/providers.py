"""L4 — the composition root's plug points (ADR-014, doc 11 §11.11).

The one place that knows which concrete observability and auth implementations
this process uses. Everything else depends on the ports.
"""

from __future__ import annotations

from pgfoundation._internal.observability.noop import NoopMetrics

from ..observability.metrics import MetricsCollector
from ..settings import ObservabilitySettings


def build_metrics(settings: ObservabilitySettings):
    """Return the ``MetricsPort`` for this process.

    Default is the Null Object, so library mode and tests pay nothing — the
    zero-cost default ADR-014 depends on. ``provider: internal`` binds the
    in-process collector that ``/metrics`` renders; any other named provider is
    an external adapter registered by the observability project.
    """
    if not settings.metrics_enabled:
        return NoopMetrics()
    return MetricsCollector()


__all__ = ["build_metrics"]
