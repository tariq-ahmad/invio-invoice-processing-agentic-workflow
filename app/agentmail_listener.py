"""AgentMail WebSocket listener: the new-mail trigger for the invoice pipeline.

Replaces the Gmail Pub/Sub push + ``users.watch`` machinery. The service opens an
outbound WebSocket to AgentMail, subscribes to ``message.received`` for the AP
inbox, and hands each new message id to ``on_message``.

Two properties make this different from the push model it replaces:

* The connection is long-lived, so on Cloud Run the instance needs CPU allocated
  outside requests (``--no-cpu-throttling``) and ``--min-instances=1``.
* AgentMail does not replay events missed while we were disconnected (restart,
  redeploy, network drop). After every (re)subscribe we therefore run a catch-up
  over REST for recent messages that carry no AP lifecycle label.

This runs as a task on the server's event loop (see the lifespan hook in
``fast_api_app.py``) because the SSE broadcaster it feeds uses asyncio queues.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from app import agentmail_tools
from app.config import AGENTMAIL_CATCHUP_MINUTES, AGENTMAIL_INBOX_ID

logger = logging.getLogger(__name__)

_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 60.0
# A bad API key will not fix itself; do not hammer the auth endpoint.
_AUTH_FAILURE_DELAY_S = 300.0
# A connection that stayed up this long is considered healthy and resets backoff.
_HEALTHY_AFTER_S = 30.0


class AgentMailListener:
    def __init__(self, on_message: Callable[[str], Awaitable[None]]) -> None:
        self._on_message = on_message
        self._connected = False
        self._subscribed_at: float | None = None
        self._last_event_at: float | None = None
        self._reconnects = 0
        self._last_error: str | None = None

    def status(self) -> dict:
        return {
            "inbox": AGENTMAIL_INBOX_ID,
            "connected": self._connected,
            "subscribed_at": self._subscribed_at,
            "last_event_at": self._last_event_at,
            "reconnects": self._reconnects,
            "last_error": self._last_error,
        }

    async def run(self) -> None:
        """Connect, listen and reconnect forever. Cancel the task to stop."""
        if not AGENTMAIL_INBOX_ID:
            logger.error("agentmail listener: AGENTMAIL_INBOX_ID not set, not starting")
            return

        backoff = _BACKOFF_START_S
        while True:
            started = time.monotonic()
            delay = backoff
            try:
                await self._session()
                logger.warning("agentmail listener: connection closed by server")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("agentmail listener: connection failed: %s", self._last_error)
                if getattr(exc, "status_code", None) == 401:
                    delay = _AUTH_FAILURE_DELAY_S
                    logger.error(
                        "agentmail listener: API key rejected (401); retrying in %.0fs",
                        delay,
                    )
            finally:
                self._connected = False

            self._reconnects += 1
            if time.monotonic() - started >= _HEALTHY_AFTER_S:
                backoff = _BACKOFF_START_S
                delay = _BACKOFF_START_S
            else:
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        from agentmail import MessageReceivedEvent, Subscribe

        client = agentmail_tools.get_async_client()
        async with client.websockets.connect() as socket:
            await socket.send_subscribe(
                Subscribe(inbox_ids=[AGENTMAIL_INBOX_ID], event_types=["message.received"])
            )
            self._connected = True
            logger.warning("agentmail listener: connected, subscribed to %s", AGENTMAIL_INBOX_ID)

            # Catch up in the background so events keep flowing meanwhile; the
            # in-flight set and AP labels dedupe any overlap with live events.
            catchup = asyncio.create_task(self._catch_up())
            try:
                async for event in socket:
                    if isinstance(event, MessageReceivedEvent):
                        self._last_event_at = time.time()
                        await self._dispatch(event.message.message_id, event.message.labels)
                    else:
                        self._note_control_event(event)
            finally:
                catchup.cancel()

    def _note_control_event(self, event: object) -> None:
        kind = type(event).__name__
        if kind == "Subscribed":
            self._subscribed_at = time.time()
            logger.info("agentmail listener: subscription confirmed")
        elif kind == "Error":
            self._last_error = str(event)
            logger.error("agentmail listener: server error event: %s", event)
        else:
            logger.debug("agentmail listener: ignoring %s", kind)

    async def _catch_up(self) -> None:
        if AGENTMAIL_CATCHUP_MINUTES <= 0:
            return
        try:
            ids = await asyncio.to_thread(
                agentmail_tools.list_unhandled_message_ids, AGENTMAIL_CATCHUP_MINUTES
            )
        except Exception as exc:
            logger.warning("agentmail listener: catch-up failed (non-fatal): %s", exc)
            return
        if ids:
            logger.warning("agentmail listener: catch-up found %d unhandled message(s)", len(ids))
        for message_id in ids:
            await self._dispatch(message_id, None)

    async def _dispatch(self, message_id: str, labels: list[str] | None) -> None:
        if agentmail_tools.is_handled(labels):
            return
        try:
            await self._on_message(message_id)
        except Exception as exc:
            logger.error("agentmail listener: handler failed for %s: %s", message_id, exc)
