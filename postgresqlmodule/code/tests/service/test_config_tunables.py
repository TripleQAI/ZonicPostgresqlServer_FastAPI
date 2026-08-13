"""P5.2/P5.3 — the last hard-coded operational values, now config.

Requirement 2's second half: *no hard-coded operational value*. `itersize=1000`
lived in four method signatures and the prepared-statement threshold was whatever
the driver chose. Both are now per-data-source config, which matters because a
long-scan source and an OLTP source want different answers.
"""

from __future__ import annotations

from pgfoundation import DataFoundation
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver


def test_stream_and_prepare_defaults_are_documented():
    settings = load_settings({"datasources": [{"name": "main", "dsn": "postgresql://x/main"}]})
    policy = settings.datasources[0].to_pool_policy()
    assert policy.itersize == 1000
    assert policy.prepare_threshold == 5
    assert policy.prepare_max_cached == 100


def test_per_datasource_tunables_reach_the_pool_policy():
    """A long-scan source should be able to fetch in bigger batches than OLTP."""
    settings = load_settings(
        {
            "datasources": [
                {"name": "oltp", "dsn": "postgresql://x/o"},
                {
                    "name": "analytics",
                    "dsn": "postgresql://x/a",
                    "stream": {"itersize": 20000},
                    "prepare": {"threshold": 1, "max_cached": 500},
                },
            ]
        }
    )
    oltp, analytics = (ds.to_pool_policy() for ds in settings.datasources)

    assert oltp.itersize == 1000
    assert analytics.itersize == 20000
    assert analytics.prepare_threshold == 1
    assert analytics.prepare_max_cached == 500


async def test_handle_streams_with_its_configured_itersize():
    """The handle inherits its source's batch size; no caller passes a literal."""
    seen: list[int] = []

    class Recording(FakeDriver):
        def build_pool(self, dsn, policy):
            pool = super().build_pool(dsn, policy)
            original = pool.acquire

            async def acquire():
                conn = await original()

                async def stream(spec, *, itersize=1000):
                    seen.append(itersize)
                    yield {"id": 1}

                conn.stream = stream  # type: ignore[method-assign]
                return conn

            pool.acquire = acquire  # type: ignore[method-assign]
            return pool

    settings = load_settings(
        {
            "datasources": [
                {"name": "analytics", "dsn": "postgresql://x/a", "stream": {"itersize": 7777}}
            ]
        }
    )
    foundation = await DataFoundation.from_settings(settings, driver=Recording())

    from pgfoundation import Query

    async for _ in foundation.datasource("analytics").stream(Query("SELECT 1")):
        pass

    assert seen == [7777], "the configured itersize did not reach the driver"
    await foundation.aclose()


async def test_an_explicit_itersize_still_wins():
    """Config sets the default; a caller with a reason can still override."""
    seen: list[int] = []

    class Recording(FakeDriver):
        def build_pool(self, dsn, policy):
            pool = super().build_pool(dsn, policy)
            original = pool.acquire

            async def acquire():
                conn = await original()

                async def stream(spec, *, itersize=1000):
                    seen.append(itersize)
                    yield {"id": 1}

                conn.stream = stream  # type: ignore[method-assign]
                return conn

            pool.acquire = acquire  # type: ignore[method-assign]
            return pool

    settings = load_settings(
        {"datasources": [{"name": "a", "dsn": "postgresql://x/a", "stream": {"itersize": 100}}]}
    )
    foundation = await DataFoundation.from_settings(settings, driver=Recording())

    from pgfoundation import Query

    async for _ in foundation.datasource("a").stream(Query("SELECT 1"), itersize=42):
        pass

    assert seen == [42]
    await foundation.aclose()
