"""Test bootstrap.

On Windows, psycopg's async mode requires a SelectorEventLoop (the default
ProactorEventLoop is unsupported). Production ASGI servers (uvicorn) select a
compatible loop; here we tell pytest-asyncio to use one via its
``event_loop_policy`` fixture.
"""
from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.fixture(scope="session")
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()
