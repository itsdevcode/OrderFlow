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
3. **Listing Invalidation in O(1)**:
   - Paginated listings are invalidated by atomically incrementing the namespace version counter via Redis `INCR` (e.g. `INCR orderflow:v1:products:version`).
   - Prior listing combinations become instant cache misses; their memory is automatically reclaimed by Redis when their TTL expires.
   - **Production Safety**: Completely eliminates blocking `KEYS *` commands from all production request and invalidation paths.
4. **Relational Consistency**:
   - Mutating a Category invalidates both the category detail, category listing version, AND product listing version. This prevents stale category-product listing relationships.

### 🛡️ Resilience & Graceful Degradation

- **Optional Startup**: Redis unavailability never prevents application startup. If unreachable, the service logs a sanitized warning and boots in degraded mode.
- **Fail-Open Reads**: When Redis is down, reads seamlessly fall back to PostgreSQL with zero HTTP 500 errors.
- **Fail-Safe Writes**: A Redis failure during cache invalidation never rolls back an already-committed database transaction.
- **Circuit Breaker Cooldown**: When a Redis connection timeout occurs, an internal circuit breaker triggers a cooldown period (default: 5 seconds). During cooldown, Redis is bypassed instantly without making network calls, preventing repeated socket timeout latency on client requests.
- **Secret Sanitization**: Redis connection URLs and error logs automatically mask credentials (`redis://user:***@host`).

### ⚠️ Eventual-Consistency Limitations & Concurrent Races

- **Concurrent Read/Write Race**:
  In high-concurrency environments, a reader task that encounters a cache miss might fetch old state from PostgreSQL just before a writer updates PostgreSQL and invalidates the cache. If the reader writes its old state back to Redis after the writer's invalidation, Redis temporarily holds stale data.
- **Consistency Bound**: TTL is the strict upper bound for staleness. Once TTL expires (or the next mutation occurs), consistency is restored. Strict cross-request serializability is not claimed for cached catalog reads.

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
