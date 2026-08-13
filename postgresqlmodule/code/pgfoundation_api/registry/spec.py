"""L4 — ``OperationSpec``: the descriptor contract (ADR-003).

One row per capability. The table of these rows
(``api/v1/operations.py``) is **pure data**; everything that turns a row into a
working endpoint lives here and in :mod:`.endpoints`, version-agnostic, written
once.

Adding a capability is adding a row — enforced by the registry invariant tests,
not by discipline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from ..security.ports import Action


@dataclass(frozen=True)
class OperationSpec:
    """One capability, described completely.

    The single source of truth for that capability: it produces the unary REST
    endpoint, the streaming endpoint, the batch-dispatch entry and the OpenAPI
    operation.
    """

    #: Stable identifier. Used in batch items (``{"op": "view"}``), as a metric
    #: label, and as the key in ``OPERATION_BY_ID``. Never change one in place —
    #: it is part of the wire contract.
    op: str

    #: URL segment under ``/{version}/datasources/{name}``.
    suffix: str

    #: Request DTO. Also the OpenAPI request schema, so it must be readable.
    request_model: type[BaseModel]

    #: Response DTO.
    response_model: type[BaseModel]

    #: DTO -> core value object (``ViewIn`` -> ``ViewQuery``).
    to_core: Callable[[Any], Any]

    #: Result -> response DTO. Separate from ``to_core`` because handle methods
    #: return different shapes: ``ResultSet`` for reads, ``int`` for ``execute``.
    to_response: Callable[[Any], BaseModel]

    #: Method name on ``DataSourceHandle`` — resolved with ``getattr``.
    handle_method: str

    #: What ``AuthGate`` authorizes. The factory wires this in, so an endpoint
    #: cannot forget its check.
    action: Action

    #: Also expose ``/{suffix}/stream`` (P3).
    streamable: bool = False

    #: Raw-SQL surface, registered only when ``api.enable_raw_sql`` is on.
    trusted_only: bool = False

    summary: str = ""
    tag: str = "invoke"

    def __post_init__(self) -> None:
        if not self.op or not self.op.replace("_", "").isalnum():
            raise ValueError(f"OperationSpec.op must be a bare identifier, got {self.op!r}")
        if not self.suffix:
            raise ValueError(f"OperationSpec.suffix must be non-empty (op={self.op!r})")


__all__ = ["OperationSpec"]
