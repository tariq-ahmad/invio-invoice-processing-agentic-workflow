"""In-memory buffer letting the frontend poll for pipeline progress instead of
relying on ADK's built-in `/run_sse` stream, which Cloud Run's front end fully
buffers over HTTP/2 until the connection closes (confirmed empirically — see
app/app_utils/sse.py). The Cloud Run service runs a single instance
(min=max=1), so plain module-level state is safe; no cross-instance sync
needed.

`SSEUnbufferedMiddleware` taps each `data:` line as ADK produces it (in real
time, independent of the HTTP transport) and appends it here, keyed by a
per-run token the frontend generates. The frontend then polls `/run_sse/poll`
for new lines instead of reading the (transport-delayed) stream directly.
"""

import time

_MAX_AGE_SECONDS = 600

_buffers: dict[str, list[str]] = {}
_done: dict[str, bool] = {}
_touched: dict[str, float] = {}


def _cleanup() -> None:
    now = time.time()
    stale = [tok for tok, t in _touched.items() if now - t > _MAX_AGE_SECONDS]
    for tok in stale:
        _buffers.pop(tok, None)
        _done.pop(tok, None)
        _touched.pop(tok, None)


def append_line(token: str, line: str) -> None:
    _cleanup()
    _buffers.setdefault(token, []).append(line)
    _touched[token] = time.time()


def mark_done(token: str) -> None:
    _done[token] = True
    _touched[token] = time.time()


def get_since(token: str, cursor: int) -> tuple[list[str], int, bool]:
    lines = _buffers.get(token, [])
    return lines[cursor:], len(lines), _done.get(token, False)
