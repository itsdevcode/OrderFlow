# OrderFlow API

> **OrderFlow** is a production-grade Order & Inventory Management API built with **FastAPI**, **SQLAlchemy (Async)**, **Alembic**, and **Pydantic v2**.

---

## 🚀 Features

- **Asynchronous Architecture**: Full async database operations with SQLAlchemy 2.0 and `asyncpg` / `aiosqlite`.
- **Clean Layered Architecture**: Strictly separated layers for **API Routers**, **Services**, **Repositories**, **Models**, **Schemas**, and **Dependencies**.
- **User & Role Management**: RBAC roles (`ADMIN`, `MANAGER`, `CUSTOMER`) with password hashing using `pwdlib`.
- **Database Migrations**: Database schema management with **Alembic**.
- **Database Seeding**: Automated script to seed initial/default roles and reference data.
- **Health Checks**: Endpoints for basic service health and real database connectivity validation.
- **Type Safety**: Strictly typed Python codebase tuned for **Pyright**.

---

## 🛠️ Tech Stack

- **Framework**: [FastAPI](https://fastapi.tiangolo.com/)
- **ORM / Database**: [SQLAlchemy 2.0 (Async)](https://www.sqlalchemy.org/)
- **Migrations**: [Alembic](https://alembic.sqlalchemy.org/)
- **Validation & Settings**: [Pydantic v2](https://docs.pydantic.dev/) & `pydantic-settings`
- **Security**: `pwdlib` for password hashing
- **Python**: 3.11+

---

## 📁 Project Structure

```text
OrderFlow/
├── alembic/              # Database migration scripts
│   ├── versions/         # Migration versions
│   └── env.py            # Alembic async environment setup
├── app/
│   ├── api/              # API layer (FastAPI routers & endpoints)
│   │   └── v1/           # Version 1 endpoints (e.g., /users)
│   ├── core/             # Application configurations, security & constants
│   ├── db/               # Database session & initial seeding logic
│   ├── dependencies/     # FastAPI Dependency Injection (DB session, etc.)
│   ├── exceptions/       # Domain-specific custom exceptions
│   ├── models/           # SQLAlchemy ORM models
│   ├── repositories/     # Data access layer (Repository pattern)
│   ├── schemas/          # Pydantic data schemas (Request/Response validation)
│   ├── scripts/          # CLI scripts (e.g., seeding roles)
│   ├── services/         # Business logic layer
│   └── main.py           # Application entrypoint
├── .env                  # Environment configuration file
├── alembic.ini           # Alembic configuration file
├── pyproject.toml        # Project metadata and dependencies
└── README.md             # Project documentation
```

---

## ⚙️ Getting Started

### 1. Prerequisites

- **Python 3.11+** installed.
- **PostgreSQL 16** and **Redis 7.0** for integration tests (PostgreSQL-specific migrations and JSONB are required).

### 2. Environment Setup

Clone the repository and set up a virtual environment:

```bash
# Clone the repository
git clone https://github.com/your-username/OrderFlow.git
cd OrderFlow

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements-dev.txt
```

Create a `.env` file in the root directory:

```env
APP_NAME="OrderFlow API"
APP_VERSION="1.0.0"
DEBUG=True
DATABASE_URL="postgresql+asyncpg://orderflow_user:orderflow_password@localhost:5432/orderflow"
JWT_SECRET_KEY="change_me_in_production"
PAYMENT_WEBHOOK_SECRET="development_only_change_me"
```

---

## 🗄️ Database Setup & Migrations

### Run Migrations

To apply database migrations:

```bash
alembic upgrade head
```

### Seed Default Roles

Seed the database with initial roles (`ADMIN`, `MANAGER`, `CUSTOMER`):

```bash
python -m app.scripts.seed
```

---

## 🏃 Running the Application

Start the development server using **Uvicorn**:

```bash
uvicorn app.main:app --reload --no-proxy-headers
```

The API will be available at `http://127.0.0.1:8000`.

---

## 📖 API Documentation

Once the server is running, you can access the interactive API docs:

- **Swagger UI**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc**: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

### Summary of Available Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Basic application health check |
| `GET` | `/health/db` | Database connection health check |
| `POST` | `/api/v1/users` | Register a new user |
| `POST` | `/api/v1/orders/{order_id}/shipments` | Create shipment for confirmed, paid order (ADMIN/MANAGER) |
| `GET` | `/api/v1/orders/{order_id}/shipments` | Get shipment for order (ADMIN/MANAGER or order owner) |
| `GET` | `/api/v1/shipments/{shipment_id}` | Get shipment details (ADMIN/MANAGER or order owner) |
| `PATCH` | `/api/v1/shipments/{shipment_id}/status` | Update shipment lifecycle status (ADMIN/MANAGER) |
| `GET` | `/api/v1/shipments` | List shipments with pagination and status filter (ADMIN/MANAGER) |

---

## 📦 Shipment Module Architecture

### Shipment State Lifecycle

```text
[ Order CONFIRMED & Paid ]
           │
           ▼
       (PENDING)
       │       │
       │       └───► (CANCELLED)  [Terminal]
       ▼
   (SHIPPED)  [Requires carrier & tracking_number]
       │
       ▼
  (DELIVERED)  [Terminal]
```

- **Allowed transitions**:
  - `PENDING -> SHIPPED` (requires non-empty `carrier` and `tracking_number`)
  - `PENDING -> CANCELLED`
  - `SHIPPED -> DELIVERED` (preserves `carrier` and `tracking_number`)
- **Terminal states**: `DELIVERED`, `CANCELLED` (no further transitions allowed).
- **Idempotency**: Repeated requests targeting current state are idempotent when compatible with existing shipment details; incompatible replayed updates return `409 Conflict`.

### RBAC Authorization

| Action | ADMIN | MANAGER | CUSTOMER |
|---|:---:|:---:|:---:|
| Create shipment (`POST /api/v1/orders/{order_id}/shipments`) | ✅ | ✅ | ❌ (403) |
| Update shipment status (`PATCH /api/v1/shipments/{id}/status`) | ✅ | ✅ | ❌ (403) |
| List all shipments (`GET /api/v1/shipments`) | ✅ | ✅ | ❌ (403) |
| View shipment (`GET /orders/{id}/shipments` or `/shipments/{id}`) | ✅ | ✅ | ✅ (Own orders only, non-owned 404) |

### Concurrency and Integrity Strategy

1. **Row-Level Locking**:
   - Order row is locked with `SELECT ... FOR UPDATE` before shipment creation to prevent race conditions.
   - Shipment row is locked with `SELECT ... FOR UPDATE` during status transitions to prevent concurrent lost updates.
2. **Database Constraints**:
   - Unique constraint on `order_id` enforces strictly one shipment per order at the database level.
   - PostgreSQL partial unique index on `(carrier, tracking_number)` where both are non-null ensures no duplicate tracking identifiers within the same carrier.
3. **Transactional Isolation**:
   - Shipment operations never modify order status, payment status, or inventory reservation quantities.
   - All failures explicitly roll back database transactions.

### Limitations

- No physical shipping carrier gateway API integration or label printing in this release.
- Shipments are created via explicit authorized staff action rather than automated event hooks.

---

## ⚡ Redis Caching Architecture

OrderFlow implements an optional, production-grade read-performance caching layer powered by **Redis** (`redis.asyncio`). PostgreSQL remains the strictly authoritative source of truth, while Redis accelerates catalog reads for Products and Categories.

### 🏛️ Cache-Aside Architecture

Catalog read endpoints follow the standard Cache-Aside pattern:

```text
       Client Request (GET /products or GET /categories)
                         │
                         ▼
             [ Authenticate Current User ]
                         │
                         ▼
               [ Build Cache Key ]
                         │
                         ▼
                 Is Redis Online?
                /                \
           (Yes)                  (No)
              │                     │
      Attempt Cache GET             │
           /     \                  │
      (Hit)       (Miss)            │
        │           │               │
        │           ▼               ▼
        │        Query PostgreSQL Authoritative Store
        │           │               │
        │       Serialize           │
        │           │               │
        │      Populate Redis       │
        │      (Bounded TTL)        │
        │           │               │
        ▼           ▼               ▼
      Return Validated JSON API Response
```

1. **Authentication First**: Endpoint authentication/RBAC dependencies execute before cache evaluation. Unauthenticated or unauthorized callers never receive cached data.
2. **Deterministic Lookup**: A normalized, versioned cache key is computed from endpoints, resource IDs, pagination offsets/limits, and active filter flags.
3. **Cache Hit**: Validated JSON payload is deserialized and returned immediately without touching PostgreSQL.
4. **Cache Miss**: Authoritative data is fetched from PostgreSQL, validated against Pydantic response models, serialized, and written to Redis with a bounded TTL.
5. **Safe Deserialization**: Any malformed or corrupted JSON encountered in Redis is treated as a cache miss, purged, and refreshed from PostgreSQL without returning HTTP 500.

### 🔑 Cache Key Format

All cache keys are structured deterministically with a prefix namespace:

| Resource Type | Cache Key Format | Example |
|---|---|---|
| **Product Detail** | `orderflow:v1:products:detail:{product_id}` | `orderflow:v1:products:detail:42` |
| **Category Detail** | `orderflow:v1:categories:detail:{category_id}` | `orderflow:v1:categories:detail:5` |
| **Product Listings** | `orderflow:v1:products:list:v{version}:{query_params}` | `orderflow:v1:products:list:v3:category_id=5&is_active=true&limit=20&offset=0` |
| **Category Listings** | `orderflow:v1:categories:list:v{version}:{query_params}` | `orderflow:v1:categories:list:v1:limit=20&offset=0` |
| **Product Version Counter** | `orderflow:v1:products:version` | `orderflow:v1:products:version` |
| **Category Version Counter** | `orderflow:v1:categories:version` | `orderflow:v1:categories:version` |

- **Determinism**: Query parameters are sorted alphabetically, ensuring identical queries with different parameter orders hit the exact same cache entry.
- **Null Safety**: Unset or null query parameters are omitted to prevent key fragmentation.

### ⏳ TTL (Time-To-Live) Strategy

- Every cache entry is written with an explicit, bounded TTL (`CACHE_DEFAULT_TTL_SECONDS`, default: 300 seconds / 5 minutes).
- **TTL as Consistency Bound**: TTL guarantees that even in unhandled edge cases or network splits during invalidation, stale cache entries naturally expire within the bounded timeframe.
- Sensitive authentication tokens, payment records, cart items, and inventory reservations are **never** cached.

### 🔄 Invalidation Strategy

OrderFlow adopts a **Post-Commit Versioned Namespace Invalidation** strategy:

1. **Database Commit First**:
   - All mutations (`POST`, `PATCH`, `DELETE`) execute against PostgreSQL.
   - The database transaction MUST be successfully committed before any cache invalidation is attempted.
   - If database commit fails or rolls back, **no cache invalidation or mutation occurs**.
2. **Detail Invalidation**:
   - Explicit `DEL` on affected resource keys (e.g. `orderflow:v1:products:detail:{id}`).
3. **Listing Invalidation in O(1) & Version Initialization**:
   - Paginated listings are invalidated by atomically incrementing the namespace version counter via an atomic Redis Lua script:
     ```lua
     local current = redis.call('GET', KEYS[1])
     if not current then
         redis.call('SET', KEYS[1], 2)
         return 2
     else
         return redis.call('INCR', KEYS[1])
     end
     ```
   - **Absence Fix**: When the namespace version key is absent in Redis (implicitly defaulting to `1`), the first mutation transitions directly to `2` (instead of standard `INCR` initializing to `1`). This guarantees that the first mutation strictly invalidates any listing previously cached under default version `v1`.
   - Prior listing combinations become instant cache misses; their memory is automatically reclaimed by Redis when their TTL expires.
   - **Production Safety**: Completely eliminates blocking `KEYS *` commands from all production request and invalidation paths.
4. **Relational Consistency**:
   - Mutating a Category invalidates both the category detail, category listing version, AND product listing version. This prevents stale category-product listing relationships.
5. **In-Flight Query Race Guard**:
   - When serving listing requests (`list_products`, `list_categories`), the service captures the namespace version before the database query and uses a Lua compare-and-set (`set_if_version`) to check that version atomically with cache population. Detail population uses the same fence; detail deletion and version advancement run in one Lua operation. If a concurrent mutation bumped the version while the database query was in-flight, the potentially stale query result is discarded rather than cached.

### 🛡️ Resilience, Atomic Invalidation & Multi-Worker Outage Recovery

- **Atomic Invalidation Intent (Transactional Outbox Pattern)**:
  - Durable cache-invalidation events (`CachePendingInvalidation`) are persisted in PostgreSQL in the **exact same transaction** as each Product/Category mutation.
  - Intents are also staged while caching is disabled, so later enablement can reconcile retained Redis data.
  - The business entity change and the cache invalidation intent commit atomically. If the database transaction rolls back, no invalidation intent is recorded.
  - After commit, the application attempts Redis cache invalidation (`execute_staged_invalidations`).
  - Pending events are removed/acknowledged from PostgreSQL **only after Redis invalidation operations succeed**.
  - If the process crashes or Redis is unreachable before invalidation completes, the pending event is retained in PostgreSQL for recovery.
- **Fail-Safe Reconciliation & Stale-Read Prevention**:
  - `sync_pending_invalidations(db)` returns `ReconciliationOutcome.SUCCESS`, `FAILED`, or `CACHE_UNAVAILABLE`. Only `SUCCESS` permits Redis reads and writes.
  - If pending invalidations cannot be reconciled (e.g. Redis remains down or DB locks fail), the catalog read (`get_product`, `list_products`, `get_category`, `list_categories`) **bypasses Redis entirely** for that request and reads directly from PostgreSQL.
  - This strictly prevents stale Redis entries from being served while unresolved invalidations may affect the catalog.
- **Startup Recovery & Bounded Reconnect Strategy**:
  - When Redis is unavailable during application startup, FastAPI lifespan logs a sanitized warning and boots in degraded mode with zero crashes.
  - Redis is **never mandatory** for normal API availability (`/health`, `/health/db`, and catalog endpoints operate seamlessly via PostgreSQL fallback).
  - The cache layer implements a bounded reconnect strategy (`ensure_connected` with a 5-second `reconnect_cooldown`). It bypasses connection attempts during cooldown to eliminate repeated socket timeout latency on client request threads, while automatically reconnecting and exiting degraded mode once Redis recovers without requiring application restarts.
- **Multi-Worker Concurrency Coordination**:
  - Concurrency between multiple API workers reconciling pending events is coordinated safely via `SELECT ... ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED`. Claims have a token and a 15-second lease; the claim transaction commits before Redis network calls. Acknowledgement checks the token. Recovery uses a separate database session, so it cannot commit or roll back caller work. A request processes at most ten batches; an unresolved backlog causes PostgreSQL fallback.
  - The recovering worker purges dirty keys and bumps namespace versions in Redis before serving the read, and deletes the reconciled database records only upon verified Redis success.
  - A worker seeing outstanding claims bypasses Redis. Crashed workers lose their leases, permitting replay. Repeated deletion/version advancement is safe, although replay may advance a namespace more than once.
- **Circuit Breaker Cooldown**: When a Redis connection timeout occurs, an internal circuit breaker triggers a cooldown period (default: 5 seconds). During cooldown, Redis is bypassed instantly without making network calls, preventing repeated socket timeout latency on client requests.
- **Corrupt List Payload Rejection**: If cached list data in Redis contains any non-dict item or fails Pydantic schema validation, the service rejects the entire payload, purges the corrupted key from Redis, and falls back to PostgreSQL to retrieve fresh authoritative data. Partial lists are never returned.
- **Secret Sanitization**: Redis connection URLs and error logs automatically mask credentials (`redis://user:***@host`).

### ⚠️ Unavoidable Cache Consistency Limitations & TTL Bounds

PostgreSQL remains authoritative. This cache provides eventual consistency, not linearizable reads.

- A mutation may commit after a reader checks for pending work or after its cache GET starts. That reader can still return the previous value. A pending event prevents subsequent readers that observe it from using Redis until reconciliation succeeds.
- Detail and listing population are fenced against completed namespace invalidations. An in-flight database response can still be returned to its caller, but cannot repopulate cache after that namespace advances. Direct low-level `cache.set` callers do not get this protection.
- Each entry expires after its configured TTL (default 300 seconds) **from its last population**. TTL is not an absolute bound measured from the business commit. Redis eviction/reset of namespace counters while detail/list keys survive can also defeat version fencing; use a Redis eviction policy that preserves counters or clear the cache on administrative resets.
- Redis outages preserve durable intents. Recovery is request-driven and may wait for the reconnect cooldown, expired claims, or backlog draining. Failed acknowledgement retains the event for replay; successful Redis invalidation is safe to repeat.
- Catalog mutation services own their commit and reject explicit caller transactions and unrelated staged or flushed changes. Compose caller-owned transactions through repositories and stage invalidation intent before the caller commits. Repository helpers never commit. Supplied-session legacy invalidation wrappers stage intent only.

### ⚙️ Environment Variables

| Variable | Type | Default | Description |
|---|---|---|---|
| `REDIS_URL` | `str` | `redis://localhost:6379/0` | Redis connection URL |
| `CACHE_ENABLED` | `bool` | `true` | Globally enable or disable cache |
| `CACHE_DEFAULT_TTL_SECONDS` | `int` | `300` | Default expiration TTL in seconds (5 min) |
| `REDIS_CONNECT_TIMEOUT` | `float` | `2.0` | Socket connect timeout in seconds |
| `REDIS_SOCKET_TIMEOUT` | `float` | `2.0` | Socket read/write timeout in seconds |
| `TEST_REDIS_URL` | `str` | `redis://localhost:6379/1` | Redis database used for automated tests |

### 🧪 Verifying Cache Hits and Invalidation

Run the dedicated test suite:

```bash
# Run Redis integration test suite
pytest tests/test_redis_cache.py -v

# Run full test suite
pytest -v
```

---

## 🧪 Development & Quality

### Code Type Checking

Check type annotations using **Pyright**:

```bash
pyright
```

---

## 📜 License

This project is licensed under the MIT License.

## Redis-backed API rate limiting

Login, refresh, and payment webhooks use atomic fixed windows shared across API workers through the existing Redis pool. Catalog reads, logout, and authenticated account reads have no new limits. Logout remains available to revoke a session even when Redis is unavailable. These pre-authentication endpoints use a client address; neither an unverified JWT nor a supplied email changes its bucket. The limiter service also accepts verified `user:<id>` identities for future authenticated policies; this PR adds no such endpoint policy.

| Endpoint | Default requests/window per address | Redis outage policy |
| --- | --- | --- |
| `POST /api/v1/auth/login` | 10 / 60 seconds | 503; do not verify credentials or issue tokens |
| `POST /api/v1/auth/refresh` | 30 / 60 seconds | 503; do not rotate or revoke tokens |
| `POST /api/v1/webhooks/payments` | 120 / 60 seconds | Continue through mandatory HMAC verification, payload bounds, timestamp/replay checks, and PostgreSQL idempotency |

Exceeded limits return `429 {"detail":"Rate limit exceeded"}` with `Retry-After` rounded up to the next window boundary in seconds. Authentication outages return `503 {"detail":"Authentication temporarily unavailable"}` and `Retry-After: 5`. The limiter never accesses a database session. Normal authentication errors remain 401/403 below the threshold; an exhausted bucket returns 429 before business processing. Webhook limits include unsigned attempts while Redis is healthy. Providers must retry 429 responses after `Retry-After`.

Each Lua invocation reads Redis `TIME`, computes the UTC epoch-aligned window `[floor(time/window)*window, next boundary)`, and atomically checks/updates a capped counter with an absolute expiry at the boundary. At the exact boundary a new window starts. Denials do not extend the expiry. Worker clocks are irrelevant. Keys include a version, fixed endpoint scope, window duration, and SHA-256 of the canonical identity; tokens, passwords, emails, and raw addresses are absent. Counter keys expire automatically. Redis must allow `EVAL`, `TIME`, `HGET`, `HSET`, and `PEXPIREAT` for these keys.

`RATE_LIMIT_ENABLED` defaults to true. Limits and windows are configured separately with the `RATE_LIMIT_LOGIN_*`, `RATE_LIMIT_REFRESH_*`, and `RATE_LIMIT_WEBHOOK_*` settings in `.env.example`. Limits must be 1–1,000,000 and windows 1–86,400 seconds. Disabling catalog caching does not disable the Redis pool or security limits. Either Redis consumer keeps the existing pool enabled. Failed checks use a per-worker five-second cooldown; startup connection failure and later recovery use the shared bounded reconnect mechanism. There is no in-memory fallback counter that could be bypassed by switching workers.

### Client addresses and proxy deployment

The default identity is the ASGI socket peer, with IPv4-mapped IPv6 normalized to IPv4. `X-Forwarded-For` is ignored unless the socket peer belongs to `RATE_LIMIT_TRUSTED_PROXIES`, a JSON array of explicit IPs/CIDRs, for example `["10.20.0.5/32"]`. With a trusted peer the chain is walked right-to-left until the first untrusted address. Duplicate, malformed, oversized (over 4096 characters), or overly long (over 32 hops) headers fall back to the peer bucket. `Forwarded` and `X-Real-IP` do not influence identity. A missing/invalid peer uses one conservative shared bucket.

Run Uvicorn with **`--no-proxy-headers`**, e.g. `uvicorn app.main:app --no-proxy-headers`, so middleware cannot replace the actual peer before application trust checks. Configure trusted proxies here, restrict direct access to the backend, and make each trusted proxy append the real connecting client address or overwrite client-supplied headers. Never configure all Internet addresses as trusted proxies. An ASGI server that rewrites the peer upstream is outside this application's trust boundary.

### Security tradeoffs and limits

A fixed window permits up to twice the configured limit around a boundary. This is acceptable for the initial coarse abuse guard; endpoint-specific thresholds, password verification, token validation, and webhook signatures remain essential. Per-address limits can affect users behind the same NAT and do not stop distributed attacks or IPv6 address rotation. The webhook fail-open policy prioritizes provider delivery during Redis outages; existing signature verification and idempotency still apply, while infrastructure-level request/body limits remain necessary against volumetric attacks. Authentication intentionally sacrifices availability when the limiter is unavailable.

Redis eviction, data loss, or failover can reset counters. Use adequate capacity and an eviction policy that preserves security counters (prefer `noeviction`), monitor Redis errors, and size/tune thresholds for real provider traffic and shared client networks. Windows use Redis wall time, so Redis clock jumps affect boundaries. This is an application abuse guard, not a durable quota or a DDoS protection system. Changing the window duration starts a new counter namespace; changing limits mid-window applies the new threshold to the existing count.

## Transactional audit records and JSON logging

Critical operations stage an `AuditLog` in their existing `AsyncSession` before the business service commits. The audit helper and repository flush but never commit or roll back the caller's transaction. Mandatory audit insertion failure raises `AuditWriteError`; transaction-owning services roll back business mutations, pending notifications, and audit rows together. No second session is used for successful business events. Payment transition auditing also covers internal `PaymentService` calls; callers using `auto_commit=False` must own commit/rollback as before.

| Event | Actor | Safe metadata |
| --- | --- | --- |
| `AUTH.LOGIN_SUCCEEDED`, `AUTH.TOKEN_REFRESHED`, `AUTH.LOGOUT` | Verified persisted user | None |
| `AUTH.TOKEN_REUSE_DETECTED` | User associated with the verified stored refresh token | None; session revocations share the audit transaction |
| `AUTH.LOGIN_FAILED` | Anonymous | None; no submitted identity or credentials |
| `ORDER.CREATED` | Order owner | Item count |
| `INVENTORY.ADJUSTED` | Verified staff user for HTTP calls; nullable for system calls | Product/warehouse IDs, exact deltas and before/after quantities |
| `PAYMENT.SUCCEEDED`, `PAYMENT.FAILED` | System/webhook (`NULL`) | Internal order ID and status transition |

Payment events are staged only for actual state changes under the existing payment locks. Replayed webhook IDs and distinct callback IDs reporting an already-applied state do not duplicate payment audit events. Invalid signatures and rolled-back operations create no success audit. `FAILURE` outcomes describe failed authentication, token reuse, and a payment's failed business outcome; other listed actions use `SUCCESS`. Provider IDs, webhook signatures, secrets, JWT/JTI values, emails, passwords, free-text reasons, raw bodies, and client addresses are not audit metadata. Unknown metadata fields and non-allowlisted value types are rejected.

Failed authentication has no committed business mutation. It uses a separate, anonymous short transaction with an `AUDIT_FAILURE_TIMEOUT_SECONDS` deadline (default 1 second, maximum 5). This is best effort: database errors/timeouts produce a sanitized operational event and preserve the existing 401/403 rejection. Requests rejected by rate limiting or body validation before authentication do not create a failed-login audit. No successful business operation uses this best-effort path.

The `audit_logs` table stores action, resource type/ID, outcome, optional actor/request IDs, JSONB `metadata` (Python attribute `details`), and a timezone-aware creation timestamp. The nullable actor foreign key uses `ON DELETE SET NULL`, preserving history after user deletion. Actor/time, resource/time, and creation-time indexes support internal investigations. Repository reads are internal and bounded to 100 entries per page; this feature adds no public audit endpoint. Apply migration `c27e8a9410bf` after `a1b2c3d4e5f6`. Downgrading removes the audit table and its history; verify reversibility only in a disposable database.

### Request IDs and structured logs

Pure ASGI middleware generates a UUID request ID or accepts exactly one canonical, 36-character UUID header (case normalized). Missing, malformed, oversized, or duplicate `X-Request-ID` headers get a fresh UUID. Every HTTP response, including unexpected default 500 responses, includes `X-Request-ID`. Async contextvars propagate the ID to audit records and application logs and reset in a `finally` block. Client-provided request IDs are correlation hints, not authentication or uniqueness guarantees.

JSON stdout records contain `timestamp`, `severity`, `logger`, `event`, `request_id`, `http_method`, `path`, `status`, and `duration_ms`. Completed-request logs use the matched route template, e.g. `/orders/{order_id}`, rather than actual resource paths; unmatched paths are recorded as `<unmatched>`. Query strings, headers, cookies, bodies, exception text/tracebacks, arbitrary extras, and legacy interpolated messages are omitted. Only explicit operational event codes are emitted; legacy messages become `application.log`. HTTP status/duration/path may be null for logs emitted before the request completes or outside an HTTP context. Duration includes awaited background work, and late streaming/background errors retain the status already sent to the client.

`LOG_LEVEL` controls the application namespace (default `INFO`). Lifespan configures the formatter idempotently. Uvicorn's raw access logs are disabled in favor of the middleware's structured record; its error logger uses the same safe formatter so raw query strings and exception payloads cannot bypass it. Root and other third-party logger configurations are unchanged. SQLAlchemy parameter echo is disabled even when `DEBUG=true`, and engine error parameters are hidden. Custom deployment log handlers must preserve these privacy rules; do not enable raw request/SQL debug logging in production.

Audit rows are application-managed history, not a tamper-proof ledger: a database owner can modify them. No retention/export policy or administrative UI is included. Anonymous failed-login audits may be lost during an audit database outage. Background tasks deliberately spawned outside the awaited request lifecycle need their own explicit context/transaction policy. Success audits prove a committed business transition, not that the client received its response.

## Continuous integration

`.github/workflows/ci.yml` runs on pull requests targeting `main`, pushes to
`main`, and manual dispatch. The `Tests, typing and migrations` job uses Python
3.12, PostgreSQL 16 and Redis 7.0 on Ubuntu 24.04, with a 25-minute timeout.
It installs pinned pip dependencies from `requirements-dev.txt` (which includes
`requirements.txt`) and caches downloads using both files. Update these pins
intentionally and verify with a clean virtual environment; no Poetry/uv project
or editable installation is configured.

The separate `Docker Compose smoke checks` job builds the runtime image and
exercises a unique disposable Compose project, including startup, readiness,
Redis policies, data durability, failed migrations and graceful shutdown. It
does not publish images or deploy the application.

The required quality sequence is dependency consistency, Settings/service
validation, migration smoke checks, `pyright app`, and the entire `pytest -v`
suite. No tests are filtered or run in parallel. Existing fixtures recreate the
test schema per test and close Redis connections after each test's event loop.
The workflow has read-only repository permission, uses official Actions, and
uses CI-only credentials without deployment or publishing steps. Superseded
runs are cancelled only for the same PR; unrelated PRs have separate groups.

### Run the same checks locally

Use disposable PostgreSQL 16 and Redis 7.0 instances on localhost. Create an
**empty** `orderflow_migrations` database owned by a role with `CREATEDB` (CI uses
`orderflow_ci` / `ci_only_password`). Leave `orderflow_test` absent: the service
check creates it and refuses to reuse an existing database. Do not point these
commands or pytest at shared resources: tests drop tables and flush Redis DB 1.

```bash
python3.12 -m venv venv
source venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip check

export DATABASE_URL=postgresql+asyncpg://orderflow_ci:ci_only_password@127.0.0.1:5432/orderflow_migrations
export TEST_DATABASE_URL=postgresql+asyncpg://orderflow_ci:ci_only_password@127.0.0.1:5432/orderflow_test
export REDIS_URL=redis://127.0.0.1:6379/0
export TEST_REDIS_URL=redis://127.0.0.1:6379/1
export DEBUG=false
export JWT_SECRET_KEY=ci_only_jwt_key_never_use_in_production
export JWT_ALGORITHM=HS256
export PAYMENT_WEBHOOK_SECRET=ci_only_webhook_key_never_use_in_production
export CACHE_ENABLED=true
export RATE_LIMIT_ENABLED=true
export RATE_LIMIT_TRUSTED_PROXIES='[]'

python -m scripts.ci_services
alembic heads
python -m scripts.ci_migrations
pyright --pythonpath "$(which python)" app
pytest -v --junitxml=test-results.xml
```

Other workflow environment values explicitly mirror Settings defaults. The
`--pythonpath` option makes Pyright inspect the active interpreter's packages,
including in CI where the repository's optional local `venv/` does not exist.
Environment variables override `.env`; `DEBUG` must be a boolean such as
`false`, never `release`. CI explicitly supplies the webhook secret so Settings
validation runs with debug disabled.

The migration helper refuses remote or differently named databases and requires
an empty public schema. It rejects multiple migration heads, runs
`alembic upgrade head`, compares `alembic_version` to the script head, prints
`alembic current`, downgrades the latest revision to its parent, verifies that
revision, then upgrades and verifies head again. This is destructive smoke
testing only on the dedicated disposable migration database. Integration tests
use a different database because their metadata fixtures do not apply Alembic
migrations. To repeat this fresh-database check locally, recreate both disposable
databases first; never downgrade an application database for this purpose.

### Troubleshooting GitHub Actions

Open the PR's **Checks** tab and expand the first failing step. Container health
failures usually indicate service startup problems; inspect the service logs.
The configuration check fails early if PostgreSQL or either Redis database is
unreachable, including when tests might otherwise tolerate a Redis outage.
Installation failures should be reproduced in a clean Python 3.12 environment;
`pip check` identifies dependency conflicts. Migration failures show revision
transitions; inspect the failing revision and keep the migration graph linear.
Type failures must be fixed in code, and pytest prints failing cases and
tracebacks. Do not paste credentials or complete environment dumps into logs.
`test-results.xml` is produced in the runner workspace for tooling; artifacts
are not published by this workflow.

GitHub branch protection is configured separately: require
**Tests, typing and migrations** before merging. Adding this workflow does not
change repository protection rules. A passing local run does not establish a
passing GitHub Actions run; inspect the actual PR run before approval.

## Docker development environment

Install Docker Engine with the Compose plugin (Compose 2.20+ or newer) or
Docker Desktop with WSL integration enabled on Windows. Docker must be running;
`docker version` and `docker compose version` should succeed. No host Python,
PostgreSQL or Redis installation is needed for the application stack.

```bash
cp .env.example .env
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 api
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/health/db
```

`.env.example` now contains local Docker settings. Keep `.env` untracked. The
example passwords and signing keys are development placeholders: replace them
before any deployment. `POSTGRES_*` provisions the database on an empty volume;
`DATABASE_URL` must contain matching credentials and use `postgres:5432`.
Percent-encode reserved characters in URL passwords. `REDIS_URL` uses
`redis:6379/0`. Host-based Python commands need localhost URLs and separate
host services; Compose does not publish database/cache ports. Test URLs in the
example are for isolated host tests and are not passed into the API container.
Use `docker compose config --quiet` for validation; plain `config` prints
resolved credentials, so do not publish its output.

The default API is bound to `127.0.0.1:8000`; use `API_PORT` to avoid a local
port collision. PostgreSQL and Redis communicate on Compose's private service
network without host port mappings. PostgreSQL uses a named `postgres_data`
volume scoped to the Compose project. Redis deliberately disables RDB and AOF
persistence; cache and rate-limit state is lost when Redis restarts. Redis uses
a 128 MB cap with `noeviction` to avoid silently evicting active security
buckets; memory exhaustion follows the existing Redis-unavailable policies.
This is a local durability choice, not a production security or HA guarantee.

The multi-stage Python 3.12 image installs the pinned runtime requirements using
binary wheels; development/test tools and compiler packages are absent. Build
context is allowlisted by `.dockerignore`, and only application/migration files
are copied. The API runs as UID 10001 with dropped capabilities, no privilege
escalation, a read-only root filesystem and writable temporary memory storage.
Uvicorn runs directly as the container process, binds to `0.0.0.0:8000`, uses no
reload, and has 25 seconds to drain before Compose's 30-second stop deadline.
Proxy-header rewriting is explicitly disabled so application rate limiting sees
the socket peer. The application separately trusts only the proxy CIDRs in
`RATE_LIMIT_TRUSTED_PROXIES` (empty by default). Configure exact trusted proxies
and TLS at the ingress for a real deployment; never blindly trust all clients.

### Startup, migrations and health

Compose waits for PostgreSQL and Redis health checks. The one-shot `migrate`
service runs `alembic upgrade head` after PostgreSQL becomes healthy, and the API
starts only after that service exits successfully. Migration failures leave the
API stopped. Migrations run once per Compose startup operation, outside API
replicas; coordinate a single migration release job for deployments spanning
multiple Compose projects or hosts. Do not scale the migration service.

```bash
# View migration output and current revision.
docker compose logs --tail=100 migrate
docker compose run --rm migrate alembic current

# After pulling application/schema changes: stop traffic, migrate, then rebuild.
docker compose stop api
docker compose build
docker compose run --rm migrate alembic upgrade head
docker compose up -d --force-recreate migrate api

# Logs and normal service operations.
docker compose logs --tail=100 api postgres redis
docker compose restart api
docker compose stop
docker compose up -d
docker compose down
```

Run the update commands sequentially and stop if migration fails. `restart`
does not rebuild images or apply changed environment variables; use `up -d
--build` when configuration or source changes. Existing volumes are retained by
`down`. Do not use automatic downgrades: some migrations destroy data. Back up
and review schema/application compatibility before any rollback. A named local
volume is not a backup; production needs tested backups, restoration, access
controls, secret management, monitoring and a coordinated deployment process.
Images track Python 3.12, PostgreSQL 16 and Redis 7.0 patch releases; pin reviewed
image digests in your release process when immutable base images are required.

`/health` reports process liveness. `/health/db` executes `SELECT 1` and is the
container readiness health check. Uvicorn serves traffic after lifespan startup;
Compose additionally gates it on completed migrations. A separate readiness
route is unnecessary for this setup. Redis is excluded from database readiness:
after startup, catalog operations continue against PostgreSQL during a Redis
outage, login/refresh return 503 with Retry-After, and payment webhooks retain
HMAC authentication and database idempotency. Compose initially requires Redis
to be healthy, but does not stop a running API when Redis later becomes
unavailable. A Redis restart resets rate-limit windows; stronger continuity
requirements need a durable, secured Redis deployment.

### Tests and disposable resets

The standard image intentionally excludes test tools. Run the full host suite
with the isolated PostgreSQL/Redis instructions in Continuous integration:

```bash
python -m pip install -r requirements-dev.txt
pyright --pythonpath "$(which python)" app
pytest -v

# Requires a running Docker engine; creates its own project and volumes.
python3 -m scripts.docker_smoke
```

The smoke script builds and runs a unique `orderflow-smoke-*` project on port
18000, seeds disposable data, checks real cache/rate-limit HTTP behavior, restarts
and recreates PostgreSQL, exercises Redis outage/recovery, injects invalid DB
credentials and a failed migration, then verifies recovery and graceful stop.
It removes only its own generated project's containers and volumes in cleanup.
CI runs it separately from the complete integration suite.

For a manual throwaway sandbox, choose a unique project name **before its first
startup** and use it on every command:

```bash
# Only use this name for a new disposable sandbox, never existing valuable data.
docker compose -p orderflow-disposable up --build -d
# Destructive: deletes only that sandbox's database volume. Review the name first.
docker compose -p orderflow-disposable down --volumes
```

Never reset an existing developer or production project without explicit approval.

### Docker troubleshooting

- If the API does not start, inspect `docker compose ps -a` and `docker compose
  logs --tail=100 migrate postgres`. Fix the migration/configuration error before
  recreating `migrate` and `api` with `up -d --force-recreate migrate api`.
- If health checks fail, verify `DATABASE_URL` uses `postgres`, the URL password
  matches the initialized volume, and Redis uses `redis`. Changing
  `POSTGRES_PASSWORD` in `.env` does not rotate an existing database password;
  rotate it deliberately rather than deleting a valuable volume.
- If port 8000 is occupied, set `API_PORT` in `.env`. If Docker is unavailable in
  WSL, enable this distribution in Docker Desktop's WSL integration settings.
- If Redis is unavailable, catalog reads can succeed while login/refresh return
  503. Restore Redis; connection recovery is bounded and may take several seconds.
- Images contain no default users or business data; provision accounts through
  the existing application administration workflow. The smoke-only seeded admin
  is confined to a disposable project.

This Compose setup is a reproducible development baseline, not a managed
production deployment. Production needs TLS, external secret injection, reviewed
network access, durable backups and an appropriate Redis availability policy.
