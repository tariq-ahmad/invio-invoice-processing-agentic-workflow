"""ASGI middleware for Server-Sent Events on Cloud Run.

Confirmed empirically (real browser vs. a plain HTTP/1.1 client): Cloud Run's
front end fully buffers a `text/event-stream` response over HTTP/2 — every
browser client, since browsers negotiate HTTP/2 by default — until the
connection closes, regardless of `X-Accel-Buffering: no` or response padding
(both verified present/applied and both had zero effect over HTTP/2; they
only help byte-count-based buffering, which isn't what's happening here).
Over plain HTTP/1.1 the same response streams incrementally as expected.

Since this is a Cloud Run/HTTP2 transport limitation, not something fixable
from inside the response, `SSEUnbufferedMiddleware` also taps each `data:`
line as ADK produces it — in real time, server-side, before the HTTP
transport gets a chance to buffer it — and stashes it in `sse_poll`'s
in-memory buffer, keyed by a per-run token the frontend sends as an
`X-Poll-Token` request header. The frontend polls `/run_sse/poll` (a small,
complete, non-streaming JSON response, which isn't subject to this buffering)
for progress instead of reading the direct stream.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.app_utils import sse_poll


class SSEUnbufferedMiddleware:
    """See module docstring. Pure ASGI (NOT ``BaseHTTPMiddleware``) so it only
    rewrites response-start headers and taps body chunks as they're produced —
    it never buffers the streaming body itself.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        poll_token = None
        for name, value in scope.get("headers", []):
            if name == b"x-poll-token":
                poll_token = value.decode("latin-1")
                break

        is_sse = False
        padded = False
        line_buf = ""

        def feed_poll_buffer(chunk: bytes) -> None:
            nonlocal line_buf
            line_buf += chunk.decode("utf-8", errors="ignore")
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
                if line.startswith("data:"):
                    sse_poll.append_line(poll_token, line)

        async def send_wrapper(message: Message) -> None:
            nonlocal is_sse, padded
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                if headers.get("content-type", "").startswith("text/event-stream"):
                    is_sse = True
                    headers["X-Accel-Buffering"] = "no"
                    if "cache-control" not in headers:
                        headers["Cache-Control"] = "no-cache"
            elif message["type"] == "http.response.body" and is_sse:
                if not padded:
                    padded = True
                    pad = b": " + b"0" * 2048 + b"\n\n"
                    message = {**message, "body": pad + message.get("body", b"")}
                if poll_token:
                    feed_poll_buffer(message.get("body", b""))
                    if not message.get("more_body", False):
                        sse_poll.mark_done(poll_token)
            await send(message)

        await self.app(scope, receive, send_wrapper)
