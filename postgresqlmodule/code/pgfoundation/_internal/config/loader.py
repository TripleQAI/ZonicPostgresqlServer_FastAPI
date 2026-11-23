"""Config loading (doc 05) — provider chain + ``${env:...}`` / ``${secret:...}``.

Phase 0 supports dict, YAML file, and env-var interpolation. Secret-manager
providers plug in later behind the same seam (Chain of Responsibility).

``${secret:NAME}`` resolves from the environment first, then from a file named
``NAME`` under ``PGF_SECRETS_MOUNT_PATH`` (default ``/mnt/secrets-store``, the
CSI mount). The file leg exists for the reason caller keys are read from files
(bootstrap/providers.py): a key new to the CSI-synced Kubernetes Secret races
the container's ``secretKeyRef`` on its first deploy and loses, while the
mounted file is guaranteed present before the container starts.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ...core.errors import ConfigError
from .settings import AppSettings

_INTERP = re.compile(r"\$\{(env|secret):([^}]+)\}")

SECRETS_MOUNT_PATH = "/mnt/secrets-store"


def _read_mounted_secret(name: str) -> str | None:
    # A flat name only: the CSI driver's objectAlias is flat, and a name with a
    # separator would let a config value read any file the process can.
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    mount = Path(os.environ.get("PGF_SECRETS_MOUNT_PATH", SECRETS_MOUNT_PATH))
    try:
        value = (mount / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _resolve_scalar(value: str) -> str:
    """Resolve ``${env:NAME}``, and ``${secret:NAME}`` from env then the mount."""

    def repl(m: re.Match[str]) -> str:
        kind, name = m.group(1), m.group(2).strip()
        if kind == "env":
            resolved = os.environ.get(name)
        else:  # secret: — env first, then the CSI-mounted file
            resolved = (
                os.environ.get(name)
                or os.environ.get(name.replace("/", "_").upper())
                or _read_mounted_secret(name)
            )
        if resolved is None:
            raise ConfigError(f"unresolved config reference: ${{{kind}:{name}}}")
        return resolved

    return _INTERP.sub(repl, value)


def _resolve_tree(node: Any) -> Any:
    if isinstance(node, str):
        return _resolve_scalar(node)
    if isinstance(node, dict):
        return {k: _resolve_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_tree(v) for v in node]
    return node


def load_settings(source: str | Path | dict[str, Any]) -> AppSettings:
    """Build validated :class:`AppSettings` from a dict or a YAML/TOML-ish file."""
    if isinstance(source, dict):
        raw = source
    else:
        path = Path(source)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        text = path.read_text(encoding="utf-8")
        raw = _parse_file(path, text)

    resolved = _resolve_tree(raw)
    try:
        return AppSettings.model_validate(resolved)
    except Exception as exc:  # pydantic ValidationError → our ConfigError
        raise ConfigError(f"invalid configuration: {exc}") from exc


def _parse_file(path: Path, text: str) -> dict[str, Any]:
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # optional extra
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise ConfigError("PyYAML required for YAML config (pip install pyyaml)") from exc
        return yaml.safe_load(text) or {}
    if path.suffix == ".json":
        import json

        return json.loads(text)
    raise ConfigError(f"unsupported config format: {path.suffix}")


__all__ = ["load_settings"]
