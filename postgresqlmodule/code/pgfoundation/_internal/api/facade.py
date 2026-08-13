"""L3 — the ``DataFoundation`` Facade: the single public entry point (doc 11)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.models import (
    Command,
    DataSourceName,
    FunctionQuery,
    HealthStatus,
    IsolationLevel,
    ProcedureCall,
    Query,
    QuerySpec,
    ResultSet,
    RetryPolicy,
    ViewQuery,
)
from ...core.ports import ClockPort, DriverPort, MetricsPort
from ..access.transactions import TxnBody
from ..access.uow import UnitOfWork
from ..clock import SystemClock
from ..config.loader import load_settings
from ..config.settings import AppSettings
from ..connection.registry import ConnectionRegistry, PoolFactory
from ..observability.noop import NoopMetrics
from .handle import DataSourceHandle


class DataFoundation:
    """Composition entry point; exposes the reusable basic APIs (doc 11 §11.4a)."""

    def __init__(
        self, registry: ConnectionRegistry, clock: ClockPort | None = None
    ) -> None:
        self._registry = registry
        self._clock = clock or SystemClock()

    # --- Bootstrapping (composition root) ---
    @classmethod
    def build(
        cls,
        settings: AppSettings,
        *,
        driver: DriverPort | None = None,
        clock: ClockPort | None = None,
        metrics: MetricsPort | None = None,
    ) -> "DataFoundation":
        """Wire the object graph. ``driver``/``clock``/``metrics`` injectable.

        ``metrics`` defaults to a no-op; an external log project binds a real
        adapter here (ADR-014). The circuit breaker + metering decorators are
        applied by the pool factory per ``settings.resilience``.
        """
        if driver is None:
            from ..drivers.psycopg_driver import PsycopgDriver

            driver = PsycopgDriver()
        clock = clock or SystemClock()
        metrics = metrics or NoopMetrics()
        factory = PoolFactory(
            driver, resilience=settings.resilience, metrics=metrics, clock=clock
        )
        registry = ConnectionRegistry(factory)
        for ds in settings.datasources:
            registry.register(ds)
        return cls(registry, clock)

    @classmethod
    async def from_settings(
        cls,
        settings: AppSettings,
        *,
        driver: DriverPort | None = None,
        clock: ClockPort | None = None,
        metrics: MetricsPort | None = None,
    ) -> "DataFoundation":
        foundation = cls.build(settings, driver=driver, clock=clock, metrics=metrics)
        await foundation.start()
        return foundation

    @classmethod
    async def from_config(
        cls,
        source: str | Path | dict[str, Any],
        *,
        driver: DriverPort | None = None,
        clock: ClockPort | None = None,
        metrics: MetricsPort | None = None,
    ) -> "DataFoundation":
        return await cls.from_settings(
            load_settings(source), driver=driver, clock=clock, metrics=metrics
        )

    # --- Resolve / list data sources ---
    def datasource(self, name: DataSourceName) -> DataSourceHandle:
        return DataSourceHandle(name, self._registry.get(name), self._clock)

    def names(self) -> list[DataSourceName]:
        return self._registry.names()

    # --- Convenience passthroughs (name-addressed) ---
    async def query(self, name: DataSourceName, spec: Query | QuerySpec) -> ResultSet:
        return await self.datasource(name).query(spec)

    async def execute(self, name: DataSourceName, cmd: Command | QuerySpec) -> int:
        return await self.datasource(name).execute(cmd)

    # --- Governed object invocation (name-based, no raw SQL) ---
    async def view(self, name: DataSourceName, spec: ViewQuery) -> ResultSet:
        return await self.datasource(name).view(spec)

    async def function(self, name: DataSourceName, spec: FunctionQuery) -> ResultSet:
        return await self.datasource(name).function(spec)

    async def procedure(self, name: DataSourceName, spec: ProcedureCall) -> ResultSet:
        return await self.datasource(name).procedure(spec)

    def transaction(
        self, name: DataSourceName, *, isolation: IsolationLevel | None = None
    ) -> UnitOfWork:
        return self.datasource(name).transaction(isolation=isolation)

    async def run_transaction(
        self,
        name: DataSourceName,
        fn: TxnBody,
        *,
        isolation: IsolationLevel | None = None,
        retry: RetryPolicy | None = None,
    ) -> Any:
        return await self.datasource(name).run_transaction(
            fn, isolation=isolation, retry=retry
        )

    # --- Lifecycle / ops ---
    async def health(self) -> dict[DataSourceName, HealthStatus]:
        return await self._registry.health()

    async def start(self) -> None:
        await self._registry.start()

    async def aclose(self) -> None:
        await self._registry.aclose()

    async def __aenter__(self) -> "DataFoundation":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = ["DataFoundation", "DataSourceHandle"]
