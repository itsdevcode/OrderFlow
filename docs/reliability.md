# Redis and reliability

[Architecture](architecture.md) · [Workflows](business-workflows.md) · [Operations](operations.md)

PostgreSQL is authoritative for catalog and business data, refresh revocation,
notification state and invalidation intent. Redis stores disposable catalog
responses and shared admission counters. PostgreSQL writes do not depend on a
successful cache write.

## Catalog read path and keys

[Product](../app/services/product.py) and [category](../app/services/category.py)
reads use [CacheService](../app/services/cache.py). Authentication runs before
catalog responses; caching does not bypass active-user/role dependencies.
Only these catalog response paths are cached; orders and user-specific carts are
not served from a shared response cache.

A read first reconciles pending invalidations through an independent database
session. On successful reconciliation it obtains the namespace version, attempts
Redis, validates cached JSON with the response schema, and otherwise reads
PostgreSQL. Invalid JSON or invalid response shapes are purged and treated as a
miss. Population happens only if the namespace version remains unchanged.

| Example | Purpose |
| --- | --- |
| `orderflow:v1:products:detail:42` | Product detail; stable key |
| `orderflow:v1:products:version` | Namespace counter, initialized to 1 |
| `orderflow:v1:products:list:v3:limit=20&offset=0` | Listing under namespace version 3 |
| `orderflow:v1:categories:detail:7` | Category detail |

Keys sort query parameter names, omit `None` and normalize boolean values.
Listing filters, including category selection where supplied, are represented in
the key. TTL defaults to 300 seconds. Detail invalidation deletes explicit keys;
list invalidation advances the namespace version, so obsolete lists become
unreachable and expire without a Redis `KEYS` scan.

## Durable invalidation and crash recovery

A catalog mutation stages a `cache_pending_invalidations` row **before** committing
its product/category change. Failure to stage the intent rolls back the write.
Product mutations stage the product detail key and products namespace. Category
mutations stage its detail/categories namespace plus a products namespace event.
Intent is retained even when caching is disabled, for future enablement.

After commit, the service attempts reconciliation. Any API worker can later
reconcile before a catalog read. The recovery implementation:

1. Opens its own session, preserving the caller's transaction.
2. Claims up to 100 rows by ascending ID using `FOR UPDATE SKIP LOCKED`, assigning
   a UUID token and time; the default lease is 15 seconds.
3. Commits the claim before network I/O.
4. Deletes detail keys and advances each affected namespace using a Lua script.
5. Acknowledges/deletes rows only after Redis success, fenced by claim token.

It processes at most ten batches per attempt. An operational failure releases
claims when possible; a crashed worker's expired claims can be reclaimed. Redis
invalidation may be repeated after a crash before acknowledgment, safely causing
extra misses/version bumps rather than lost intent. There is no separate outbox
worker deployment: this reconciliation is request-driven.

If another worker has in-flight pending rows, reconciliation fails, reaches its
batch bound, or Redis is unavailable, catalog services bypass both cache reads
and population and use PostgreSQL for that request. That prevents a worker from
serving old Redis data merely because another worker claimed the relevant intent.

## Population race protection and limits

A version check after the SQL read narrows the race, and `set_if_version` performs
the final comparison and SET in one Lua script. Invalidation's deletion and version
bump are also atomic relative to that script. An invalidated snapshot cannot be
repopulated under the old version after the bump.

There is still no atomic transaction spanning a SQL commit and Redis. A read
already in progress may return a pre-commit snapshot; the outbox/version fence
does not make every read linearizable with a concurrent catalog update. Direct
SQL mutations that bypass services also bypass invalidation intent; cached data
can remain stale until its TTL or explicit invalidation. Keep writes within the
catalog service boundary.

## Redis outage and recovery

The pool is initialized if either caching or rate limiting is enabled. Application
startup tolerates Redis connection failure. Helpers use bounded socket/connect
timeouts (both default to two seconds), a reconnect cooldown, and cache/limiter
cooldowns of five seconds to avoid repeated network attempts. Startup tolerance
is distinct from Compose's initial healthy-Redis dependency gate.

| Path during Redis outage | Behavior |
| --- | --- |
| Catalog reads | Bypass cache and query PostgreSQL |
| Catalog mutations | Commit database change with durable invalidation intent; replay later |
| Login and refresh with limits enabled | 503, `Retry-After: 5`, before token mutation |
| Payment webhook limiter | Continue to body limits, HMAC, timestamp rules and PostgreSQL idempotency |
| Orders, inventory, shipments | Their business transactions remain PostgreSQL-backed; they are not directly Redis-gated |
| Other protected routes | Valid access JWT/user checks remain usable without a Redis token lookup |

A missing startup pool can reconnect on a later request. `CACHE_ENABLED=false`
does not disable the security pool when rate limiting is enabled. Redis recovery
reconciles retained intents before cache use; it does not reconstruct old limiter
windows. Compose disables Redis RDB/AOF, so a restart loses counters and cache.

## Rate limiting

[RateLimiter](../app/services/rate_limit.py) uses one atomic Lua script with Redis
`TIME`, an epoch-aligned fixed-window bucket, count and absolute expiration at the
next boundary. Every worker uses the same shared clock and key. The admitted
request that reaches the configured limit succeeds; the next request is denied.
Denied requests neither increase the count nor extend the window.

| Scope | Default admitted requests / window |
| --- | --- |
| Login | 10 / 60 seconds |
| Refresh | 30 / 60 seconds |
| Payment webhook | 120 / 60 seconds |

Keys are `orderflow:rate:v1:{scope}:{window_seconds}:{sha256(identity)}`. Identity
comes from a trusted client IP, not arbitrary JWT claims, login email or a supplied
header. 429 returns the ceiling of seconds until reset in `Retry-After`. These
fixed windows allow bursts across a boundary; there is no sliding-window or
per-account distributed limit in the current implementation. Clients sharing NAT
can share a bucket.

The default is socket-peer identity. `X-Forwarded-For` is considered only if that
peer matches explicitly configured trusted proxy IPs/CIDRs, with a bounded chain
walk from right to left. Malformed/duplicate/oversized forwarding metadata falls
back to the peer. Keep Uvicorn `--no-proxy-headers` so a second layer does not
rewrite the trusted socket peer before this logic runs.

## Notifications and retry boundaries

[NotificationService](../app/services/notification.py) inserts a durable unique
`deduplication_key` with the business transaction. FastAPI schedules in-process
dispatch after commit, using a fresh session. The worker commits a claim and
increments attempts, delivers outside a transaction, then finalizes only if its
claim token still matches. Default maximum attempts is three; lease duration is
300 seconds. SENT records cannot be reclaimed. FAILED and expired PROCESSING
records can be eligible for another explicit dispatch while below the attempt cap.

A lease expiring does not itself launch another attempt. No scheduler, queue
consumer, retry API or automatic sweep is supplied. A future operator-owned
sweeper can invoke the existing dispatch method. Token fencing stops an older
worker overwriting a newer claim's result; it does not cancel an old external
send or provide external exactly-once delivery. The included in-app adapter does
not call email/SMS providers.

Webhook retry correctness and reservation expiry limitations are described in
[business workflows](business-workflows.md). Provider reconciliation remains an
operational responsibility when a real payment cannot safely confirm inventory.
