"""In-process SSE broadcaster for the invoice pipeline auto-trigger.

Singleton: the push handler publishes events, the /events SSE endpoint fans
them out to all open browser connections. Clients that connect after the event
get a replay if still within TTL.

The last event is also persisted to disk so it survives server restarts —
critical because Cloud Run deploys a new revision while the old one is still
handling the broadcast, then the old process dies before the browser reconnects.

Single-instance only: the broadcaster lives in process memory, so Cloud Run
must be configured min=max=1 (in-memory HITL sessions have the same constraint).
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Replay window: only long enough to cover the 1-2s gap between the Pub/Sub
# push and the browser's SSE/poll connecting. Kept short so a stale event can't
# be re-consumed after a refresh (client-side message_id dedup is the real
# guard; this is defense-in-depth).
_TTL_SECONDS = 90
_REPLAY_FILE = Path(os.environ.get("HOME", "/tmp")) / ".sse_replay.json"


def _load_replay_from_disk() -> tuple[dict, float] | None:
    try:
        raw = json.loads(_REPLAY_FILE.read_text())
        return (raw["payload"], float(raw["ts"]))
    except Exception:
        return None


def _save_replay_to_disk(payload: dict, ts: float) -> None:
    try:
        _REPLAY_FILE.write_text(json.dumps({"payload": payload, "ts": ts}))
    except Exception as exc:
        logger.warning("broadcaster: could not persist replay: %s", exc)


class EventsBroadcaster:
    """Fan-out typed SSE events to all subscribed async queues."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()
        # (event_dict, unix_ts) — replayed once to newly-connected clients
        self._last: tuple[dict, float] | None = _load_replay_from_disk()
        if self._last:
            _payload, ts = self._last
            age = int(time.time() - ts)
            if age < _TTL_SECONDS:
                logger.warning("broadcaster: loaded replay from disk (age=%ds)", age)
            else:
                self._last = None  # expired

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    async def publish(self, event_type: str, data: dict) -> None:
        """Fan-out to all subscribers; drop slow ones; persist for replay."""
        payload = {"type": event_type, "data": data}
        ts = time.time()
        self._last = (payload, ts)
        _save_replay_to_disk(payload, ts)

        dead: list[asyncio.Queue] = []
        for q in list(self._queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("broadcaster: dropped event for slow subscriber")
                dead.append(q)
        for q in dead:
            self._queues.discard(q)
        logger.warning(
            "broadcaster: published %s to %d live subscriber(s)", event_type, len(self._queues)
        )

    def get_replay(self) -> dict | None:
        """Return the most recent event if still within TTL, else None."""
        if self._last is None:
            return None
        payload, ts = self._last
        return payload if (time.time() - ts) < _TTL_SECONDS else None


# Module-level singleton — imported by both fast_api_app and push handler tests.
broadcaster = EventsBroadcaster()
