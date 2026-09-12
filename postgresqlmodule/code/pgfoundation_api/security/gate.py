"""L4 — ``AuthGate`` and the ``authorize()`` dependency (ADR-005, ADR-008).

In the Django shell every view opened with ``await get_auth_gate().check(...)`` —
a line that could be forgotten. Here the check is a **dependency supplied by the
descriptor**, so an endpoint cannot be registered without it (ADR-003).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, Path, Request

from pgfoundation._internal.observability.noop import NoopMetrics

from ..settings import AuthSettings
from .policy import PolicyAuthorizer
from .ports import (
    Action,
    AllowAllAuthenticator,
    AllowAllAuthorizer,
    AuthenticationError,
    AuthenticatorPort,
    AuthorizationError,
    AuthorizerPort,
    Principal,
)
from .providers import MeshIdentityAuthenticator, SharedSecretAuthenticator


class AuthGate:
    """Runs AuthN then AuthZ when enabled; a pass-through when disabled.

    Fails closed: enabling auth without a registered provider refuses to start,
    rather than silently running open.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        authenticator: AuthenticatorPort | None = None,
        authorizer: AuthorizerPort | None = None,
        metrics: Any = None,
    ) -> None:
        self.enabled = enabled
        self._metrics = metrics or NoopMetrics()
        if enabled and (authenticator is None or authorizer is None):
            raise RuntimeError(
                "auth.enabled=true but no auth provider registered — refusing to start"
            )
        self._authn: AuthenticatorPort = authenticator or AllowAllAuthenticator()
        self._authz: AuthorizerPort = authorizer or AllowAllAuthorizer()

    async def check(self, request: Any, action: str, resource: str) -> Principal:
        if not self.enabled:
            return Principal(subject="anonymous")

        principal = await self._authn.authenticate(request)
        if principal is None:
            self._metrics.incr("pgf_auth_checks_total", 1, action=action, result="unauthenticated")
            raise AuthenticationError("authentication required")

        if not await self._authz.authorize(principal, action, resource):
            # `subject` is deliberately NOT a metric label — it is unbounded in
            # principle and belongs in the log event instead (doc 11 §11.9).
            # OBSERVE (LOG): auth.denied {action, resource, subject} — the
            #                subject, never the credential (doc 11 §11.8)
            self._metrics.incr("pgf_auth_checks_total", 1, action=action, result="deny")
            raise AuthorizationError(f"not permitted: {action} {resource}")

        self._metrics.incr("pgf_auth_checks_total", 1, action=action, result="allow")
        return principal


def build_auth_gate(
    settings: AuthSettings,
    *,
    api_keys: dict[str, str] | None = None,
    metrics: Any = None,
) -> AuthGate:
    """Composition-root helper: resolve providers from config (ADR-008 §3, P4.8).

    ``api_keys`` maps subject -> key and is supplied by the platform's secret
    manager, never by the config file — the file names *who* may call, the secret
    store holds *what proves it*.

    An unknown provider name fails closed rather than falling back to
    ``AllowAll``: a typo in a production profile must not silently open the
    service.
    """
    if not settings.enabled:
        return AuthGate(enabled=False, metrics=metrics)

    authenticator: AuthenticatorPort
    if settings.provider == "mtls":
        authenticator = MeshIdentityAuthenticator(settings.mtls)
    elif settings.provider == "token":
        # Defence in depth against the same mistake load_api_keys guards at the
        # composition root: an empty key map authenticates nobody, so every
        # request 401s and the cause looks like a broken credential rather than
        # a missing one. Checked here too because this is a public function --
        # a future caller could construct a gate without going through
        # bootstrap.providers.
        if not api_keys:
            raise RuntimeError(
                "auth.enabled=true with provider 'token' but no api_keys were "
                "supplied; every caller would be rejected -- refusing to start"
            )
        authenticator = SharedSecretAuthenticator(settings.token, api_keys)
    else:
        raise RuntimeError(
            f"auth.enabled=true with unknown provider {settings.provider!r}; "
            f"expected 'mtls' or 'token', or register an external adapter — "
            f"refusing to start"
        )

    return AuthGate(
        enabled=True,
        authenticator=authenticator,
        authorizer=PolicyAuthorizer(settings.policy),
        metrics=metrics,
    )


def get_auth_gate(request: Request) -> AuthGate:
    gate: AuthGate | None = getattr(request.app.state, "auth_gate", None)
    if gate is None:  # pragma: no cover — lifespan guarantees this
        raise RuntimeError("AuthGate is not initialised; lifespan did not run")
    return gate


def authorize(action: Action) -> Callable[..., Any]:
    """Dependency factory: ``Depends(authorize("read"))``.

    ``resource`` is the data-source name from the path — the coarse infrastructure
    resource ADR-008 scopes policy to.
    """

    async def dependency(
        request: Request,
        name: str = Path(default=...),
        gate: AuthGate = Depends(get_auth_gate),
    ) -> Principal:
        return await gate.check(request, action=action, resource=name)

    return dependency


def authorize_global(action: Action, resource: str = "*") -> Callable[..., Any]:
    """Same, for routes with no ``{name}`` in the path (e.g. listing sources)."""

    async def dependency(
        request: Request,
        gate: AuthGate = Depends(get_auth_gate),
    ) -> Principal:
        return await gate.check(request, action=action, resource=resource)

    return dependency


__all__ = [
    "AuthGate",
    "Principal",
    "authorize",
    "authorize_global",
    "build_auth_gate",
    "get_auth_gate",
]
