"""L4 — service-shell settings (doc 05 §5.3).

Two settings objects are parsed from the same document:

* ``AppSettings``     — datasources / resilience / observability, owned by the
  **core engine**, so the core stays usable as a library with no shell present.
* ``ServiceSettings`` — everything the **shell** owns: api, server, concurrency,
  batch, auth.

Precedence (doc 05 §5.3)::

    explicit argument  >  environment variable  >  config file  >  schema default

Environment overrides use a ``PGF_`` prefix and ``__`` for nesting, e.g.
``PGF_SERVER__PORT=8600``, ``PGF_BATCH__MAX_ITEMS=50``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pgfoundation import ConfigError

RowFormat = Literal["dict", "tuple"]


# --------------------------------------------------------------------------- #
# api
# --------------------------------------------------------------------------- #
class ApiVersionSettings(BaseModel):
    """One mounted API version (doc 09 §9.1b)."""

    name: str = "v1"
    enabled: bool = True
    deprecated: bool = False
    sunset_date: str | None = None  # ISO date; never a literal in code


class ApiSettings(BaseModel):
    title: str = "pgfoundation-service"
    version: str = "1.0.0"
    root_path: str = ""
    docs_enabled: bool = True
    enable_raw_sql: bool = False  # gate the trusted /query and /execute surface
    default_row_format: RowFormat = "dict"
    max_response_rows: int = Field(50_000, gt=0)
    request_timeout_ms: int = Field(15_000, gt=0)
    versions: list[ApiVersionSettings] = Field(default_factory=lambda: [ApiVersionSettings()])


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(8600, gt=0, le=65535)
    workers: int = Field(1, ge=1)
    log_level: str = "info"


# --------------------------------------------------------------------------- #
# concurrency + batch  (doc 06 — parsed now, enforced in P2)
# --------------------------------------------------------------------------- #
class ConcurrencySettings(BaseModel):
    max_inflight_total: int = Field(128, ge=1)
    max_inflight_per_datasource: int = Field(32, ge=1)
    admission_timeout_ms: int = Field(2_000, gt=0)


class BatchSettings(BaseModel):
    enabled: bool = True
    default_mode: Literal["parallel", "sequential", "transactional"] = "parallel"
    max_items: int = Field(100, ge=1)
    max_concurrency: int = Field(16, ge=1)
    item_timeout_ms: int = Field(10_000, gt=0)
    total_timeout_ms: int = Field(30_000, gt=0)
    fail_fast: bool = False

    @model_validator(mode="after")
    def _item_budget_within_total(self) -> BatchSettings:
        if self.item_timeout_ms > self.total_timeout_ms:
            raise ValueError(
                "batch.item_timeout_ms must be <= batch.total_timeout_ms — a per-item "
                "budget larger than the whole-batch budget is meaningless"
            )
        return self


# --------------------------------------------------------------------------- #
# auth  (ADR-008 — CALLER identity, not end-user identity)
# --------------------------------------------------------------------------- #
class AuthMtlsSettings(BaseModel):
    identity_header: str = "x-client-cert-subject"


class AuthTokenSettings(BaseModel):
    issuer: str | None = None
    jwks: str | None = None
    leeway_seconds: int = Field(30, ge=0)


class CallerPolicy(BaseModel):
    subject: str
    datasources: list[str] = Field(default_factory=list)
    actions: list[Literal["read", "write", "list"]] = Field(default_factory=list)


class AuthPolicySettings(BaseModel):
    default: Literal["deny", "allow"] = "deny"
    callers: list[CallerPolicy] = Field(default_factory=list)


class AuthSettings(BaseModel):
    enabled: bool = False
    provider: Literal["none", "mtls", "token"] | str = "none"
    mtls: AuthMtlsSettings = Field(default_factory=AuthMtlsSettings)
    token: AuthTokenSettings = Field(default_factory=AuthTokenSettings)
    policy: AuthPolicySettings = Field(default_factory=AuthPolicySettings)


# --------------------------------------------------------------------------- #
# observability  (doc 11 §11.10 — emission thresholds, no pipeline)
# --------------------------------------------------------------------------- #
class ObservabilitySettings(BaseModel):
    provider: str = "none"
    metrics_enabled: bool = False
    tracing_enabled: bool = False
    slow_query_ms: int = Field(250, gt=0)
    trace_sample_ratio: float = Field(0.05, ge=0.0, le=1.0)
    log_level: str = "info"
    redact_sql: bool = True
    application_name: bool = True


# --------------------------------------------------------------------------- #
# root
# --------------------------------------------------------------------------- #
class ServiceSettings(BaseSettings):
    """Shell-owned settings. The core's ``AppSettings`` is parsed separately."""

    model_config = SettingsConfigDict(
        env_prefix="PGF_",
        env_nested_delimiter="__",
        extra="ignore",  # the same document also carries core-owned keys
    )

    config_path: str | None = None  # PGF_CONFIG — the pgfoundation.yaml to load
    profile: str | None = None  # PGF_PROFILE — dev | staging | prod

    api: ApiSettings = Field(default_factory=ApiSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)
    batch: BatchSettings = Field(default_factory=BatchSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    # -- cross-field rules (doc 05 §5.5) ---------------------------------- #
    @model_validator(mode="after")
    def _batch_within_admission(self) -> ServiceSettings:
        if self.batch.max_concurrency > self.concurrency.max_inflight_total:
            raise ValueError(
                "batch.max_concurrency must be <= concurrency.max_inflight_total — a "
                "single batch must not be able to exceed the global admission cap"
            )
        return self

    @model_validator(mode="after")
    def _raw_sql_needs_auth(self) -> ServiceSettings:
        # Raw SQL on an unauthenticated surface is the one combination that must
        # never ship by accident (doc 05 §5.5, ADR-008 §8). Dev may opt in.
        if self.api.enable_raw_sql and not self.auth.enabled and self.profile != "dev":
            raise ValueError(
                "api.enable_raw_sql=true with auth.enabled=false is refused outside the "
                "dev profile — raw SQL must never be exposed on an unauthenticated surface"
            )
        return self

    @model_validator(mode="after")
    def _auth_policy_is_usable(self) -> ServiceSettings:
        if (
            self.auth.enabled
            and self.auth.policy.default == "deny"
            and not self.auth.policy.callers
        ):
            raise ValueError(
                "auth.enabled=true with policy.default=deny and no callers authorizes "
                "nobody — that is a misconfiguration, not a lockdown (ADR-008)"
            )
        return self


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Overlay wins; nested dicts merge rather than replace."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_service_settings(
    config_path: str | Path | None = None,
    *,
    profile: str | None = None,
    **overrides: Any,
) -> ServiceSettings:
    """Build :class:`ServiceSettings` from file + profile overlay + env + args.

    ``config_path`` and ``profile`` fall back to ``PGF_CONFIG`` / ``PGF_PROFILE``,
    which ``BaseSettings`` reads for us. Invalid configuration raises
    :class:`ConfigError` at boot — never at first request (doc 05 §5.1).
    """
    # Resolve path/profile from env first, so the file we read is itself
    # env-overridable before any of its contents are parsed.
    bootstrap = ServiceSettings()
    path = config_path or bootstrap.config_path
    prof = profile or bootstrap.profile

    document: dict[str, Any] = {}
    if path:
        base = Path(path)
        document = _read_yaml(base)
        if prof:
            overlay_path = base.parent / "profiles" / f"{prof}.yaml"
            if overlay_path.exists():
                document = _deep_merge(document, _read_yaml(overlay_path))
        document["config_path"] = str(base)

    # Set unconditionally: the profile governs the cross-field rules below, so
    # dropping it when no file is supplied would silently apply prod rules to a
    # dev-profile call.
    document["profile"] = prof

    document.update(overrides)
    try:
        return ServiceSettings(**document)
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError -> our ConfigError
        raise ConfigError(f"invalid service configuration: {exc}") from exc


__all__ = [
    "ApiSettings",
    "ApiVersionSettings",
    "AuthSettings",
    "AuthPolicySettings",
    "CallerPolicy",
    "BatchSettings",
    "ConcurrencySettings",
    "ObservabilitySettings",
    "ServerSettings",
    "ServiceSettings",
    "load_service_settings",
]
