"""L4 — the auth seam's ports (ADR-008, ported from the Django shell).

The foundation owns **caller** identity and coarse ``datasource x action`` policy.
**End-user** identity, tenancy and row-level policy stay with the higher-level
APIs — the foundation has no concept of a user, and modelling one here would put
business semantics in an infrastructure layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

#: The coarse action vocabulary ADR-008 scopes policy to. It lives here, not in
#: the registry, because it is part of the authorization model — and because
#: `security/` must not import `registry/` (the cross-cutting-are-leaves
#: contract, doc 09 §9.3).
Action = Literal["read", "write", "list"]


class AuthError(Exception):
    """Base for auth failures (mapped to 401/403 by the shell)."""


class AuthenticationError(AuthError):
    """401 — could not identify the caller."""


class AuthorizationError(AuthError):
    """403 — caller not permitted for this action/resource."""


@dataclass(frozen=True)
class Principal:
    """The calling **service**, not an end user."""

    subject: str
    scopes: tuple[str, ...] = ()


class AuthenticatorPort(Protocol):
    async def authenticate(self, request: Any) -> Principal | None: ...


class AuthorizerPort(Protocol):
    async def authorize(self, principal: Principal, action: str, resource: str) -> bool: ...


class AllowAllAuthenticator:
    """Default when auth is disabled — every caller is anonymous."""

    async def authenticate(self, request: Any) -> Principal:
        return Principal(subject="anonymous")


class AllowAllAuthorizer:
    """Default when auth is disabled — every action is permitted."""

    async def authorize(self, principal: Principal, action: str, resource: str) -> bool:
        return True


__all__ = [
    "Action",
    "AllowAllAuthenticator",
    "AllowAllAuthorizer",
    "AuthenticationError",
    "AuthenticatorPort",
    "AuthError",
    "AuthorizationError",
    "AuthorizerPort",
    "Principal",
]
