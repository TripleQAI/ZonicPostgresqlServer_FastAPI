"""L0 config — validation + env/secret interpolation (doc 05, doc 13 §13.2)."""
from __future__ import annotations

import pytest

from pgfoundation import ConfigError
from pgfoundation._internal.config.loader import load_settings


def _cfg(**overrides):
    base = {"datasources": [{"name": "orders", "dsn": "postgresql://x/orders"}]}
    base.update(overrides)
    return base


def test_valid_config_builds_settings():
    s = load_settings(_cfg())
    assert s.datasources[0].name == "orders"
    assert s.datasources[0].pool.max_size == 10  # documented default
    assert s.auth.enabled is False               # ADR-013 default
    assert s.observability.provider == "none"    # ADR-014 default


def test_dsn_is_secret_and_not_leaked_in_repr():
    s = load_settings(_cfg())
    assert "orders" not in repr(s.datasources[0].dsn)  # SecretStr redaction
    assert s.datasources[0].dsn.get_secret_value() == "postgresql://x/orders"


def test_duplicate_datasource_names_rejected():
    cfg = {
        "datasources": [
            {"name": "db", "dsn": "postgresql://x/a"},
            {"name": "db", "dsn": "postgresql://x/b"},
        ]
    }
    with pytest.raises(ConfigError, match="duplicate data-source names"):
        load_settings(cfg)


def test_empty_datasources_rejected():
    with pytest.raises(ConfigError, match="at least one data source"):
        load_settings({"datasources": []})


def test_pool_max_size_below_min_rejected():
    cfg = _cfg(datasources=[
        {"name": "db", "dsn": "postgresql://x/a", "pool": {"min_size": 5, "max_size": 2}}
    ])
    with pytest.raises(ConfigError):
        load_settings(cfg)


def test_env_interpolation(monkeypatch):
    monkeypatch.setenv("ORDERS_DSN", "postgresql://host/orders")
    s = load_settings({"datasources": [{"name": "orders", "dsn": "${env:ORDERS_DSN}"}]})
    assert s.datasources[0].dsn.get_secret_value() == "postgresql://host/orders"


def test_unresolved_reference_fails_fast():
    with pytest.raises(ConfigError, match="unresolved config reference"):
        load_settings({"datasources": [{"name": "o", "dsn": "${env:NOPE_MISSING}"}]})


def _secret_cfg():
    return {"datasources": [{"name": "o", "dsn": "${secret:ORDERS_DSN}"}]}


def test_secret_reads_the_mounted_file_when_env_is_unset(monkeypatch, tmp_path):
    """The deployed path: the CSI driver mounts the DSN as a file, and a key new
    to the synced Kubernetes Secret would race the container's secretKeyRef."""
    monkeypatch.delenv("ORDERS_DSN", raising=False)
    monkeypatch.setenv("PGF_SECRETS_MOUNT_PATH", str(tmp_path))
    (tmp_path / "ORDERS_DSN").write_text("postgresql://mounted/orders\n", encoding="utf-8")
    s = load_settings(_secret_cfg())
    assert s.datasources[0].dsn.get_secret_value() == "postgresql://mounted/orders"


def test_secret_prefers_env_over_the_mounted_file(monkeypatch, tmp_path):
    """The local path: .env supplies the value, and an explicit injection wins."""
    monkeypatch.setenv("ORDERS_DSN", "postgresql://env/orders")
    monkeypatch.setenv("PGF_SECRETS_MOUNT_PATH", str(tmp_path))
    (tmp_path / "ORDERS_DSN").write_text("postgresql://mounted/orders", encoding="utf-8")
    s = load_settings(_secret_cfg())
    assert s.datasources[0].dsn.get_secret_value() == "postgresql://env/orders"


def test_secret_with_no_env_and_no_file_fails_fast(monkeypatch, tmp_path):
    monkeypatch.delenv("ORDERS_DSN", raising=False)
    monkeypatch.setenv("PGF_SECRETS_MOUNT_PATH", str(tmp_path))
    with pytest.raises(ConfigError, match="unresolved config reference"):
        load_settings(_secret_cfg())


def test_secret_name_cannot_walk_out_of_the_mount(monkeypatch, tmp_path):
    """A flat name only, so a config value cannot read an arbitrary file."""
    mount = tmp_path / "mount"
    mount.mkdir()
    (tmp_path / "outside").write_text("postgresql://leak/x", encoding="utf-8")
    monkeypatch.setenv("PGF_SECRETS_MOUNT_PATH", str(mount))
    with pytest.raises(ConfigError, match="unresolved config reference"):
        load_settings({"datasources": [{"name": "o", "dsn": "${secret:../outside}"}]})


def test_session_timeout_sql_inlines_the_value():
    """`SET` is a utility statement: PostgreSQL rejects bound parameters for it.

    `SET statement_timeout = %s` fails with `syntax error at or near "$1"` as
    soon as a connection is configured with a timeout — which no test caught,
    because no configuration set one until the cloud data source did.
    """
    from pgfoundation._internal.drivers.psycopg_driver import session_timeout_sql

    sql = session_timeout_sql(30000)
    assert sql == "SET statement_timeout = 30000"
    assert "%s" not in sql and "$1" not in sql


def test_session_timeout_sql_refuses_anything_but_a_number():
    """`int()` is the only reason inlining is acceptable here.

    Anything that is not a number raises rather than reaching the SQL string, so
    this one inlined value cannot become an injection point.
    """
    import pytest

    from pgfoundation._internal.drivers.psycopg_driver import session_timeout_sql

    with pytest.raises(ValueError):
        session_timeout_sql("5000; DROP TABLE demo_orders")

    # A numeric string is still coerced, not concatenated.
    assert session_timeout_sql("250") == "SET statement_timeout = 250"
