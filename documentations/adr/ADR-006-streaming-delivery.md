# ADR-006 — Streaming delivery: NDJSON and SSE

**Status:** Accepted
**Date:** 2026-08-13
**Serves:** requirements 1 (delivers designed-but-unbuilt features) and 4

## Context

The core engine has had streaming since Phase 1.5: `stream()` over a server-side
cursor, `interval()`, `listen()` for `LISTEN/NOTIFY`, and `copy_stream()` for bulk
ingest — all async generators with constant memory, all unit-tested.

**None of it is reachable over HTTP.** The Django `urls.py` exposes five routes,
none of them streaming. The design docs specify chunked NDJSON, SSE, gRPC
server-streaming and Arrow IPC as delivery formats; none were built.

Meanwhile the unary endpoints materialize whole result sets into a Python list,
then into a JSON document, with no size bound. A large view read is an OOM waiting
for a caller.

## Decision

Ship **two** wire formats in v1, over one internal async generator:

| Format | Media type | Route shape | Use |
|--------|-----------|-------------|-----|
| **NDJSON** (default) | `application/x-ndjson` | `POST .../{op}/stream` | Large reads, exports, batch extraction |
| **SSE** | `text/event-stream` | same route with `Accept: text/event-stream`; `GET .../subscribe/{channel}` | Live dashboards, `LISTEN/NOTIFY` feeds, browser `EventSource` |

Content negotiation via `Accept` is a **Strategy**: the endpoint gets one async
generator from the Facade and picks a framing adapter. Adding Arrow IPC later is a
third adapter, not a new endpoint.

**Rules:**

1. **Backpressure is honored end-to-end.** The generator yields, the ASGI server
   awaits the send; a slow client throttles the database fetch. No unbounded
   server-side buffer.
2. **Connections are released deterministically.** The generator's `finally`
   closes the cursor and returns the connection — including on client disconnect
   and task cancellation. This is tested (`released == acquired`).
3. **Cancellation reaches PostgreSQL.** A disconnect must cancel the query, not
   just the coroutine; otherwise the server keeps working for a socket nobody
   reads.
4. **Errors after the first byte are in-band.** The status line is already sent,
   so a mid-stream failure emits a final line
   `{"error": {...}, "partial": true}`. This is part of the documented contract,
   because a silently truncated stream is indistinguishable from a complete one.
5. **Streams count against the per-datasource bulkhead** and should target a
   dedicated long-scan data source. They are excluded from batch items
   ([ADR-004](./ADR-004-batch-and-parallel-execution.md)).
6. **The unary path gets a bound.** `api.max_response_rows` exceeded ⇒ `413`
   naming the `/stream` route, instead of an OOM.

## Options considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **NDJSON + SSE now, Arrow behind a seam** | Covers export and live-feed cases with zero new dependencies; both are trivially consumable (`curl -N`, `EventSource`) | Not columnar | **Chosen** |
| Arrow IPC / Arrow Flight in v1 | Best for analytics consumers; columnar and compact | Heavy dependency; a second server for Flight; the delivery Strategy seam absorbs it later with no core change | Deferred |
| gRPC server-streaming in v1 | Native backpressure, typed | A whole second transport to keep at parity, for internal callers only. The operation registry makes it additive later | Deferred |
| WebSockets for live feeds | Bidirectional | These feeds are one-way; SSE reconnects natively and needs no protocol upgrade | Rejected |
| Paginated polling instead of streaming | Simple | Turns one cursor into N deep queries; `fetch_since` already covers the *resumable* case properly, keyset-based | Rejected |

## Consequences

- **Positive:** core capabilities built and tested a phase ago finally become
  usable by non-Python clients — a large parity gain for very little new code,
  since most streaming routes are registry rows once the framing adapter exists.
- **Positive:** the memory profile of the service stops depending on the size of
  the largest table a client happens to query.
- **Negative / mitigation:** streaming endpoints hold a connection for the client's
  read duration. Mitigated by the per-datasource semaphore, a dedicated long-scan
  data source, and an idle-stream timeout.
- **Negative / mitigation:** in-band error reporting is unusual and clients must be
  told to check the last line. Documented in the OpenAPI description for every
  stream route and covered by a contract test.
