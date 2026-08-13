"""L2 — thin Repository base (doc 07 §7.6). No identity map, no lazy loading

(that would make us an ORM, a non-goal). It centralizes a team's SQL and
decouples callers from the execution API.
"""
from __future__ import annotations

from typing import Callable, Generic, TypeVar

from ...core.models import Command, Query, Row

T = TypeVar("T")

# A mapper turns a raw dict row into the target type. Default = identity (dict).
Mapper = Callable[[Row], T]


class Repository(Generic[T]):
    def __init__(self, handle, mapper: Mapper | None = None) -> None:
        self._handle = handle
        self._map: Mapper = mapper or (lambda row: row)  # type: ignore[assignment]

    async def find(self, spec: Query) -> list[T]:
        rs = await self._handle.query(spec)
        return [self._map(row) for row in rs.rows]

    async def one(self, spec: Query) -> T | None:
        row = await self._handle.one(spec)
        return self._map(row) if row is not None else None

    async def execute(self, cmd: Command) -> int:
        return await self._handle.execute(cmd)


__all__ = ["Repository", "Mapper"]
