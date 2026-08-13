# ADR-008 — The authentication & authorization boundary

**Status:** Accepted — **amends the inherited ADR-013** ("auth is a seam, disabled
by default, integrated later from a separate project")
**Date:** 2026-08-13
**Blocks:** [13 · P4.8 / P4.9](../13-implementation-plan.md)

## Context

The foundation is being adopted as the shared data layer beneath several
higher-level APIs. That raises a question the inherited design answers only for a
different deployment shape: **does the foundation authenticate and authorize, or
does the higher level do it?**

ADR-013 (inherited from the Django project) says the foundation ships an
`AuthenticatorPort` / `AuthorizerPort` seam with `AllowAll` defaults, disabled by
default, fails closed if enabled without a provider, and is "integrated later from
a separate project." Until then the service is expected to run open on a trusted
network behind a gateway.

That posture is **correct for a library, and for a sidecar deployed 1:1 with a
single caller** — there the process or pod boundary genuinely is the security
boundary. But this design targets the other shape explicitly: a **shared service**
acting as a connection concentrator ([02 §2.3](../02-architecture.md),
[07 §7.6](../07-performance.md)). Three facts follow from that choice:

1. **The service holds pooled credentials to every configured database** —
   credentials the calling applications deliberately do not have. By construction
   it is the highest-value target in the deployment.
2. **The blast radius is the whole database estate.** There is no row-level
   filtering, no tenant scoping, and — when `enable_raw_sql` is on — arbitrary
   parameterized SQL.
3. **"Trusted network" weakens as adoption grows**, which is precisely the success
   case for a foundation. One misconfigured NetworkPolicy, one debug port-forward,
   or one compromised pod anywhere on that network, and there is no second line.

Leaving this to "a separate project, later" has a predictable failure mode: a
fail-closed seam with **no shipped provider** cannot be enabled without work that
nobody owns, so it stays disabled indefinitely and "temporarily on a trusted
network" becomes the permanent posture.

## Decision

Split the responsibility along **caller identity vs. end-user identity** — not
along "foundation vs. higher level" as a whole.

| Concern | Owner | Rationale |
|---------|-------|-----------|
| Which **end user** is this, and what may they see? | **Higher level** | The foundation has no concept of a user or tenant. Modelling one here would put business semantics in an infrastructure layer, contradicting its stated non-goals ([01 §1.4](../01-scope-and-parity.md)) |
| Row / tenant filtering, field masking | **Higher level** | It knows the user; it decides *what* filter applies |
| Which **service** is calling? | **Foundation** | Not a business rule — a blast-radius control |
| May that caller reach this data source, with this action? | **Foundation** | Infrastructure policy: `analytics-api` gets `read` on `analytics`, never `write` on `main` |

Concretely:

1. **Caller authentication is required in shared deployments.** The service must
   be able to identify the calling service, not merely accept traffic from a
   network range.
2. **Coarse authorization is owned here**: `caller → {datasource} × {action}`,
   where action ∈ `read | write | list`. The existing
   `AuthGate.check(request, action, resource)` signature — already called with the
   data-source name as `resource` — fits this **unchanged**. No redesign; what is
   missing is a policy and a provider, not a seam.
3. **Ship a default provider, not only a port.** The foundation ships two small,
   boring authenticators so the secure configuration is reachable on day one:
   - **mTLS / mesh identity** — trust a caller-identity header asserted by the
     mesh or ingress (never accepted from an untrusted hop);
   - **signed token / API key** per calling service.
   An external auth project may still register richer adapters through the same
   ports; that path is unchanged. This is the specific reversal of ADR-013:
   *a seam with no implementation is a seam that never gets switched on.*
4. **Deployment-shape exemption.** Library mode, and a sidecar deployed 1:1 with a
   single caller, may run with `auth.enabled: false`. The exemption is about the
   process boundary being real, and it must be a recorded decision per deployment,
   not a default that drifts.
5. **Least-privilege database roles are mandatory regardless of auth**
   ([12 §12.5](../12-deployment-targets.md)). If the login role physically cannot
   write, an authorization defect degrades to a failed query rather than data
   loss. This is the control that survives a bug in our own authorization code, so
   it is not optional and not conditional on `auth.enabled`.
6. **Row-level enforcement, when it is eventually needed, belongs in PostgreSQL** —
   RLS with `SET LOCAL`, driven by a claim the higher level passes — not in
   application-side filtering. Deferred; noted so it is not reinvented.
7. **Authorization without audit is half a control.** `auth.denied` is a required
   signal ([11 §11.5](../11-observability-instrumentation-points.md)), and the
   subject — never the credential — is logged.
8. **Config fails closed at the profile level.** The prod profile asserts
   `auth.enabled: true`; `enable_raw_sql: true` combined with `auth.enabled: false`
   is refused outside dev ([05 §5.5](../05-configuration.md)). Raw SQL on an
   unauthenticated port is the one combination that must never ship by accident.

### Explicitly *not* added by this ADR

No identity provider, no token issuance, no user store, no session management, no
per-user RBAC, no tenant model, no consent or delegation flows. This is caller
authentication plus a coarse allow-list — a policy table and a signature check,
not an IAM system.

## Options considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Caller authN + coarse datasource×action authZ here; end-user authZ above (chosen)** | Bounds the blast radius at the layer holding the credentials; keeps business semantics out of infrastructure; fits the existing port signature with no redesign | The foundation ships a little security code it would rather not own | **Chosen** |
| Keep ADR-013 unchanged — seam only, trusted network | No work now; matches the inherited plan | The one asset worth protecting is protected only by network position, and the protection weakens exactly as adoption succeeds. The seam stays disabled indefinitely for want of a provider | Rejected for the shared-service shape; retained as the library/sidecar exemption (§4) |
| Full authorization here: per-user, per-row, per-tenant | One place to enforce everything | Requires the foundation to model users and tenants — business semantics in an infrastructure layer, and duplicated in every higher-level API that already has them | Rejected |
| Delegate everything to an API gateway | Centralized; no code here | A gateway protects the *edge*; it does nothing about in-cluster callers, and it cannot express "this service may read only this data source" without duplicating our data-source model | Rejected as sufficient; valid as an additional layer |
| Rely solely on least-privilege database roles | Strong, DB-enforced, survives our bugs | Cannot distinguish callers that share a connection pool — every caller through the service looks like one role. Necessary, not sufficient | Rejected as sufficient; **adopted as mandatory defence in depth** (§5) |

## Consequences

- **Positive:** the credential-holding layer stops being protected by network trust
  alone, and a compromised caller is contained to the data sources its policy
  names.
- **Positive:** no redesign — `AuthGate`, the ports, the fail-closed behaviour and
  the `401`/`403` mappings ([08 §8.3](../08-api-surface.md)) already exist. The
  delta is a policy table, two providers, and a phase to build them in.
- **Positive:** the split keeps the foundation free of user and tenant concepts, so
  the higher-level APIs remain the single place business authorization lives — no
  duplicated, divergent rules.
- **Negative / mitigation — the foundation now owns a little security code**, which
  ADR-013 deliberately avoided. Mitigated by keeping the scope to caller identity
  and a coarse allow-list, keeping both providers small and boring, and leaving the
  ports open for a richer external implementation.
- **Negative / mitigation — an operational burden**: every calling service needs an
  identity and a policy entry. Mitigated by making the policy plain config with a
  `default: deny`, and by the mTLS provider reusing mesh identity where a mesh
  already exists.
- **Consequence for the plan:** caller auth acquires a delivery phase
  ([13 · P4.8–P4.11](../13-implementation-plan.md)) instead of being deferred to an
  unowned project. **Until that phase lands, no second consuming API should be
  onboarded** — the current posture is safe only while the trusted network contains
  exactly one caller.
- **Consequence for adopters:** teams in library mode or a 1:1 sidecar are
  unaffected and may keep `auth.enabled: false` under §4.
