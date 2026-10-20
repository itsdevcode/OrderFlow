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
- **PostgreSQL** or **SQLite** (Async supported).

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
pip install -r requirements.txt # or pip install -e .
```

Create a `.env` file in the root directory:

```env
APP_NAME="OrderFlow API"
APP_VERSION="1.0.0"
DEBUG=True
DATABASE_URL="sqlite+aiosqlite:///./orderflow.db"
# For PostgreSQL:
# DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/orderflow"
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
uvicorn app.main:app --reload
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
   - When serving listing requests (`list_products`, `list_categories`), the service reads the namespace version before executing the database query and re-verifies `v_after == version` prior to writing to Redis (`cache.set`). If a concurrent mutation bumped the version while the database query was in-flight, the potentially stale query result is discarded rather than cached.

### 🛡️ Resilience, Atomic Invalidation & Multi-Worker Outage Recovery

- **Atomic Invalidation Intent (Transactional Outbox Pattern)**:
  - Durable cache-invalidation events (`CachePendingInvalidation`) are persisted in PostgreSQL in the **exact same transaction** as each Product/Category mutation.
  - The business entity change and the cache invalidation intent commit atomically. If the database transaction rolls back, no invalidation intent is recorded.
  - After commit, the application attempts Redis cache invalidation (`execute_staged_invalidations`).
  - Pending events are removed/acknowledged from PostgreSQL **only after Redis invalidation operations succeed**.
  - If the process crashes or Redis is unreachable before invalidation completes, the pending event is retained in PostgreSQL for recovery.
- **Fail-Safe Reconciliation & Stale-Read Prevention**:
  - `sync_pending_invalidations(db)` returns an explicit boolean result (`True` for success/no pending events, `False` on reconciliation failure or outage).
  - If pending invalidations cannot be reconciled (e.g. Redis remains down or DB locks fail), the catalog read (`get_product`, `list_products`, `get_category`, `list_categories`) **bypasses Redis entirely** for that request and reads directly from PostgreSQL.
  - This strictly prevents stale Redis entries from being served while unresolved invalidations may affect the catalog.
- **Startup Recovery & Bounded Reconnect Strategy**:
  - When Redis is unavailable during application startup, FastAPI lifespan logs a sanitized warning and boots in degraded mode with zero crashes.
  - Redis is **never mandatory** for normal API availability (`/health`, `/health/db`, and catalog endpoints operate seamlessly via PostgreSQL fallback).
  - The cache layer implements a bounded reconnect strategy (`ensure_connected` with a 5-second `reconnect_cooldown`). It bypasses connection attempts during cooldown to eliminate repeated socket timeout latency on client request threads, while automatically reconnecting and exiting degraded mode once Redis recovers without requiring application restarts.
- **Multi-Worker Concurrency Coordination**:
  - Concurrency between multiple API workers reconciling pending events is coordinated safely via `SELECT ... ORDER BY id LIMIT 100 FOR UPDATE`.
  - The recovering worker purges dirty keys and bumps namespace versions in Redis before serving the read, and deletes the reconciled database records only upon verified Redis success.
  - This architecture ensures that **recovered Redis can never serve known-stale cache entries across multiple worker processes**.
- **Circuit Breaker Cooldown**: When a Redis connection timeout occurs, an internal circuit breaker triggers a cooldown period (default: 5 seconds). During cooldown, Redis is bypassed instantly without making network calls, preventing repeated socket timeout latency on client requests.
- **Corrupt List Payload Rejection**: If cached list data in Redis contains any non-dict item or fails Pydantic schema validation, the service rejects the entire payload, purges the corrupted key from Redis, and falls back to PostgreSQL to retrieve fresh authoritative data. Partial lists are never returned.
- **Secret Sanitization**: Redis connection URLs and error logs automatically mask credentials (`redis://user:***@host`).

### ⚠️ Unavoidable Cache Consistency Limitations & TTL Bounds

In accordance with the CAP theorem and distributed systems design, Redis is an auxiliary, cache-aside read optimization layer, while PostgreSQL is the single authoritative source of truth. The following unavoidable consistency bounds apply:

1. **Transient Outage Window**:
   - Durable invalidation intent is committed atomically with mutations in PostgreSQL.
   - If Redis is unreachable, pending invalidations remain safely in PostgreSQL, and all subsequent catalog reads bypass Redis to fetch fresh data directly from PostgreSQL.
   - When Redis recovers, the very next read request through any API worker executes pending synchronization before reading or serving cache data, restoring consistency.
   - During the outage, reads fall back directly to PostgreSQL and receive fresh data.
   - If Redis recovers, the very next read request through any API worker executes pending synchronization before reading or serving cache data, restoring consistency.
2. **TTL as an Absolute Staleness Upper Bound**:
   - In catastrophic scenarios (e.g. concurrent network partitioning where both Redis invalidation and database pending-invalidation logging fail), every cached entry is bounded by its Time-To-Live (`CACHE_DEFAULT_TTL_SECONDS`, default: 300 seconds).
   - Stale data can never persist beyond this bounded window under any failure mode.
3. **Read-After-Write Eventual Consistency Window**:
   - In a cache-aside architecture, a microsecond window exists between the database commit and the subsequent Redis invalidation execution. Concurrent readers within this microsecond window may observe pre-mutation data before invalidation completes. Optimistic version guards (`v_after == version`) prevent in-flight queries from overwriting newer cache state.

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
