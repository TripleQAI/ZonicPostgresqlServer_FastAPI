"""Caller keys reach the auth gate, and every way of not reaching it fails closed.

ADR-008 ships a working provider precisely so "temporarily on a trusted network"
does not become permanent. That only holds if turning auth on actually
authenticates somebody -- and until 2026-09-12 it did not: app.py called
build_auth_gate WITHOUT api_keys, so provider 'token' built an authenticator
with an empty key map and rejected every caller. The service looked configured
and authenticated nobody.

Each test below is one way that failure could recur.
"""

from __future__ import annotations

import pytest

from pgfoundation_api.bootstrap.providers import API_KEY_ENV_PREFIX, load_api_keys
from pgfoundation_api.security.gate import build_auth_gate
from pgfoundation_api.settings import AuthPolicySettings, AuthSettings, CallerPolicy


def auth_settings(**overrides: object) -> AuthSettings:
    defaults: dict[str, object] = {
        "enabled": True,
        "provider": "token",
        "policy": AuthPolicySettings(
            default="deny",
            callers=[CallerPolicy(subject="zonic-bff", datasources=["tsdb"], actions=["read"])],
        ),
    }
    defaults.update(overrides)
    return AuthSettings(**defaults)  # type: ignore[arg-type]


BFF_KEY_VAR = f"{API_KEY_ENV_PREFIX}ZONIC_BFF"


def test_keys_are_read_from_the_environment() -> None:
    keys = load_api_keys(auth_settings(), {BFF_KEY_VAR: "key-1"})

    assert keys == {"zonic-bff": "key-1"}


def test_the_env_var_name_is_derived_from_the_subject() -> None:
    """One name, derived, not two names kept in sync. A hyphen becomes an
    underscore because env var names cannot carry one."""
    assert BFF_KEY_VAR == "PGF_API_KEY_ZONIC_BFF"


def test_auth_disabled_needs_no_keys() -> None:
    """A local run, a test, or a library embed must not require a secret to
    exist. Nothing is being proved, so there is nothing to supply."""
    assert load_api_keys(auth_settings(enabled=False), {}) == {}


def test_a_non_token_provider_needs_no_keys() -> None:
    """mtls proves identity with a header the mesh asserts, not with a key."""
    assert load_api_keys(auth_settings(provider="mtls"), {}) == {}


def test_a_missing_key_refuses_to_start() -> None:
    """THE regression this module exists for.

    Auth on, provider token, no key: previously produced a running service that
    rejected every caller. It must now refuse to start, because a failed deploy
    is diagnosable and a silent 401 for every caller is not.
    """
    with pytest.raises(RuntimeError, match="zonic-bff"):
        load_api_keys(auth_settings(), {})


def test_the_error_names_the_variable_to_set() -> None:
    """An operator reading this should not have to derive the name."""
    with pytest.raises(RuntimeError, match="PGF_API_KEY_ZONIC_BFF"):
        load_api_keys(auth_settings(), {})


def test_a_blank_key_refuses_to_start() -> None:
    """An empty secret is the shape a failed CSI sync leaves behind. Treating
    it as a credential would register one nothing can present."""
    with pytest.raises(RuntimeError, match="zonic-bff"):
        load_api_keys(auth_settings(), {BFF_KEY_VAR: "   "})


def test_an_empty_policy_refuses_to_start() -> None:
    """Doc 05 line 167's rule: authenticated-but-authorizes-nobody is a
    misconfiguration, not a lockdown."""
    settings = auth_settings(policy=AuthPolicySettings(default="deny", callers=[]))

    with pytest.raises(RuntimeError, match="auth.policy.callers is"):
        load_api_keys(settings, {})


def test_keys_are_driven_by_the_policy_not_the_environment() -> None:
    """A key in the environment for a subject no policy names is ignored, so an
    orphaned credential cannot silently become a working one."""
    keys = load_api_keys(
        auth_settings(),
        {BFF_KEY_VAR: "key-1", f"{API_KEY_ENV_PREFIX}GHOST_SERVICE": "key-2"},
    )

    assert keys == {"zonic-bff": "key-1"}


def test_every_policy_subject_needs_its_own_key() -> None:
    settings = auth_settings(
        policy=AuthPolicySettings(
            default="deny",
            callers=[
                CallerPolicy(subject="zonic-bff", datasources=["tsdb"], actions=["read"]),
                CallerPolicy(
                    subject="zonic-authorization-server",
                    datasources=["tsdb"],
                    actions=["read", "write"],
                ),
            ],
        )
    )

    with pytest.raises(RuntimeError, match="zonic-authorization-server"):
        load_api_keys(settings, {BFF_KEY_VAR: "key-1"})


def test_the_error_never_echoes_the_secret() -> None:
    """A startup error goes to logs an operator reads and a platform ships
    elsewhere. The credential must not travel with it."""
    secret = "super-secret-value-do-not-log"
    settings = auth_settings(
        policy=AuthPolicySettings(
            default="deny",
            callers=[
                CallerPolicy(subject="zonic-bff", datasources=["tsdb"], actions=["read"]),
                CallerPolicy(subject="other", datasources=["tsdb"], actions=["read"]),
            ],
        )
    )

    with pytest.raises(RuntimeError) as raised:
        load_api_keys(settings, {BFF_KEY_VAR: secret})

    assert secret not in str(raised.value)


def test_the_gate_refuses_an_empty_key_map() -> None:
    """Defence in depth: build_auth_gate is public, so a future caller could
    bypass load_api_keys entirely."""
    with pytest.raises(RuntimeError, match="no api_keys"):
        build_auth_gate(auth_settings(), api_keys={})


def test_the_gate_accepts_keys_and_enables_auth() -> None:
    gate = build_auth_gate(auth_settings(), api_keys={"zonic-bff": "key-1"})

    assert gate.enabled is True
