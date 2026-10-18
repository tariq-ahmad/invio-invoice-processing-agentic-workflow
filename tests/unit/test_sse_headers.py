"""Unit tests for SSEUnbufferedMiddleware — it must add X-Accel-Buffering: no to
text/event-stream responses (so Cloud Run doesn't buffer the pipeline SSE stream)
without touching non-SSE responses or clobbering an explicit Cache-Control.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.app_utils.sse import SSEUnbufferedMiddleware


def _client() -> TestClient:
    async def sse(_request):
        async def gen():
            yield "data: hello\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def sse_with_cache(_request):
        async def gen():
            yield "data: hi\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    async def plain(_request):
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/sse", sse),
            Route("/sse-cache", sse_with_cache),
            Route("/plain", plain),
        ]
    )
    app.add_middleware(SSEUnbufferedMiddleware)
    return TestClient(app)


def test_sse_response_gets_accel_buffering_header():
    resp = _client().get("/sse")
    assert resp.headers["x-accel-buffering"] == "no"
    # Default Cache-Control is added when the response didn't set one.
    assert resp.headers["cache-control"] == "no-cache"


def test_sse_middleware_preserves_explicit_cache_control():
    resp = _client().get("/sse-cache")
    assert resp.headers["x-accel-buffering"] == "no"
    # An explicit Cache-Control must not be overwritten.
    assert resp.headers["cache-control"] == "no-store"


def test_non_sse_response_untouched():
    resp = _client().get("/plain")
    assert "x-accel-buffering" not in resp.headers
