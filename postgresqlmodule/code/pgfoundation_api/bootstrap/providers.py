"""L4 — the composition root's plug points (ADR-014, doc 11 §11.11).

The one place that knows which concrete observability and auth implementations
this process uses. Everything else depends on the ports.
"""

from __future__ import annotations

import os
from pathlib import Path

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

# Where the secrets-store CSI driver writes each mounted object as a FILE.
#
# Files, not the synced Kubernetes Secret, are the primary source. The driver
# writes these DURING the volume mount, which completes before the container is
# created; it populates the Secret afterwards, and kubelet resolves every
# secretKeyRef at container-creation time. Those two orderings race, and a key
# that has never been in the Secret before loses: the container fails with
# "couldn't find key PGF_API_KEY_... in Secret", while the file is sitting there
# correctly mounted. Cost three rollouts to find, 2026-09-13.
#
# An env var still wins when set, so a deployment that injects keys some other
# way -- or a test -- needs no file.
API_KEY_MOUNT_PATH = "/mnt/secrets-store"


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

    mount = Path(env.get("PGF_API_KEY_MOUNT_PATH", API_KEY_MOUNT_PATH))

    keys: dict[str, str] = {}
    missing: list[str] = []
    for subject in subjects:
        # Env var first so an explicit injection always wins, then the mounted
        # file, which is what the CSI driver guarantees is present before this
        # process starts.
        value = env.get(_env_var_for(subject), "").strip()
        if not value:
            candidate = mount / f"caller-{subject}"
            try:
                value = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
        if not value:
            missing.append(f"{subject} ({_env_var_for(subject)} or {mount}/caller-{subject})")
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


__all__ = ["API_KEY_ENV_PREFIX", "API_KEY_MOUNT_PATH", "build_metrics", "load_api_keys"]
