"""L4 — the composition root's plug points (ADR-014, doc 11 §11.11).

The one place that knows which concrete observability and auth implementations
this process uses. Everything else depends on the ports.
"""

from __future__ import annotations

import os

from pgfoundation._internal.observability.noop import NoopMetrics

from ..observability.metrics import MetricsCollector
from ..settings import AuthSettings, ObservabilitySettings

# Prefix for the per-caller key env vars the platform's secret store syncs in.
# Read from the environment, never from the config file: doc 05's rule is that
# the file names WHO may call (auth.policy.callers) and the secret store holds
# WHAT PROVES IT. Committing a key to a values file would put a live credential
# in git.
#
# ONE VAR PER CALLER, not one JSON blob, because each key is a separate Secrets
# Manager secret -- rotating the BFF's credential must not require reading,
# rewriting and re-deploying every other caller's. The CSI driver syncs each
# secret to its own key in one Kubernetes Secret, and each of those becomes one
# env var; assembling JSON would need a step nothing in this platform has.
#
# The suffix is the caller subject, upper-cased with '-' as '_':
#   zonic-bff -> PGF_API_KEY_ZONIC_BFF
API_KEY_ENV_PREFIX = "PGF_API_KEY_"


def _env_var_for(subject: str) -> str:
    """The env var carrying one caller's key.

    Derived from the subject rather than configured separately: two places to
    write the same name is one place for them to disagree, and the failure --
    a caller whose key is silently absent -- looks like a bad credential.
    """
    return API_KEY_ENV_PREFIX + subject.upper().replace("-", "_")


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


def load_api_keys(settings: AuthSettings, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve ``subject -> key`` for ``SharedSecretAuthenticator``.

    The policy is the source of truth for WHO may call: this reads one env var
    per subject named in ``auth.policy.callers``, so a caller that no policy
    names has nowhere to put a key. Driving it from the policy rather than from
    whatever the environment happens to hold is what makes an orphaned
    credential impossible rather than merely detectable.

    FAILS CLOSED, because each alternative silently produces a service that
    authenticates nobody:

    * a subject in the policy with no key in the environment -> refuse to
      start. This is the specific bug this function exists to prevent:
      build_auth_gate accepted ``api_keys=None`` and produced an authenticator
      whose key map was empty, so every caller was rejected with no indication
      why.
    * an empty or whitespace-only key -> refuse to start, rather than
      registering a credential nothing can present.

    Returns an empty map when auth is disabled or the provider is not ``token``:
    there is nothing to prove, so requiring a secret would make every local run
    and every test need one.
    """
    if not settings.enabled or settings.provider != "token":
        return {}

    env = environ if environ is not None else dict(os.environ)

    subjects = [caller.subject for caller in settings.policy.callers]
    if not subjects:
        raise RuntimeError(
            "auth.enabled=true with provider 'token' but auth.policy.callers is "
            "empty; no caller could ever be authorized -- refusing to start"
        )

    keys: dict[str, str] = {}
    missing: list[str] = []
    for subject in subjects:
        value = env.get(_env_var_for(subject), "").strip()
        if not value:
            missing.append(f"{subject} ({_env_var_for(subject)})")
            continue
        keys[subject] = value

    if missing:
        # Names the subject and the variable, never the value: a startup error
        # goes to logs an operator reads and a platform ships elsewhere.
        raise RuntimeError(
            "auth.enabled=true with provider 'token' but no key was supplied for: "
            f"{', '.join(missing)}. Every request from those callers would be "
            "rejected -- refusing to start."
        )

    return keys


__all__ = ["API_KEY_ENV_PREFIX", "build_metrics", "load_api_keys"]
