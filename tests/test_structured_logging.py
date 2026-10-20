import asyncio
from io import StringIO
import json
import logging
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
import pytest

from app.core.logging import JsonFormatter, configure_logging, request_context
from app.middleware.request_context import RequestContextMiddleware


@pytest.fixture
def logged_app():
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app.http")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    @app.get("/health")
    async def health():
        return {"status": "ok", "request_id": request_context.get().request_id}

    @app.get("/error")
    async def error():
        raise RuntimeError("password=private-password token=private-token person@example.com")

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return {"ok": True}

    yield app, output
    logger.removeHandler(handler)
    logger.setLevel(old_level)


async def test_request_id_header_and_context_reset(logged_app):
    app, _ = logged_app
    assert request_context.get() is None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        UUID(response.headers["X-Request-ID"])
    assert request_context.get() is None


@pytest.mark.parametrize("supplied", ["invalid", "x" * 2048, "", "person@example.com", "00000000000000000000000000000000"])
async def test_malformed_request_ids_are_replaced(logged_app, supplied):
    app, _ = logged_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"X-Request-ID": supplied})
        value = response.headers["X-Request-ID"]
        assert value != supplied and str(UUID(value)) == value


async def test_valid_and_duplicate_request_ids(logged_app):
    app, _ = logged_app
    request_id = str(uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"X-Request-ID": request_id})
        assert response.headers["X-Request-ID"] == request_id
        duplicate = await client.get("/health", headers=[("X-Request-ID", request_id), ("X-Request-ID", request_id)])
        assert duplicate.headers["X-Request-ID"] != request_id


async def test_concurrent_requests_keep_distinct_contexts(logged_app):
    app, output = logged_app
    both_entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    @app.get("/concurrent")
    async def concurrent(request: Request):
        before = request_context.get().request_id
        seen.append(before)
        if len(seen) == 2:
            both_entered.set()
        await release.wait()
        assert request_context.get().request_id == before
        assert request.state.request_id == before
        return {"id": before}

    ids = [str(uuid4()), str(uuid4())]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        tasks = [asyncio.create_task(client.get("/concurrent", headers={"X-Request-ID": value})) for value in ids]
        try:
            await asyncio.wait_for(both_entered.wait(), timeout=5)
        finally:
            release.set()
        results = await asyncio.gather(*tasks)
    assert [result.json()["id"] for result in results] == ids
    assert {json.loads(line)["request_id"] for line in output.getvalue().splitlines()} == set(ids)
    assert request_context.get() is None


async def test_http_json_fields_and_private_values_are_omitted(logged_app):
    app, output = logged_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/items/private-token?email=person@example.com", headers={"Authorization": "Bearer private-token", "Cookie": "secret=private-cookie"})
        assert response.status_code == 200
        await client.get("/unknown-private-path?password=private-password")
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    first = records[0]
    assert set(first) == {"timestamp", "severity", "logger", "event", "request_id", "http_method", "path", "status", "duration_ms"}
    assert first["path"] == "/items/{item_id}" and first["status"] == 200
    assert first["http_method"] == "GET" and first["duration_ms"] >= 0
    assert records[1]["path"] == "<unmatched>"
    for secret in ("private-token", "person@example.com", "private-cookie", "private-password", "unknown-private-path"):
        assert secret not in output.getvalue()


async def test_unexpected_error_preserves_500_and_correlation_without_exception_payload(logged_app):
    app, output = logged_app
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.get("/error")
    assert response.status_code == 500 and response.text == "Internal Server Error"
    UUID(response.headers["X-Request-ID"])
    entry = json.loads(output.getvalue())
    assert entry["event"] == "http.request_failed" and entry["status"] == 500
    assert "private-password" not in output.getvalue() and "person@example.com" not in output.getvalue()
    assert request_context.get() is None


def test_formatter_drops_messages_exception_payloads_and_unapproved_extras():
    record = logging.LogRecord("app.test", logging.ERROR, "", 0, "token=%s person@example.com", ("private-token",), None)
    record.exc_info = (ValueError, ValueError("private-password"), None)
    record.stack_info = "private-cookie"
    record.password = "private-password"
    record.event = "private-token"
    result = JsonFormatter().format(record)
    assert json.loads(result)["event"] == "application.log"
    for secret in ("private-token", "private-password", "private-cookie", "person@example.com"):
        assert secret not in result


def test_logging_configuration_is_idempotent_and_scoped():
    app = logging.getLogger("app")
    access = logging.getLogger("uvicorn.access")
    errors = logging.getLogger("uvicorn.error")
    unrelated = logging.getLogger("unrelated.library")
    before = (list(app.handlers), app.level, app.propagate, access.disabled, list(errors.handlers), errors.propagate)
    root_handlers, unrelated_handlers = list(logging.getLogger().handlers), list(unrelated.handlers)
    try:
        configure_logging()
        configure_logging()
        assert len([h for h in app.handlers if h.name == "orderflow.json"]) == 1
        assert access.disabled and isinstance(errors.handlers[0].formatter, JsonFormatter)
        assert logging.getLogger().handlers == root_handlers and unrelated.handlers == unrelated_handlers
    finally:
        app.handlers, app.level, app.propagate, access.disabled, errors.handlers, errors.propagate = before
