"""Exercise Compose on a unique disposable project; never reuse existing volumes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def main() -> None:
    project = f"orderflow-smoke-{uuid4().hex[:12]}"
    env = dict(os.environ)
    env.update(
        POSTGRES_DB="orderflow", POSTGRES_USER="orderflow_smoke",
        POSTGRES_PASSWORD="smoke_only_password",
        DATABASE_URL="postgresql+asyncpg://orderflow_smoke:smoke_only_password@postgres:5432/orderflow",
        REDIS_URL="redis://redis:6379/0", DEBUG="false",
        JWT_SECRET_KEY="smoke_only_jwt_key_not_for_deployment",
        PAYMENT_WEBHOOK_SECRET="smoke_only_webhook_key_not_for_deployment",
        RATE_LIMIT_LOGIN_LIMIT="3", RATE_LIMIT_LOGIN_WINDOW_SECONDS="86400",
        RATE_LIMIT_TRUSTED_PROXIES="[]", CACHE_ENABLED="true", RATE_LIMIT_ENABLED="true",
        API_PORT="18000", ORDERFLOW_IMAGE_TAG=project,
    )
    with tempfile.TemporaryDirectory(prefix=project) as directory:
        env_file = Path(directory) / "empty.env"
        env_file.touch()
        base = ["docker", "compose", "--env-file", str(env_file), "-p", project, "-f", "compose.yaml"]

        def compose(*args: str, check: bool = True, override: str | None = None) -> subprocess.CompletedProcess[str]:
            command = base + (["-f", override] if override else []) + list(args)
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=600)
            if check and result.returncode:
                # Commands contain no production values. Avoid printing resolved config.
                raise RuntimeError(f"Compose {args[0]} failed:\n{result.stdout}\n{result.stderr}")
            return result

        def request(path: str, payload: dict | None = None, token: str | None = None) -> tuple[int, dict | list]:
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = Request("http://127.0.0.1:18000" + path, headers=headers,
                          data=json.dumps(payload).encode() if payload is not None else None)
            try:
                with urlopen(req, timeout=15) as response:
                    return response.status, json.load(response)
            except HTTPError as exc:
                return exc.code, json.load(exc)

        def ready() -> None:
            for _ in range(60):
                try:
                    if request("/health/db")[0] == 200:
                        return
                except (URLError, TimeoutError, ConnectionError):
                    pass
                time.sleep(1)
            raise RuntimeError("API did not become database-ready")

        def assert_api_blocked() -> None:
            running = compose("ps", "--status", "running", "--services").stdout.splitlines()
            assert "api" not in running, "Failed migrations must block API startup"

        try:
            compose("config", "--quiet")
            compose("build")
            compose("up", "-d", "--wait", "--wait-timeout", "120")
            ready()
            assert request("/health")[0] == 200
            uid = compose("exec", "-T", "api", "id", "-u").stdout.strip()
            assert uid == "10001", "API must run as the non-root image user"
            revision = compose("run", "--rm", "--no-deps", "migrate", "alembic", "current").stdout
            assert "(head)" in revision
            # Seed a disposable administrator; credentials are never printed.
            seed = '''import asyncio
from app.db.session import AsyncSessionLocal, engine
from app.models.role import Role
from app.models.user import User
from app.core.security import hash_password
async def seed():
    async with AsyncSessionLocal() as db:
        role = Role(name="ADMIN")
        db.add(role)
        await db.flush()
        db.add(User(name="Smoke", email="smoke@example.com", role_id=role.id,
                    hashed_password=hash_password("SmokePassword123!")))
        await db.commit()
    await engine.dispose()
asyncio.run(seed())'''
            compose("exec", "-T", "api", "python", "-c", seed)
            status, login = request("/api/v1/auth/login", {"email": "smoke@example.com", "password": "SmokePassword123!"})
            assert status == 200
            token = login["access_token"]
            status, category = request("/api/v1/categories", {"name": "Docker persistence smoke", "slug": "docker-persistence-smoke"}, token)
            assert status == 201
            path = f"/api/v1/categories/{category['id']}"
            assert request(path, token=token)[0] == 200
            cached = compose("exec", "-T", "redis", "redis-cli", "EXISTS", f"orderflow:v1:categories:detail:{category['id']}").stdout.strip()
            assert cached == "1", "Catalog reads must populate the Redis detail key"
            invalid = {"email": "missing@example.com", "password": "incorrect"}
            assert request("/api/v1/auth/login", invalid)[0] == 401
            assert request("/api/v1/auth/login", invalid)[0] == 401
            assert request("/api/v1/auth/login", invalid)[0] == 429
            print("PASS: image, non-root user, fresh migrations, health, catalog cache and rate limiting", flush=True)

            compose("restart", "postgres")
            ready()
            assert request(path, token=token)[0] == 200
            # Recreate database container with its same volume to verify durability.
            compose("up", "-d", "--force-recreate", "--wait", "postgres")
            compose("restart", "api")
            ready()
            assert request(path, token=token)[0] == 200
            stored = compose("exec", "-T", "postgres", "psql", "-U", "orderflow_smoke", "-d", "orderflow", "-tAc",
                             "SELECT count(*) FROM categories WHERE slug = 'docker-persistence-smoke'").stdout.strip()
            assert stored == "1", "Category must persist in PostgreSQL, independently of cache"
            print("PASS: PostgreSQL data survives restart and container recreation", flush=True)

            compose("stop", "redis")
            assert request(path, token=token)[0] == 200
            assert request("/api/v1/auth/login", invalid)[0] == 503
            assert request("/api/v1/auth/refresh", {"refresh_token": "invalid"})[0] == 503
            assert request("/api/v1/webhooks/payments", {})[0] == 401
            assert request("/health/db")[0] == 200
            compose("start", "redis")
            for _ in range(30):
                if request("/api/v1/auth/login", invalid)[0] == 401:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Rate limiter did not recover after Redis returned")
            print("PASS: Redis outage preserves catalog fallback and auth/webhook policies; recovery", flush=True)

            compose("stop", "api")
            container = compose("ps", "-aq", "api").stdout.strip()
            state = json.loads(subprocess.check_output(["docker", "inspect", "--format", "{{json .State}}", container], text=True))
            assert state["ExitCode"] == 0 and not state["OOMKilled"], "API did not stop gracefully"
            compose("rm", "-f", "api", "migrate")
            good_url = env["DATABASE_URL"]
            env["DATABASE_URL"] = good_url.replace("smoke_only_password", "wrong_password")
            assert compose("up", "-d", "api", check=False).returncode != 0
            assert_api_blocked()
            env["DATABASE_URL"] = good_url
            compose("rm", "-sf", "api", "migrate")
            override = Path(directory) / "failure.yaml"
            override.write_text('services:\n  migrate:\n    command: [python, -c, "raise SystemExit(7)"]\n')
            assert compose("up", "-d", "api", override=str(override), check=False).returncode != 0
            assert_api_blocked()
            compose("rm", "-sf", "api", "migrate")
            compose("up", "-d", "--wait", "--wait-timeout", "120")
            ready()
            compose("stop", "api", "redis", "postgres")
            for service in ("api", "redis", "postgres"):
                container = compose("ps", "-aq", service).stdout.strip()
                state = json.loads(subprocess.check_output(["docker", "inspect", "--format", "{{json .State}}", container], text=True))
                assert state["ExitCode"] == 0 and not state["OOMKilled"], f"{service} did not stop gracefully"
            print("PASS: graceful stop, invalid credentials and migration failures block API; clean recovery", flush=True)
        except BaseException:
            print(compose("logs", "--no-color", "--tail", "100", check=False).stdout)
            raise
        finally:
            # Only this generated project's disposable volumes are removed.
            compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
