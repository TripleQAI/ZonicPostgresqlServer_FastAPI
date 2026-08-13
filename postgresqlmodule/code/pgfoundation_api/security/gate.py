"""L4 — ``AuthGate`` and the ``authorize()`` dependency (ADR-005, ADR-008).

In the Django shell every view opened with ``await get_auth_gate().check(...)`` —
a line that could be forgotten. Here the check is a **dependency supplied by the
descriptor**, so an endpoint cannot be registered without it (ADR-003).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, Path, Request

from ..settings import AuthSettings
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
    ) -> None:
        self.enabled = enabled
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
            # OBSERVE (METRIC): pgf_auth_checks_total{action, result="unauthenticated"}
            raise AuthenticationError("authentication required")

        if not await self._authz.authorize(principal, action, resource):
            # OBSERVE (METRIC): pgf_auth_checks_total{action, result="deny"}
            # OBSERVE (LOG):    auth.denied {action, resource, subject}
            #                   — the SUBJECT, never the credential (doc 11 §11.8)
            raise AuthorizationError(f"not permitted: {action} {resource}")

        # OBSERVE (METRIC): pgf_auth_checks_total{action, result="allow"}
        return principal


def build_auth_gate(settings: AuthSettings) -> AuthGate:
    """Composition-root helper: pick providers from config (ADR-008 §3).

    P4.8/P4.9 register the real mTLS and token authenticators here. Until then,
    ``enabled: true`` fails closed rather than silently allowing everything —
    which is the behaviour the prod profile depends on.
    """
    if not settings.enabled:
        return AuthGate(enabled=False)
    # P4.8: resolve settings.provider -> concrete authenticator + policy authorizer.
    return AuthGate(enabled=True)


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
