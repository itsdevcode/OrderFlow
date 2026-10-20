import logging
from time import perf_counter
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import RequestContext, request_context, validated_request_id

logger = logging.getLogger("app.http")


class RequestContextMiddleware:
    """Pure ASGI middleware, preserving streaming and contextvar propagation."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = Headers(scope=scope).getlist("x-request-id")
        request_id = validated_request_id(supplied[0]) if len(supplied) == 1 else None
        request_id = request_id or str(uuid4())
        method = scope.get("method", "OTHER")
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}:
            method = "OTHER"
        token = request_context.set(RequestContext(request_id, method))
        scope.setdefault("state", {})["request_id"] = request_id
        started_at = perf_counter()
        status = 500
        response_started = False
        failed = False

        async def correlated_send(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, correlated_send)
        except Exception:
            failed = True
            if not response_started:
                # Starlette's outer ServerErrorMiddleware cannot add our header.
                # Send its existing default 500 body, then preserve propagation.
                await PlainTextResponse("Internal Server Error", status_code=500)(scope, receive, correlated_send)
            raise
        finally:
            try:
                route = getattr(scope.get("route"), "path", None)
                # Never log raw URLs, unknown paths, query strings, or headers.
                request_context.set(RequestContext(
                    request_id, method, route if isinstance(route, str) else "<unmatched>",
                    status, round((perf_counter() - started_at) * 1000, 3),
                ))
                logger.log(logging.ERROR if failed else logging.INFO, "request", extra={
                    "event": "http.request_failed" if failed else "http.request_completed",
                })
            finally:
                request_context.reset(token)
