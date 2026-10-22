# Operations and configuration

[README](../README.md) · [Reliability](reliability.md) · [Security](security-observability.md)

## Docker development stack

[Dockerfile](../Dockerfile) builds a Python 3.12 virtual environment with pinned
runtime dependencies and binary wheels, then copies it into a slim Bookworm
runtime. Development tools and tests are not installed in the image. UID/GID
10001 runs Uvicorn directly with no reload, proxy-header rewriting or default
access logs. [.dockerignore](../.dockerignore) allowlists the required build inputs
and excludes local secrets/environments/Git metadata.

[compose.yaml](../compose.yaml) defines:

| Service | Responsibility and startup gate |
| --- | --- |
| postgres | PostgreSQL 16; named `postgres_data` volume; pg_isready health check |
| redis | Redis 7.0; ping health check; disposable cache/limiter state |
| migrate | Same app image, one-shot `alembic upgrade head`; waits for healthy PostgreSQL |
| api | Waits for healthy PostgreSQL/Redis and successful migrate; binds localhost API port |

The default services use Compose DNS (`postgres`, `redis`); only API port 8000 is
published on `127.0.0.1`. Redis has RDB and AOF disabled, 128 MB maxmemory and
`noeviction`. Memory pressure can therefore fail Redis writes instead of silently
evicting active limiter keys; authentication admission can return 503. PostgreSQL
retains business data in its named volume. Restarting Redis resets limiter windows.

API/migrate drop Linux capabilities, disallow privilege escalation, use a read-only
root filesystem and mount writable `/tmp` as tmpfs. These controls do not supply
a complete production perimeter or secret store.

```bash
cp .env.example .env
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs migrate
docker compose exec api python -m app.scripts.seed
```

Compose initializes `POSTGRES_*` only for an empty database volume. Changing those
variables later does not rotate an existing database password or rename its role.
`DATABASE_URL` credentials must match the database; percent-encode reserved URL
characters. Prefer `config --quiet`: fully rendered Compose output can expose
runtime secrets.

### Health and shutdown

`GET /health` is process liveness. `GET /health/db` executes `SELECT 1`; the image
health check calls it. Neither endpoint asserts Redis admission health. App
startup can tolerate Redis failure independently of Compose's initial dependency
gate. A container health check marks status; it is not itself an auto-restart policy.

Compose signals the directly running Uvicorn process. Uvicorn has 25 seconds for
graceful shutdown, within the API's 30-second Compose stop grace period. Lifespan
closes Redis. In-process background tasks can still be interrupted by crashes,
forced shutdown or expiration of that budget; durable rows enable later recovery
but do not trigger an automatic retry scheduler.

```bash
docker compose logs --tail=100 api
docker compose stop
docker compose start
```

### Updates and migrations

Stop application traffic before an explicit schema update; a preexisting successful
migration container does not mean newly pulled migrations have already run.

```bash
docker compose stop api
docker compose build
docker compose run --rm migrate
docker compose up -d api
```

Review migrations and take appropriate backups before applying them to valuable
data. This is a single-project development sequence, not an expand/contract or
zero-downtime rollout guarantee. Independent Compose projects/hosts need external
migration coordination. Do not run database downgrades or volume deletion as a
routine troubleshooting step.

## First-admin bootstrap

The role seed command creates ADMIN/MANAGER/CUSTOMER roles only. There is no default
user or public registration endpoint. On a database you own, seed roles first,
then create the first admin through a controlled local administration session.
The following command prompts inside the container, hashes the password through
the application helper, refuses to overwrite an existing email, and commits one
new account. Keep the terminal private; do not pipe credentials into logs.

```bash
docker compose exec api python -c '
import asyncio
from getpass import getpass
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.role import Role
from app.models.user import User
from app.core.security import hash_password

async def main():
    email = input("Admin email: ").strip()
    name = input("Admin name: ").strip()
    password = getpass("Admin password: ")
    if not email or not name or len(password) < 12:
        raise SystemExit("Provide email/name and a password of at least 12 characters")
    async with AsyncSessionLocal() as db:
        role = await db.scalar(select(Role).where(Role.name == "ADMIN"))
        if role is None:
            raise SystemExit("Run python -m app.scripts.seed first")
        if await db.scalar(select(User.id).where(User.email == email)):
            raise SystemExit("Email already exists; no account was changed")
        db.add(User(name=name, email=email, hashed_password=hash_password(password), role_id=role.id))
        await db.commit()
    print("Admin created")

asyncio.run(main())
'
```

This is an explicit operator action, not an HTTP endpoint, migration or automatic
startup behavior. For host execution, use `python -c` with the same script and the
intended local DATABASE_URL. Provision further users via the authenticated admin
API, using actual role IDs rather than assuming fixed numeric IDs. The bootstrap
script's password minimum is an administrative example; it does not change the
application schema's own validation rules.

## Host development setup

Use separately running PostgreSQL 16 and Redis 7.0 reachable from host Python.
Default Compose does not publish database/cache ports. Avoid port collisions and
production services when provisioning a development environment.

Using your PostgreSQL administrator connection, the SQL below creates local-only
example credentials and two databases. Run it only when these names are absent;
it deliberately does not drop or replace existing resources.

```sql
CREATE ROLE orderflow_user WITH LOGIN PASSWORD 'local_only_password';
CREATE DATABASE orderflow OWNER orderflow_user;
CREATE DATABASE orderflow_test OWNER orderflow_user;
```

After creating a Python 3.12 virtual environment and installing
`requirements-dev.txt`, copy `.env.example` and change these host-specific values:

```dotenv
DATABASE_URL=postgresql+asyncpg://orderflow_user:local_only_password@127.0.0.1:5432/orderflow
REDIS_URL=redis://127.0.0.1:6379/0
DEBUG=false
```

Retain the signing/webhook variables from the local example. Ensure no inherited
shell variable overrides them unexpectedly. Then run:

```bash
alembic upgrade head
python -m app.scripts.seed
uvicorn app.main:app --reload --no-proxy-headers --no-access-log
```

Local signing-secret placeholders are not deployment secrets. Install exact
runtime versions from requirements; Python 3.12 is the tested Docker/CI target.
The code uses Python 3.12 features such as `typing.override`.

## Environment reference

[Settings](../app/core/config.py) reads `.env`; process environment takes priority.
Names below are uppercase spellings of its fields. Values are code defaults,
which `.env.example` may deliberately override for local Compose. Empty database
and JWT defaults are not usable secure deployment configuration.

| Application variable | Code default | Meaning |
| --- | --- | --- |
| `AUDIT_FAILURE_TIMEOUT_SECONDS` | `1.0` | Failed-login audit deadline; greater than 0, at most 5 seconds |
| `LOG_LEVEL` | `INFO` | Application log threshold |
| `APP_NAME` | `OrderFlow API` | OpenAPI title |
| `APP_VERSION` | `1.0.0` | OpenAPI application version |
| `DEBUG` | `false` | Enables development webhook-secret fallback when true |
| `DATABASE_URL` | `empty; must configure` | Async PostgreSQL URL; configure explicitly |
| `JWT_SECRET_KEY` | `empty; must configure` | Signing key; configure a strong secret explicitly |
| `JWT_ALGORITHM` | `HS256` | JWT signing/verification algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access JWT lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh JWT lifetime |
| `PAYMENT_WEBHOOK_SECRET` | `empty; must configure` | Required outside debug; configure explicitly |
| `PAYMENT_WEBHOOK_MAX_BYTES` | `65536` | Maximum streamed webhook bytes |
| `NOTIFICATION_MAX_ATTEMPTS` | `3` | Maximum dispatch claims per notification |
| `NOTIFICATION_CLAIM_LEASE_SECONDS` | `300` | Age permitting recovery of PROCESSING claims |
| `REDIS_URL` | `redis://localhost:6379/0` | Shared cache/limiter endpoint |
| `CACHE_ENABLED` | `true` | Enable catalog response caching |
| `CACHE_DEFAULT_TTL_SECONDS` | `300` | Catalog response TTL |
| `REDIS_CONNECT_TIMEOUT` | `2.0` | Connection timeout seconds |
| `REDIS_SOCKET_TIMEOUT` | `2.0` | Socket timeout seconds |
| `RATE_LIMIT_ENABLED` | `true` | Enable endpoint admission limits |
| `RATE_LIMIT_LOGIN_LIMIT` | `10` | Admitted login requests per fixed window |
| `RATE_LIMIT_LOGIN_WINDOW_SECONDS` | `60` | Login window seconds |
| `RATE_LIMIT_REFRESH_LIMIT` | `30` | Admitted refresh requests per fixed window |
| `RATE_LIMIT_REFRESH_WINDOW_SECONDS` | `60` | Refresh window seconds |
| `RATE_LIMIT_WEBHOOK_LIMIT` | `120` | Admitted webhook requests per fixed window |
| `RATE_LIMIT_WEBHOOK_WINDOW_SECONDS` | `60` | Webhook window seconds |
| `RATE_LIMIT_TRUSTED_PROXIES` | `[]` | JSON array of trusted IPs/CIDRs |

Rate limits must be positive integers up to 1,000,000 and windows between 1 and
86,400 seconds; proxy entries are validated IP networks. Other settings have the
specific validation shown in Settings, not a blanket positivity guarantee.

| Compose/test variable | Scope |
| --- | --- |
| POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD | Image initialization; defaults orderflow/orderflow_user, required password |
| API_PORT | Localhost published API port, default 8000 |
| ORDERFLOW_IMAGE_TAG | Local image tag, default local |
| TEST_DATABASE_URL | Process-env fixture override, local asyncpg URL named orderflow_test |
| TEST_REDIS_URL | Process-env fixture override, local Redis database 1 |

Compose explicitly forwards application fields listed in its environment map;
APP_NAME/APP_VERSION are Settings fields but are not forwarded by the current
Compose map. The notification/Redis timeout settings have code and Compose
defaults even where `.env.example` does not list them. TEST_* values in `.env`
are examples; export them for pytest's `os.getenv` fixtures.

## Tests and CI isolation

Tests drop/recreate all application metadata tables in the dedicated local
`orderflow_test` database and flush Redis database 1. Fixtures reject remote
hosts, other database names/indices and URL query overrides before constructing
test engines/clients. They configure Settings before app imports and bind both
request and background session factories to test resources. NullPool avoids
connections crossing function event loops; each test closes its Redis pool.

```bash
export TEST_DATABASE_URL='postgresql+asyncpg://orderflow_user:local_only_password@127.0.0.1:5432/orderflow_test'
export TEST_REDIS_URL='redis://127.0.0.1:6379/1'
export DEBUG=false
pytest -v
pytest --cov=app --cov-branch --cov-report=term-missing
pyright app
alembic heads
```

These commands require the local signing/webhook configuration in `.env`. Do not
use xdist workers against the shared schema. `metadata.create_all` in tests does
not replace migration verification.

[CI](../.github/workflows/ci.yml) triggers on PRs to main, pushes to main and
workflow_dispatch. Both jobs run on Ubuntu 24.04 with read-only repository token
permissions and bounded timeouts. The verify job uses Python 3.12, PostgreSQL 16
and Redis 7.0 service containers. A dedicated `orderflow_migrations` database must
start empty; [ci_services](../scripts/ci_services.py) validates local URLs and
creates the separate empty `orderflow_test` database. Test Redis uses database 1,
while ordinary configuration uses database 0.

The migration script upgrades the fresh migration database, downgrades **only the
latest revision** to its parent, then upgrades again. Typing checks `app`. Pytest
generates terminal branch coverage, `coverage.xml` and `test-results.xml`. The
workflow does not currently upload those XML files as retained artifacts or enforce
a minimum coverage percentage. Greenlet/thread coverage tracking is configured in
[pyproject.toml](../pyproject.toml); no custom coverage exclusions are set.

The independent Docker job runs [scripts.docker_smoke](../scripts/docker_smoke.py)
with a unique disposable project and cleanup restricted to its own containers and
volumes. It verifies fresh image/migrations/health, non-root execution, real cache
and limiting, PostgreSQL persistence, Redis outage/recovery, expected credential/
migration failures and graceful shutdown. Running it locally requires Docker:

```bash
python -m scripts.docker_smoke
```

Do not run `scripts.ci_services` or `scripts.ci_migrations` against an existing
valuable database; their empty disposable-resource preconditions are intentional.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No working Docker command in WSL | Enable Docker Desktop's WSL integration or use a host with a working engine |
| API waits or migration exits | Inspect `docker compose ps` and `docker compose logs migrate postgres`; verify URL host/credentials and migration failure |
| Password changed in .env but login to PostgreSQL fails | Existing volume keeps old role credentials; plan an actual role/password update and matching URL |
| Host Python cannot resolve postgres/redis | Use host-local URLs and separately exposed/running services |
| Login/refresh returns 503 | Redis availability, capacity, pool configuration and limiter cooldown; inspect Retry-After |
| Tests reject a URL | Use explicit local orderflow_test and Redis database 1 URLs without query overrides |
| Settings says DEBUG is not boolean | Override inherited values such as DEBUG=release with true/false |
| Notification stays PROCESSING or FAILED | Inspect attempts/lease and provide explicit operator-owned redispatch; there is no scheduler |
| Payment requires reconciliation | Review event diagnostics and reservation state; do not force success or stock mutation blindly |

Local Compose is not a hardened production topology. See
[production limitations](security-observability.md#production-limitations) before
adapting it for any deployment.
