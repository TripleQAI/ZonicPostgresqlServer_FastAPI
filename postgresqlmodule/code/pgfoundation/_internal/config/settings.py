"""L0 — typed, validated settings (doc 05). Invalid config fails fast at boot."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from ...core.models import PoolPolicy


class PoolSettings(BaseModel):
    min_size: int = Field(1, ge=0)
    max_size: int = Field(10, ge=1)
    acquire_timeout_seconds: float = Field(5.0, gt=0)
    max_idle_seconds: float = Field(300.0, gt=0)
    max_lifetime_seconds: float = Field(1800.0, gt=0)

    @field_validator("max_size")
    @classmethod
    def _max_ge_min(cls, v: int, info) -> int:
        min_size = info.data.get("min_size", 0)
        if v < min_size:
            raise ValueError("pool.max_size must be >= min_size")
        return v


class DataSourceSettings(BaseModel):
    name: str = Field(min_length=1)
    dsn: SecretStr  # resolved from env/secret manager; never logged
    role: Literal["primary", "replica", "standalone"] = "standalone"
    pool: PoolSettings = PoolSettings()
    read_only: bool = False
    statement_timeout_ms: int | None = None

    def to_pool_policy(self) -> PoolPolicy:
        return PoolPolicy(
            min_size=self.pool.min_size,
            max_size=self.pool.max_size,
            acquire_timeout_seconds=self.pool.acquire_timeout_seconds,
            max_idle_seconds=self.pool.max_idle_seconds,
            max_lifetime_seconds=self.pool.max_lifetime_seconds,
            statement_timeout_ms=self.statement_timeout_ms,
            read_only=self.read_only,
        )


class RetrySettings(BaseModel):
    max_attempts: int = Field(3, ge=1)
    base_backoff_ms: float = Field(20.0, gt=0)
    max_backoff_ms: float = Field(1000.0, gt=0)
    jitter: bool = True


class CircuitBreakerSettings(BaseModel):
    enabled: bool = True
    failure_threshold: int = Field(5, ge=1)
    reset_timeout_seconds: float = Field(30.0, gt=0)


class ResilienceSettings(BaseModel):
    retry: RetrySettings = RetrySettings()
    circuit_breaker: CircuitBreakerSettings = CircuitBreakerSettings()


class AuthSettings(BaseModel):
    # Auth is a pluggable seam, disabled by default (ADR-013).
    enabled: bool = False
    provider: str | None = None


class ObservabilitySettings(BaseModel):
    # Integrated from an external log project (ADR-014). Default no-op.
    provider: str = "none"
    metrics_enabled: bool = False
    tracing_enabled: bool = False


class AppSettings(BaseModel):
    datasources: list[DataSourceSettings]
    resilience: ResilienceSettings = ResilienceSettings()
    auth: AuthSettings = AuthSettings()
    observability: ObservabilitySettings = ObservabilitySettings()

    @field_validator("datasources")
    @classmethod
    def _names_unique_nonempty(
        cls, v: list[DataSourceSettings]
    ) -> list[DataSourceSettings]:
        if not v:
            raise ValueError("at least one data source must be configured")
        names = [ds.name for ds in v]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate data-source names: {sorted(dupes)}")
        return v


__all__ = [
    "PoolSettings",
    "DataSourceSettings",
    "RetrySettings",
    "CircuitBreakerSettings",
    "ResilienceSettings",
    "AuthSettings",
    "ObservabilitySettings",
    "AppSettings",
]
