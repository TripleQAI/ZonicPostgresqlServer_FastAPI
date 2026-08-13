"""L4 — the shipped caller authenticators (ADR-008 §3, P4.8).

ADR-008's specific reversal of the inherited ADR-013: **ship a provider, not only
a port.** A fail-closed seam with no implementation cannot be switched on without
work nobody owns, so it stays disabled indefinitely and "temporarily on a trusted
network" becomes the permanent posture.

Both providers are deliberately small and boring. Richer adapters — a real
identity provider, mTLS with full chain validation — register through the same
``AuthenticatorPort`` and need no change here.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from ..settings import AuthMtlsSettings, AuthTokenSettings
from .ports import Principal


class MeshIdentityAuthenticator:
    """Trust a caller-identity header asserted by the service mesh or ingress.

    **This header is only trustworthy if the mesh sets it and the service is not
    reachable except through the mesh.** Accepting it from an untrusted hop makes
    impersonation a one-line curl. Deployments must therefore ensure the header
    is stripped at the edge — which is exactly why this is not the default.
    """

    def __init__(self, settings: AuthMtlsSettings) -> None:
        self._header = settings.identity_header.lower()

    async def authenticate(self, request: Any) -> Principal | None:
        subject = request.headers.get(self._header)
        if not subject:
            return None
        return Principal(subject=subject)


class SharedSecretAuthenticator:
    """Per-service API key, compared in constant time.

    Keys map to a subject, so the policy table keys off a stable service name
    rather than off the credential itself. Rotating a key does not rewrite policy.
    """

    def __init__(self, settings: AuthTokenSettings, keys: dict[str, str]) -> None:
        self._leeway = settings.leeway_seconds
        # digest -> subject. Storing digests keeps the raw key out of memory
        # dumps and out of any accidental repr.
        self._digests = {
            hashlib.sha256(key.encode()).hexdigest(): subject for subject, key in keys.items()
        }

    async def authenticate(self, request: Any) -> Principal | None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else header
        if not token:
            return None

        digest = hashlib.sha256(token.encode()).hexdigest()
        # compare_digest against each known digest: constant time per comparison,
        # so a timing signal cannot enumerate valid keys.
        for known, subject in self._digests.items():
            if hmac.compare_digest(digest, known):
                return Principal(subject=subject)
        return None


__all__ = ["MeshIdentityAuthenticator", "SharedSecretAuthenticator"]
