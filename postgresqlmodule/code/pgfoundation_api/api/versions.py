"""Which API versions this process mounts (doc 09 §9.1b).

Adding v2 is one entry here plus an ``api/v2/`` package. The engine, the batch
layer and the security model are reused untouched.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter

from ..settings import ServiceSettings
from . import v1

#: version name -> builder
VERSIONS: dict[str, Callable[[ServiceSettings], APIRouter]] = {
    "v1": v1.build,
    # "v2": v2.build,
}


def mounted(settings: ServiceSettings) -> list[tuple[str, APIRouter]]:
    """Build every enabled version's router, in config order."""
    out: list[tuple[str, APIRouter]] = []
    for version in settings.api.versions:
        if not version.enabled:
            continue
        builder = VERSIONS.get(version.name)
        if builder is None:
            raise ValueError(
                f"api.versions names {version.name!r}, which this build does not "
                f"provide; known versions: {sorted(VERSIONS)}"
            )
        out.append((version.name, builder(settings)))
    return out


__all__ = ["VERSIONS", "mounted"]
