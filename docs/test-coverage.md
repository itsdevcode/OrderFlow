# Day 6 coverage investigation

Base: `4d6b613` (fetched origin/main). The existing clean
`test/integration-coverage` branch already points to this commit.

## Coverage-gap matrix (before implementation)

| Module | Existing coverage | Missing edge cases | Business risk | Proposed test | Priority |
| --- | --- | --- | --- | --- | --- |
| Authentication | Login, rotation, replay revokes all tokens, logout persistence, mandatory audit rollback | Concurrent rotation; expired JWT; disabled user with issued tokens; malformed signed claims | Multiple usable refresh descendants; disabled accounts retain access | Synchronize two independent sessions at user lookup; assert persisted tokens and audits; exercise expired and disabled credentials | P0 |
| Inventory | Five repeats each of competing orders, multi-quantity and multi-product races; negative adjustment; multi-item rollback; reservation release/expiry | Concurrent terminal reservation transitions | Stock restored or consumed twice | Barrier-start independent release/confirm sessions; verify one terminal outcome and conserved stock | P1 |
| Orders | Empty/inactive carts, ownership, snapshots, insufficient stock, injected clear-cart failure, audit and notification failure | Actual commit rejection after cart clearing and audit staging | Partial order, lost cart or reserved stock | Reject commit via PostgreSQL deferred constraint; verify all related tables and retry | P0 |
| Payments/webhooks | HMAC/tampering, replay, conflicting payload, concurrent duplicate events, inventory rollback, payment audit rollback | One assertion spanning concurrent event, audit, notification and inventory effects; commit failure followed by retry | Duplicate observable effects or missing retry | Verify all persisted effects across concurrent deliveries; force commit failure and retry same event | P0 |
| Redis | Hits/misses, TTL, invalidation, outage/recovery, independent workers, durable outbox, race fencing, atomic rate limits and Retry-After | No high-risk gap identified | Stale catalog/security bypass | Retain existing real Redis regression tests | Covered |
| Audit/logging | Mandatory audit failures, FK insert failure, transaction visibility, concurrent webhook audit, isolated request IDs, secret exclusion | Concurrent refresh success/replay audit counts | Misleading security trail | Assert security audit counts alongside concurrent token state | P0 |
| Shipments/notifications | Valid/invalid transitions, duplicate creation, update race, constraints, rollback, retry ceiling, deduplication, recovery and worker fencing | No high-risk gap identified | Duplicate dispatch or invalid fulfillment | Retain existing lifecycle and recovery tests | Covered |
| Infrastructure | Function-loop Redis cleanup; NullPool; independent HTTP sessions; real PostgreSQL schema per test | Destructive fixtures accept arbitrary resource URLs; cov installed locally but absent from dev requirements/CI | Accidental non-test resource deletion; unreproducible coverage | Fail before resource creation unless local dedicated test DB/Redis; pin coverage dependency and report in CI | P0 |

## Infrastructure decisions

Keep function-scoped schema recreation: tests commit real transactions and use
independent sessions, so a shared rollback fixture would hide concurrency behavior.
Keep NullPool and function-scoped Redis shutdown to avoid event-loop reuse.
The suite is serial: parallel pytest workers would share and drop the same schema.
Use barriers/events with bounded waits for new concurrency tests, with fresh
observer sessions for persistence assertions. Never use production resources.

## Results

- Original suite: **230 passed** in 509.39 seconds with test-only worker
  session binding. The first coverage report lacked greenlet tracking and is
  unsuitable for comparing coverage improvements.
- New regressions: **18 passed** in 23.39 seconds; total suite now collects 248.
- Confirmed defect: two concurrent refresh requests could both rotate one token.
  Refresh/logout now acquire a per-user PostgreSQL `FOR NO KEY UPDATE` lock
  before reading token state, retaining the lock through audited commit.
- Mutation checks: reverting the refresh fix caused the concurrency assertion
  to fail; disabling JWT expiry verification caused both expiry tests to fail.
  Both temporary changes were restored completely.
- Pyright: **0 errors, 0 warnings, 0 informations**. Alembic: one head,
  `c27e8a9410bf`. Dependency consistency check passed.
- Full final-suite, corrected baseline/final coverage and GitHub Actions
  verification are pending. No success is claimed for interrupted test runs.

Coverage now tracks `greenlet` and `thread`, as required by the
[coverage.py configuration reference](https://coverage.readthedocs.io/en/7.10.7/config.html#run-concurrency).
No coverage exclusions were added or changed. Baseline and final measurements
use the same tracking configuration; line and branch percentages will be
calculated separately from executed statements and branch counts.

## Remaining testing risks

- Tests require dedicated local PostgreSQL `orderflow_test` and Redis database 1.
  Shared-schema pytest parallel workers are unsupported.
- Notification lease recovery requires a scheduler or explicit redispatch.
  Provider idempotency remains necessary for a crash after external delivery.
- Logout revokes refresh tokens; issued stateless access tokens retain their
  configured lifetime.
- Local Docker execution is unavailable in this WSL environment. The existing
  Docker Compose smoke job remains required for runtime verification.
