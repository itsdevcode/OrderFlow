# Interview walkthrough

This guide presents OrderFlow in about ten minutes, followed by questions for a
deeper technical discussion.

## Minute 0–2: problem and shape

OrderFlow is a transactional commerce API built with FastAPI, SQLAlchemy, and
PostgreSQL. It covers catalog management, cart checkout, inventory reservation,
payments, shipments, notifications, and audit events. Redis improves catalog
reads and coordinates rate limits; PostgreSQL remains the system of record.

Start with [the architecture diagram](architecture.md). Routes handle HTTP,
dependencies establish identity and roles, services own transactions,
repositories perform persistence, and adapters isolate Redis and payments.

## Minute 2–4: checkout concurrency

Open the sequence in [Business workflows](business-workflows.md). The service
locks products in identifier order and inventory in warehouse order. For each
product it chooses the first single warehouse with enough stock. It moves units
from available to reserved, creates price snapshots and expiring reservation
rows, clears the cart, and commits once.

Stable lock ordering reduces deadlock risk. Constraints protect nonnegative
quantities, while row locks stop concurrent checkouts from selling the same
units. The allocator deliberately does not split an item across warehouses.

## Minute 4–6: payment correctness

Payment initiation locks the order and derives the amount from snapshots. A
client idempotency key maps retries to the same payment. Partial unique indexes
allow at most one pending and one successful payment per order.

Webhooks authenticate exact request bytes and store a payload hash. Processing
locks the order, payment, reservations, and inventory in stable order. Success
confirms active reservations and the order atomically. Missing or expired
reservations produce a durable reconciliation state rather than silently
confirming the order. Duplicate events have exactly-once database effects; the
external provider is outside that guarantee.

## Minute 6–8: failure handling

Catalog mutations write invalidation intents in PostgreSQL before commit. Redis
invalidation uses Lua to delete detail keys and bump namespace versions. If
Redis is down, request-driven recovery leases pending outbox rows later. Reads
bypass Redis while invalidation is unsafe.

Notifications use a claim token and lease. A worker commits its claim before
delivery, then finalizes only if it still owns the token. A crash after an
external send can still duplicate delivery, so side-effecting adapters should
support provider idempotency.

## Minute 8–10: security, operations, and evidence

Passwords use Argon2 through `pwdlib`; access and refresh JWTs have short,
explicit lifetimes. Refresh records store identifiers rather than raw tokens.
Roles protect administrative work, ownership checks hide other users' resources,
and structured logs omit request content and SQL parameters.

The container runs as UID 10001 with a read-only filesystem and dropped Linux
capabilities. CI type-checks the application, tests migration downgrade and
upgrade, runs branch coverage, and smoke-tests the real container. The measured
suite has 248 passing tests; [Test coverage](test-coverage.md) records the exact
snapshot and limitations.

## Discussion questions

### 1. Why is PostgreSQL the system of record?

Orders, inventory, idempotency, webhook events, and recovery intents need
transactions and durable constraints. Redis holds replaceable acceleration and
coordination state, so core correctness survives its unavailability.

### 2. How does checkout prevent overselling?

It locks inventory rows, validates availability, and commits the stock movement
with the order and reservations. Concurrent checkouts serialize on shared stock.

### 3. Why acquire locks in a stable order?

Transactions needing overlapping rows are less likely to form a lock cycle when
they acquire product, inventory, and reservation rows in the same order.

### 4. Why snapshot product prices?

Catalog prices can change later. Snapshots preserve the agreed amount and make
payment totals reproducible.

### 5. What happens when payment initiation is retried?

A compatible idempotency key returns the existing payment. Reusing it for another
order or provider conflicts. Indexes constrain concurrent active attempts.

### 6. What does webhook idempotency guarantee?

A provider/event identifier and raw-body hash prevent repeated database
transitions. A different body for the same identifier is rejected. Provider
delivery remains outside the database transaction.

### 7. Why is reconciliation a first-class state?

Payment may succeed after reservation expiry. Reconciliation preserves evidence
and avoids claiming fulfillment when inventory confirmation failed.

### 8. What is the cache consistency model?

Versioned list keys, durable invalidation, and Lua fences close common races. An
in-flight read can return an older snapshot, and direct SQL can remain cached
until TTL, so cache reads are not linearizable.

### 9. Why put invalidation intents in the data transaction?

The data change and obligation to invalidate commit or roll back together. A
later recovery request can discover every committed obligation.

### 10. What happens when Redis is unavailable?

Catalog reads use PostgreSQL. Login and refresh rate limits fail closed with
`503`; webhook rate limiting fails open, while HMAC and database checks still run.

### 11. Can notification delivery be exactly once?

Not with a generic external adapter and database alone. A crash after provider
acceptance can cause a retry. Claim tokens prevent stale worker finalization;
provider idempotency is needed to suppress external duplicates.

### 12. Why store refresh-token identifiers rather than tokens?

The database can revoke and detect reuse without retaining usable bearer
credentials.

### 13. What are the main authorization boundaries?

Managers and admins manage catalog and stock; only admins create users; staff
manage shipments. Customers access their own carts, orders, shipments, and
notifications. Payment access is owner-or-staff.

### 14. What does the logging policy protect?

It admits fixed operational fields and excludes credentials, request content,
arbitrary extras, exception text, and SQL parameters.

### 15. What does the coverage number mean?

The recorded run executed 248 tests and covered 88.97% of lines and 73.40% of
branches with concurrency-aware tracing. It is a point-in-time measurement, not
proof that every production failure is covered.

### 16. What production work remains?

Add a real payment provider, scheduled recovery workers, TLS termination,
managed secrets, backups, metrics and tracing, and durable Redis where required.
Define retention and tamper-resistance controls for audit data.

### 17. Why disable proxy-header trust in Docker?

The service is safe when run directly. A trusted reverse proxy should have an
explicit forwarded-header policy rather than accepting headers from any peer.

### 18. Which design would you revisit first at larger scale?

Move request-driven recovery and notification dispatch into scheduled workers,
then measure inventory contention. Warehouse allocation and reservation expiry
are natural candidates for dedicated orchestration as throughput grows.

## Useful review paths

- [Architecture](architecture.md)
- [Database design](database-design.md)
- [Business workflows](business-workflows.md)
- [Reliability](reliability.md)
- [Security and observability](security-observability.md)
- [Operations](operations.md)
- [API reference](api-reference.md)
