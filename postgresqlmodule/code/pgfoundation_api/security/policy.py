"""L4 — coarse authorization: caller -> {datasource} x {action} (ADR-008, P4.9).

**Infrastructure policy, not business policy.** This answers "may `analytics-api`
read `analytics`?" It does not know about users, tenants or rows — those stay
with the higher-level APIs, which are the only layer that knows who the end user
is.

The policy is plain config with ``default: deny``, so adding a caller is a
reviewable diff rather than a code change.
"""

from __future__ import annotations

from ..settings import AuthPolicySettings
from .ports import Principal


class PolicyAuthorizer:
    """Table-driven authorizer built from ``auth.policy`` (doc 05)."""

    def __init__(self, settings: AuthPolicySettings) -> None:
        self._default_allow = settings.default == "allow"
        # subject -> (datasources, actions). A wildcard "*" matches everything,
        # which keeps single-tenant deployments from needing a row per source.
        self._rules: dict[str, tuple[frozenset[str], frozenset[str]]] = {
            caller.subject: (frozenset(caller.datasources), frozenset(caller.actions))
            for caller in settings.callers
        }

    async def authorize(self, principal: Principal, action: str, resource: str) -> bool:
        rule = self._rules.get(principal.subject)
        if rule is None:
            # An unknown caller is not a partially-trusted caller.
            return self._default_allow

        datasources, actions = rule
        source_ok = "*" in datasources or resource in datasources
        action_ok = "*" in actions or action in actions
        return source_ok and action_ok

    def known_subjects(self) -> frozenset[str]:
        return frozenset(self._rules)


__all__ = ["PolicyAuthorizer"]
