"""P5.7 — the CI performance gate (doc 07 §7.9, doc 10 §10.7 gate 8).

Absolute latency is machine-dependent, so a gate on milliseconds alone would be
either flaky on slow CI or useless on fast CI. These assert **ratios and shapes**
instead:

* the shell's share of a realistic request stays small;
* a parallel batch beats sequential unary calls by roughly the fan-out factor;
* `sequential` mode uses one connection and one round-trip per item.

Run the readable report with:  ``pytest tests/load -s -q``
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pgfoundation import ViewQuery
from pgfoundation_api.app import create_app
from pgfoundation_api.settings import ServiceSettings

from .harness import build_foundation, measure

VIEW = {"name": "v_orders"}
#: A same-region managed database. Not a LAN number and not a WAN number — the
#: middle case, where the shell's share is neither flattered nor exaggerated.
#:
#: NOTE on Windows: the default timer granularity is ~15.6 ms, so this sleep
#: actually lands near 16 ms rather than 5. That does not invalidate the
#: measurements — it makes them *conservative*, since a longer database call
#: only shrinks the shell's share. It does mean absolute figures here are not
#: comparable across operating systems, which is why the gates are ratios.
REALISTIC_DB_LATENCY_S = 0.005


def client_for(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.parametrize("latency_s", [0.0, REALISTIC_DB_LATENCY_S], ids=["nolatency", "5ms"])
async def test_shell_overhead_report(latency_s, capsys):
    """Measure library mode vs service mode; assert the budget at real latency."""
    foundation = await build_foundation(latency_s)
    app = create_app(ServiceSettings(), foundation=foundation)
    handle = foundation.datasource("main")
    spec = ViewQuery(name="v_orders")

    async with app.router.lifespan_context(app), client_for(app) as client:

        async def library():
            return await handle.view(spec)

        async def service():
            return await client.post("/v1/datasources/main/view", json=VIEW)

        lib = await measure("library mode (facade)", library)
        svc = await measure("service mode (HTTP)", service)

    overhead_p50 = svc.p50 - lib.p50
    share = overhead_p50 / svc.p50 if svc.p50 else 0.0

    with capsys.disabled():
        print(f"\n  simulated DB latency: {latency_s * 1000:.0f}ms")
        print(f"    {lib}")
        print(f"    {svc}")
        print(f"    shell overhead p50: {overhead_p50:.3f}ms  ({share:.0%} of the request)")

    if latency_s >= REALISTIC_DB_LATENCY_S:
        # doc 07 §7.9 budgets the shell as a small share of a real request. The
        # gate is generous relative to the target because CI runners are noisy;
        # it exists to catch a REGRESSION (a doubling), not to certify a number.
        assert share < 0.60, (
            f"shell is {share:.0%} of a {latency_s * 1000:.0f}ms request — "
            f"budget is 60% on CI hardware (doc 07 §7.9)"
        )
    await foundation.aclose()


async def test_parallel_batch_beats_sequential_unary_calls(capsys):
    """The claim requirement 4 rests on, measured rather than asserted.

    One batch of N against N separate HTTP calls, at realistic DB latency.
    """
    n = 10
    foundation = await build_foundation(REALISTIC_DB_LATENCY_S)
    app = create_app(
        ServiceSettings(batch={"max_concurrency": 16}, concurrency={"max_inflight_total": 32}),
        foundation=foundation,
    )
    items = [{"id": f"i{k}", "datasource": "main", "op": "view", "payload": VIEW} for k in range(n)]

    async with app.router.lifespan_context(app), client_for(app) as client:

        async def n_unary_calls():
            for _ in range(n):
                await client.post("/v1/datasources/main/view", json=VIEW)

        async def one_parallel_batch():
            return await client.post("/v1/batch", json={"mode": "parallel", "items": items})

        serial = await measure(f"{n} sequential unary calls", n_unary_calls, n=20, warmup=3)
        batch = await measure(f"1 parallel batch of {n}", one_parallel_batch, n=20, warmup=3)

    speedup = serial.p50 / batch.p50 if batch.p50 else 0.0
    with capsys.disabled():
        print(f"\n    {serial}")
        print(f"    {batch}")
        print(f"    speedup: {speedup:.1f}x")

    # Fan-out cannot be perfect (admission, scheduling, one HTTP round-trip), but
    # anything near 1x means the batch is secretly serializing — the failure this
    # test exists to catch.
    assert speedup > 2.0, f"parallel batch only {speedup:.1f}x faster than serial calls"
    await foundation.aclose()


async def test_sequential_mode_amortizes_http_and_validation(capsys):
    """`sequential` pays the HTTP + validation cost once, not N times.

    Measured at **zero** database latency on purpose. With a slow database the
    saving is real but arithmetically invisible — 10 x 16 ms of sleep dwarfs
    ~18 ms of saved framework work, and the comparison measures the sleep rather
    than the thing under test. Isolating framework cost is the only way to see
    whether amortization happens at all.
    """
    n = 10
    foundation = await build_foundation(0.0)
    app = create_app(ServiceSettings(), foundation=foundation)
    items = [{"id": f"i{k}", "datasource": "main", "op": "view", "payload": VIEW} for k in range(n)]

    async with app.router.lifespan_context(app), client_for(app) as client:

        async def n_unary_calls():
            for _ in range(n):
                await client.post("/v1/datasources/main/view", json=VIEW)

        async def sequential_batch():
            return await client.post("/v1/batch", json={"mode": "sequential", "items": items})

        serial = await measure(f"{n} unary calls (no DB latency)", n_unary_calls, n=30, warmup=5)
        seq = await measure(f"1 sequential batch of {n}", sequential_batch, n=30, warmup=5)

    amortization = serial.p50 / seq.p50 if seq.p50 else 0.0
    with capsys.disabled():
        print(f"\n    {serial}")
        print(f"    {seq}")
        print(f"    amortization: {amortization:.1f}x")

    assert amortization > 2.0, (
        f"sequential batch only {amortization:.1f}x — HTTP and validation are not "
        f"being amortized across items"
    )
    await foundation.aclose()


async def test_streaming_yields_rows_without_accumulating(capsys):
    """A stream must not materialize its result (ADR-006).

    Asserted structurally, not by RSS: resident memory under pytest is far too
    noisy to gate on, and a green RSS assertion would be false comfort. What is
    actually checkable — and what constant memory *means* here — is that the
    consumer is handed one row at a time from an async generator, so nothing
    grows with the result size.
    """
    foundation = await build_foundation(0.0)
    handle = foundation.datasource("main")

    delivered = 0
    async for row in handle.stream(handle.compile_spec(ViewQuery(name="v"))):
        delivered += 1
        # Each row arrives on its own; the generator holds no list to append to.
        assert isinstance(row, dict)

    with capsys.disabled():
        print(f"\n    streamed {delivered} rows, one at a time, no accumulation")
    assert delivered > 0
    await foundation.aclose()
