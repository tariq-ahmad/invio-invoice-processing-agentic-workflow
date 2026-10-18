"""AgentMail WebSocket listener: dispatch, skip-if-labeled, catch-up, reconnect."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from agentmail import MessageReceivedEvent
from agentmail.core.unchecked_base_model import construct_type

from app import agentmail_listener as al
from app import agentmail_tools


def _event(message_id: str, labels: list[str]) -> MessageReceivedEvent:
    return construct_type(
        type_=MessageReceivedEvent,
        object_={
            "type": "event",
            "event_type": "message.received",
            "event_id": f"evt_{message_id}",
            "message": {
                "message_id": message_id,
                "inbox_id": "ap@agentmail.to",
                "labels": labels,
            },
        },
    )


class _Socket:
    def __init__(self, events, hold_s: float = 0.0):
        self._events = events
        self._hold_s = hold_s
        self.subscribed = None

    async def send_subscribe(self, sub):
        self.subscribed = sub

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
            if self._hold_s:
                await asyncio.sleep(self._hold_s)

        return gen()


def _client(socket):
    @asynccontextmanager
    async def connect():
        yield socket

    return SimpleNamespace(websockets=SimpleNamespace(connect=connect))


@pytest.fixture(autouse=True)
def _inbox(monkeypatch):
    monkeypatch.setattr(al, "AGENTMAIL_INBOX_ID", "ap@agentmail.to")
    monkeypatch.setattr(al, "AGENTMAIL_CATCHUP_MINUTES", 0)


@pytest.mark.asyncio
async def test_session_subscribes_and_dispatches_new_messages(monkeypatch):
    seen: list[str] = []

    async def on_message(mid):
        seen.append(mid)

    socket = _Socket(
        [
            _event("m1", ["received"]),
            _event("m2", ["received", agentmail_tools.IN_PROGRESS_LABEL]),
        ]
    )
    monkeypatch.setattr(agentmail_tools, "get_async_client", lambda: _client(socket))

    await al.AgentMailListener(on_message)._session()

    assert socket.subscribed.inbox_ids == ["ap@agentmail.to"]
    assert socket.subscribed.event_types == ["message.received"]
    assert seen == ["m1"]  # m2 already carries an AP label


@pytest.mark.asyncio
async def test_session_runs_catch_up_after_subscribing(monkeypatch):
    seen: list[str] = []

    async def on_message(mid):
        seen.append(mid)

    monkeypatch.setattr(al, "AGENTMAIL_CATCHUP_MINUTES", 30)
    monkeypatch.setattr(
        agentmail_tools, "list_unhandled_message_ids", lambda minutes: ["old1", "old2"]
    )
    # Hold the connection open briefly so the background catch-up task can finish.
    monkeypatch.setattr(
        agentmail_tools, "get_async_client", lambda: _client(_Socket([], hold_s=0.2))
    )

    await al.AgentMailListener(on_message)._session()

    assert seen == ["old1", "old2"]


@pytest.mark.asyncio
async def test_catch_up_failure_is_non_fatal(monkeypatch):
    seen: list[str] = []

    async def on_message(mid):
        seen.append(mid)

    def boom(minutes):
        raise RuntimeError("api down")

    monkeypatch.setattr(al, "AGENTMAIL_CATCHUP_MINUTES", 30)
    monkeypatch.setattr(agentmail_tools, "list_unhandled_message_ids", boom)
    socket = _Socket([_event("live", [])], hold_s=0.1)
    monkeypatch.setattr(agentmail_tools, "get_async_client", lambda: _client(socket))

    await al.AgentMailListener(on_message)._session()

    assert seen == ["live"]


@pytest.mark.asyncio
async def test_handler_error_does_not_kill_the_session(monkeypatch):
    seen: list[str] = []

    async def on_message(mid):
        if mid == "bad":
            raise RuntimeError("boom")
        seen.append(mid)

    socket = _Socket([_event("bad", []), _event("good", [])])
    monkeypatch.setattr(agentmail_tools, "get_async_client", lambda: _client(socket))

    await al.AgentMailListener(on_message)._session()

    assert seen == ["good"]


@pytest.mark.asyncio
async def test_run_reconnects_with_backoff_after_failure(monkeypatch):
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(d):
        sleeps.append(d)
        await real_sleep(0)

    monkeypatch.setattr(al.asyncio, "sleep", fake_sleep)

    listener = al.AgentMailListener(lambda mid: None)
    calls = {"n": 0}

    async def flaky_session():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ConnectionError("dropped")
        raise asyncio.CancelledError

    monkeypatch.setattr(listener, "_session", flaky_session)

    with pytest.raises(asyncio.CancelledError):
        await listener.run()

    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]
    assert listener.status()["reconnects"] == 2
    assert "ConnectionError" in listener.status()["last_error"]


@pytest.mark.asyncio
async def test_run_backs_off_long_on_auth_failure(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)
        raise asyncio.CancelledError

    monkeypatch.setattr(al.asyncio, "sleep", fake_sleep)
    listener = al.AgentMailListener(lambda mid: None)

    class _Unauthorized(Exception):
        status_code = 401

    async def unauthorized():
        raise _Unauthorized

    monkeypatch.setattr(listener, "_session", unauthorized)

    with pytest.raises(asyncio.CancelledError):
        await listener.run()

    assert sleeps == [al._AUTH_FAILURE_DELAY_S]
