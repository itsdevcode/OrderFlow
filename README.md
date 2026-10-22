# OrderFlow

OrderFlow is a FastAPI order and inventory API that coordinates carts, stock
reservations, payment callbacks, and fulfillment. PostgreSQL owns business state;
Redis accelerates catalog reads and coordinates API rate limits.

The engineering focus is consistency under concurrent requests and retries:
prevent overselling, preserve order snapshots, apply payment effects once in the
database, and keep audits and notifications in the same business transaction.
This repository is an engineering reference and a local deployment baseline;
[production limitations](docs/security-observability.md#production-limitations)
are explicit.

## Engineering highlights

- JWT authentication, persisted refresh rotation/replay detection, and RBAC for
  `ADMIN`, `MANAGER`, and `CUSTOMER`.
- PostgreSQL row locks for stock reservation, refresh mutations, payment attempts,
  shipment transitions, and notification claims.
- Atomic order creation with inventory reservations, immutable item snapshots,
  cart clearing, an audit event, and a durable notification.
- HMAC-authenticated payment webhooks with event identity, payload hashing,
  reconciliation states, and transactional duplicate protection.
- Versioned catalog caching with a PostgreSQL invalidation outbox, Lua population
  fencing, and fallback during Redis outages.
- Redis-clock fixed-window rate limits; login and refresh fail closed when the
  shared limiter is unavailable.
- Structured JSON request logs, UUID correlation, and mandatory transactional
  audits for selected security and business events.
- Real PostgreSQL/Redis regression tests, Pyright, migrations, and Docker smoke
  checks in GitHub Actions.

## Architecture at a glance

```mermaid
flowchart LR
    Client[HTTP client] --> API[FastAPI routers and dependencies]
    API --> Services[Business services]
    Services --> Repos[Repositories and async SQLAlchemy]
    Repos --> PG[(PostgreSQL)]
    Services --> Redis[(Redis cache and rate limits)]
    Services --> Provider[Fake payment provider]
    Services --> Tasks[In-process background notification task]
    Tasks --> PG
```

Start with the [architecture](docs/architecture.md), then follow
[business workflows](docs/business-workflows.md). The
[10-minute interview walkthrough](docs/interview-walkthrough.md) provides a
short presentation and code-grounded questions.

## Verified stack

Versions below come from [requirements.txt](requirements.txt),
[requirements-dev.txt](requirements-dev.txt), [Dockerfile](Dockerfile), and
[compose.yaml](compose.yaml). They describe this checkout, rather than the latest
upstream releases.

| Component | Repository version |
| --- | --- |
| Python | 3.12 in Docker and CI |
| FastAPI / Uvicorn | 0.142.2 / 0.54.0 |
| SQLAlchemy / asyncpg | 2.1.1 / 0.31.0 |
| Alembic | 1.20.0 |
| Pydantic / pydantic-settings | 2.13.5 / 2.15.0 |
| PostgreSQL / Redis server | Docker image families `postgres:16` / `redis:7.0` |
| Redis Python client | 8.1.0 |
| PyJWT / pwdlib | 2.15.1 / 0.3.1; Argon2 password hashing |
| pytest / pytest-asyncio / pytest-cov | 9.1.1 / 1.4.0 / 7.0.0 |
| Pyright | 1.1.414 |

PostgreSQL is required: the implementation uses JSONB, partial unique indexes,
row locks, and PostgreSQL conflict handling. SQLite is not a supported substitute
for these integration tests.

## Repository structure

```text
app/
  api/v1/          HTTP routes
  api/background.py  Notification task with its own session
  core/            Settings, JWT, hashing, Redis and JSON logging
  dependencies/    Authentication, permissions, session and limiter injection
  middleware/      Request correlation and safe HTTP completion logs
  services/        Business rules and transaction ownership
  repositories/    Queries, locks, conflict handling and flushes
  models/          SQLAlchemy entities and constraints
  schemas/         Pydantic request/response models
  db/              Async session factory, metadata and role seeding
  scripts/         Role-seeding entry point
alembic/versions/   Schema history
scripts/           Disposable CI migration and Docker smoke checks
tests/             Unit and real-service integration regressions
docs/              Architecture, operations and interview reference
```

## Quick start with Docker Compose

Requires Git and a working Docker engine with Compose v2. These commands create a
local development stack; review existing resources before reusing a project.

```bash
git clone https://github.com/itsdevcode/OrderFlow.git
cd OrderFlow
cp .env.example .env
docker compose up --build -d
docker compose ps
docker compose logs migrate
curl --fail http://127.0.0.1:8000/health/db
docker compose exec api python -m app.scripts.seed
```

The checked-in example contains **local-only** credentials. Replace signing
secrets for any deployment. Compose publishes only the API, on localhost port
8000 by default; PostgreSQL and Redis remain on the Compose network. An empty
PostgreSQL volume is initialized from `POSTGRES_*`, then the one-shot migration
service must succeed before the API starts. Role seeding creates roles only.

There is no seeded user and no public registration endpoint. Follow the
[first-admin bootstrap](docs/operations.md#first-admin-bootstrap), then use the
admin-only user API to provision other users.

```bash
docker compose logs --tail=100 api
docker compose stop
docker compose start
```

`docker compose down` removes containers/network while retaining the database
volume. Volume deletion destroys data; it is deliberately absent from the quick
start. See [operations](docs/operations.md) for upgrades and troubleshooting.

## Local Python development

Use Python 3.12 and separately running PostgreSQL 16 and Redis 7.0 on localhost.
Default Compose does not expose their ports to host Python processes.

```bash
python3.12 -m venv venv
source venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip check
cp .env.example .env
```

Provision the local role and databases as described in
[host development setup](docs/operations.md#host-development-setup). In `.env`,
change `DATABASE_URL` to your local PostgreSQL URL and `REDIS_URL` to
`redis://127.0.0.1:6379/0`. Container hostnames `postgres` and `redis` are for
Compose execution. Keep both signing secrets configured and `DEBUG=false`.

```bash
alembic upgrade head
python -m app.scripts.seed
uvicorn app.main:app --reload --no-proxy-headers --no-access-log
```

## Configuration and migrations

[.env.example](.env.example) documents local Compose values;
[the configuration reference](docs/operations.md#environment-reference) includes
application defaults and Compose-only/test variables. Environment variables
supersede `.env`. Keep `.env` out of Git and container build contexts.

```bash
alembic heads
alembic current
alembic upgrade head
python -m app.scripts.seed
```

Migrations own schema deployment. Startup does not call `create_all` or seed
accounts. Current migration head: `c27e8a9410bf`. Seeding the three default roles
is safe to rerun sequentially; it does not create an admin account.

## API documentation and examples

When the API runs locally:

- [Swagger UI](http://127.0.0.1:8000/docs)
- [ReDoc](http://127.0.0.1:8000/redoc)
- [OpenAPI JSON](http://127.0.0.1:8000/openapi.json)

The [route reference and examples](docs/api-reference.md) covers all implemented
routes and access rules. Login uses a JSON body:

```bash
curl --fail-with-body http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  --data '{"email":"admin@example.com","password":"YOUR_BOOTSTRAPPED_PASSWORD"}'
```

Use the returned access token as `Authorization: Bearer ...`. Payment initiation
creates a pending fake-provider payment; only a verified webhook confirms the
order. There is no public order cancellation or refund endpoint.

## Tests and coverage

Tests recreate the **entire** local `orderflow_test` schema and flush Redis
database **1**. Provision those disposable resources first. Export test URLs
explicitly: these fixture overrides use process environment variables, not `.env`
loading. The URL password must match the local role you created.

```bash
export TEST_DATABASE_URL='postgresql+asyncpg://orderflow_user:local_only_password@127.0.0.1:5432/orderflow_test'
export TEST_REDIS_URL='redis://127.0.0.1:6379/1'
export DEBUG=false
pytest -v
pytest --cov=app --cov-branch --cov-report=term-missing
pyright app
alembic heads
```

Run serially; independent pytest workers cannot share the schema recreation
fixture. Tests use independent HTTP/database sessions for concurrency and fresh
sessions to verify committed state. Coverage tracks SQLAlchemy greenlets and
threads; no custom exclusions inflate the result.

The verified Day 6 run on PR #28's code passed **248 tests** with **88.97% line**
and **73.40% branch** coverage. These are measured snapshot results, not a
promise about future revisions. See the [coverage report](docs/test-coverage.md)
for counts, scope, mutation checks and remaining failure paths.

## GitHub Actions

[CI](.github/workflows/ci.yml) runs on PRs targeting `main`, pushes to `main`, and
manual dispatch:

| Job | Checks |
| --- | --- |
| Tests, typing and migrations | Pinned dependency install, isolated service validation, migration round trip, Pyright, verbose pytest, branch coverage XML and JUnit generation |
| Docker Compose smoke checks | Fresh image/stack, startup and migrations, health, non-root runtime, cache/limiter behavior, persistence, outages, failure gates and graceful shutdown |

The XML files are generated in the runner workspace; the workflow currently has
no artifact-upload step. Verified Day 6 [CI run 39825899644](https://github.com/itsdevcode/OrderFlow/actions/runs/39825899644)
passed both jobs. That result applies to its recorded commit, not automatically
to later changes.

## Security and operational boundaries

JWT access tokens are stateless and may remain usable until expiry after logout;
disabled/deleted users are checked on each protected request. Login and refresh
return 503 during a Redis limiter outage. Payment webhooks continue through HMAC,
body limits, replay checks and database idempotency during that outage.

The default payment provider is fake, notification delivery is in-app, and
background dispatch runs inside the API process. There is no automatic scheduler
for reservation expiration, notification retries or payment reconciliation.
External delivery would require provider idempotency to handle crash-after-send
retries. TLS ingress, secret rotation, backups, restore drills and deployment
coordination remain operator responsibilities.

## Detailed engineering reference

| Read next | Purpose |
| --- | --- |
| [Architecture](docs/architecture.md) | Layers, dependencies and transaction ownership |
| [Database design](docs/database-design.md) | ER diagram, keys, constraints and indexes |
| [Business workflows](docs/business-workflows.md) | Authentication, orders, payments and shipments |
| [Reliability](docs/reliability.md) | Cache outbox, Lua fencing, rate limits and recovery |
| [Security and observability](docs/security-observability.md) | Trust boundaries, audit events and JSON logs |
| [Operations](docs/operations.md) | Setup, configuration, Docker and CI |
| [API reference](docs/api-reference.md) | Route inventory, permissions and examples |
| [Interview walkthrough](docs/interview-walkthrough.md) | Ten-minute presentation and technical Q&A |
| [Test coverage](docs/test-coverage.md) | Verified measurements and testing limits |
