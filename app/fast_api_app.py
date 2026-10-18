# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import google.auth
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.adk.cli.fast_api import get_fast_api_app
from google.cloud import logging as google_cloud_logging
from starlette.types import Scope

from app import agentmail_tools
from app.agentmail_listener import AgentMailListener
from app.app_utils import sse_poll
from app.app_utils.sse import SSEUnbufferedMiddleware
from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback
from app.config import AGENTMAIL_INBOX_ID, AGENTMAIL_INTAKE_ENABLED
from app.events_broadcaster import broadcaster

setup_telemetry()
_, project_id = google.auth.default()
logging_client = google_cloud_logging.Client()
logger = logging_client.logger(__name__)
_logger = logging.getLogger(__name__)

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
session_service_uri = None
artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

# Use web=False so ADK's playground doesn't conflict with our React SPA.
# StaticFiles is mounted below when frontend/dist is present (Docker build).
app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=False,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=True,
)
app.title = "invoice-processing-agent"
app.description = "API for interacting with the Agent invoice-processing-agent"

# Keep SSE streams (esp. ADK's built-in /run_sse, which drives the pipeline rail)
# unbuffered on Cloud Run. Without X-Accel-Buffering: no, the front end buffers the
# stream in 64 KB blocks, so the small early pipeline events aren't delivered until
# the long extraction call ends — freezing the UI rail on "Intake". ADK's /run_sse
# omits this header, so we inject it here for every text/event-stream response.
app.add_middleware(SSEUnbufferedMiddleware)

# ── A2A surface (for Agent Registry + managed evaluation) ─────────────────────
# Exposes an A2A agent card at /a2a/app/.well-known/agent-card.json and the
# JSON-RPC endpoint at /a2a/app — the contract Agent Registry indexes and the
# Gemini Enterprise eval service invokes.
#
# We register the routes directly instead of using get_fast_api_app(a2a=True) or
# to_a2a(): ADK 2.2.0's a2a directory-scan path raises UnboundLocalError on
# `json` (a later local `import json` shadows the module inside the function),
# and to_a2a() registers its routes inside a Starlette lifespan, which a parent
# FastAPI app never runs for a mounted sub-app. Appending the prefixed routes
# from A2AStarletteApplication (ADK's own underlying mechanism) avoids both.
from a2a.server.apps import A2AStarletteApplication  # noqa: E402
from a2a.server.request_handlers import DefaultRequestHandler  # noqa: E402
from a2a.server.tasks import (  # noqa: E402
    InMemoryPushNotificationConfigStore,
    InMemoryTaskStore,
)
from a2a.types import AgentCard  # noqa: E402
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH  # noqa: E402
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor  # noqa: E402
from google.adk.artifacts import InMemoryArtifactService  # noqa: E402
from google.adk.auth.credential_service.in_memory_credential_service import (  # noqa: E402
    InMemoryCredentialService,
)
from google.adk.memory import InMemoryMemoryService  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402

from app.agent import root_agent  # noqa: E402


def _build_a2a_runner() -> Runner:
    """Fresh in-memory runner for A2A-driven (eval) invocations of the workflow."""
    return Runner(
        app_name="app",
        node=root_agent,
        artifact_service=InMemoryArtifactService(),
        session_service=InMemorySessionService(),
        memory_service=InMemoryMemoryService(),
        credential_service=InMemoryCredentialService(),
    )


with (Path(AGENT_DIR) / "app" / "agent.json").open(encoding="utf-8") as _card_file:
    _agent_card_data = json.load(_card_file)
# agent.json ships with a placeholder URL; point it at the real deployed
# service via env var instead of hardcoding a live Cloud Run URL in the repo.
if os.environ.get("AGENT_CARD_URL"):
    _agent_card_data["url"] = os.environ["AGENT_CARD_URL"]
_agent_card = AgentCard(**_agent_card_data)

_a2a_star_app = A2AStarletteApplication(
    agent_card=_agent_card,
    http_handler=DefaultRequestHandler(
        agent_executor=A2aAgentExecutor(runner=_build_a2a_runner),
        task_store=InMemoryTaskStore(),
        push_config_store=InMemoryPushNotificationConfigStore(),
    ),
)
for _a2a_route in _a2a_star_app.routes(
    rpc_url="/a2a/app",
    agent_card_url=f"/a2a/app{AGENT_CARD_WELL_KNOWN_PATH}",
):
    app.router.routes.append(_a2a_route)

# ── AgentMail intake (WebSocket) ──────────────────────────────────────────────
# A new message on the AP inbox arrives as a `message.received` WebSocket event
# (app/agentmail_listener.py). We label it in-flight and broadcast it over SSE so
# the browser starts the pipeline.

# In-flight set: prevents the same message from being handled twice when a live
# event and the post-reconnect catch-up both surface it.
_in_flight: set[str] = set()


async def _process_message(message_id: str) -> None:
    """Mark a new message in-flight (AP-InProgress) and broadcast it so the
    pipeline starts.

    Invoice/non-invoice classification happens inside the ADK graph via the
    screening_agent, and pipeline_complete stamps the final outcome label
    (AP-Processed for handled invoices, AP-NoAction for screened-out noise). Here
    we only apply the interim in-progress label, which also serves as the 'seen'
    dedupe marker so the same email is not picked up twice.
    """
    if message_id in _in_flight:
        _logger.info("agentmail: already in-flight for %s, skipping", message_id)
        return
    _in_flight.add(message_id)
    try:
        await asyncio.to_thread(agentmail_tools.label_in_progress, message_id)
        await broadcaster.publish("new_invoice", {"message_id": message_id})
        _logger.warning("agentmail: new_invoice broadcast for %s", message_id)
    except Exception as exc:
        _logger.error("agentmail: processing failed for %s: %s", message_id, exc)
    finally:
        _in_flight.discard(message_id)


_listener = AgentMailListener(_process_message)

# Run the listener on the server's own event loop (the SSE broadcaster uses
# asyncio queues, so it must publish from that loop). `@app.on_event("startup")`
# is silently ignored when ADK's get_fast_api_app() installs a lifespan=, so we
# wrap the existing lifespan context instead.
_adk_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan_with_intake(a: FastAPI):
    task: asyncio.Task | None = None
    if AGENTMAIL_INTAKE_ENABLED and AGENTMAIL_INBOX_ID:
        task = asyncio.create_task(_listener.run(), name="agentmail-listener")
    else:
        _logger.warning(
            "agentmail: intake listener NOT started (enabled=%s inbox=%r)",
            AGENTMAIL_INTAKE_ENABLED,
            AGENTMAIL_INBOX_ID,
        )
    try:
        async with _adk_lifespan(a) as state:
            yield state
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


app.router.lifespan_context = _lifespan_with_intake


@app.get("/intake/status")
def intake_status() -> dict:
    """Listener health: connection state, last event time, reconnect count."""
    return {"enabled": AGENTMAIL_INTAKE_ENABLED, **_listener.status()}


# ── /feedback ─────────────────────────────────────────────────────────────────

@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    logger.log_struct(feedback.model_dump(), severity="INFO")
    return {"status": "success"}


# ── /config/model  Runtime per-agent model selection ─────────────────────────

@app.get("/config/model")
def get_model_config() -> dict:
    """Return the live per-agent models and the allowed set (UI syncs on load)."""
    from app.agent import ALLOWED_MODELS, CURRENT_MODELS

    return {"models": dict(CURRENT_MODELS), "allowed": sorted(ALLOWED_MODELS)}


@app.post("/config/model")
def set_model_config(body: dict) -> dict:
    """Re-point agents to selected Gemini tiers. Body: {"extraction"?, "draft_email"?}."""
    from app.agent import ALLOWED_MODELS, CURRENT_MODELS, set_agent_model

    updates = {k: v for k, v in body.items() if k in CURRENT_MODELS}
    if not updates:
        return {"status": "error", "detail": "no valid agent keys in body"}
    for model in updates.values():
        if model not in ALLOWED_MODELS:
            return {"status": "error", "detail": f"unsupported model {model!r}"}
    for key, model in updates.items():
        set_agent_model(key, model)
    return {"status": "ok", "models": dict(CURRENT_MODELS)}


# ── /stats  Processing stats for the UI's stats drawer (from Firestore) ───────

@app.get("/stats")
def get_stats(response: Response, days: int = 0) -> dict:
    """Totals over completed runs recorded in Firestore (``run_outcomes``).

    ``days`` > 0 limits to the last N days (0 = all recorded runs).
    """
    from app.firestore_store import ConfigStoreError
    from app.run_stats import get_run_stats

    if not 0 <= days <= 3650:
        raise HTTPException(status_code=422, detail="days must be between 0 and 3650")
    response.headers["Cache-Control"] = "no-store"
    try:
        return get_run_stats(days)
    except ConfigStoreError as exc:
        _logger.warning("stats unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Stats are temporarily unavailable") from exc


# ── /config/security  Which security controls are on (UI shows Model Armor steps) ─

@app.get("/config/security")
def get_security_config() -> dict:
    """Report whether Model Armor is enabled so the UI can show its scan steps."""
    from app import config

    return {
        "model_armor": {
            "enabled": config.MODEL_ARMOR_ENABLED,
            "enforce": config.MODEL_ARMOR_ENFORCE,
            "fail_closed": config.MODEL_ARMOR_FAIL_CLOSED,
        },
        "pii_masking": config.PII_MASKING_ENABLED,
    }


# ── /invoices/{message_id}/pdf  Original source document for HITL review ─────

@app.get("/invoices/{message_id}/pdf")
async def get_invoice_document(message_id: str) -> Response:
    """Re-fetch and stream the original invoice attachment from AgentMail by
    message id, for the HITL reviewer to inspect the source document
    directly (not just the agent's extracted summary)."""
    from agentmail.errors import NotFoundError

    try:
        data = await asyncio.to_thread(agentmail_tools.fetch_invoice, message_id)
    except NotFoundError:
        return Response(status_code=404)
    attachment_bytes = data.get("attachment_bytes")
    if not attachment_bytes:
        return Response(status_code=404)
    mime = data.get("attachment_mime") or "application/pdf"
    filename = data.get("attachment_filename") or "invoice.pdf"
    return Response(
        content=attachment_bytes,
        media_type=mime,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ── /events  SSE endpoint ─────────────────────────────────────────────────────

@app.get("/events")
async def sse_events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream. Delivers `new_invoice` events to the browser.

    Replays the last qualifying event (within TTL) immediately on connect so a
    browser that opens after the AgentMail event still gets the trigger.
    Sends `: keep-alive` heartbeats every 20 s to prevent proxy timeouts.
    """
    q = broadcaster.subscribe()

    async def stream():
        try:
            # Replay latest pending event so a freshly-opened tab still gets triggered.
            replay = broadcaster.get_replay()
            if replay:
                yield f"event: {replay['type']}\ndata: {json.dumps(replay['data'])}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=8.0)
                    yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── /events/latest  Polling fallback ─────────────────────────────────────────
# The browser polls this every 5s as a fallback when SSE drops. Returns the
# most recent new_invoice event within TTL, or 204 when nothing is pending.

@app.get("/events/latest")
async def events_latest() -> Response:
    replay = broadcaster.get_replay()
    if replay:
        return Response(
            content=json.dumps(replay),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    return Response(status_code=204)


# ── /run_sse/poll  Pipeline-progress polling fallback ─────────────────────────
# Cloud Run's front end fully buffers /run_sse over HTTP/2 (every browser
# client) until the connection closes, so the pipeline rail otherwise shows
# nothing until the whole run finishes. SSEUnbufferedMiddleware taps each
# `data:` line into an in-memory buffer as ADK produces it; the frontend polls
# here instead of reading the (transport-delayed) stream directly. A small,
# complete JSON response isn't subject to the same buffering.

@app.get("/run_sse/poll")
async def run_sse_poll(token: str, cursor: int = 0) -> dict:
    lines, new_cursor, done = sse_poll.get_since(token, cursor)
    return {"lines": lines, "cursor": new_cursor, "done": done}


# ── /architecture  Interactive end-to-end architecture diagram ────────────────
# Standalone HTML (Mermaid via CDN, animated + hover-to-focus). Registered before
# the SPA catch-all mount below so it wins route resolution on the same app URL.

_ARCH_HTML = Path(AGENT_DIR) / "docs" / "architecture.html"


@app.get("/architecture")
def architecture_page() -> Response:
    """Serve the interactive architecture diagram at /architecture."""
    if _ARCH_HTML.is_file():
        return FileResponse(
            str(_ARCH_HTML),
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )
    return Response(status_code=404, content="architecture.html not found")


# ── /overview  One-page executive brief (print-to-PDF) ────────────────────────

_OVERVIEW_HTML = Path(AGENT_DIR) / "docs" / "overview.html"


@app.get("/overview")
def overview_page() -> Response:
    """Serve the leadership one-pager at /overview."""
    if _OVERVIEW_HTML.is_file():
        return FileResponse(
            str(_OVERVIEW_HTML),
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )
    return Response(status_code=404, content="overview.html not found")


# ── React SPA (production Docker build only) ──────────────────────────────────
# Mount StaticFiles AFTER all API routes. When frontend/dist is absent (local
# dev), Vite's dev server serves the SPA and proxies to this FastAPI server.


class _SpaStaticFiles(StaticFiles):
    """StaticFiles with SPA-correct caching.

    Vite content-hashes asset filenames (index-<hash>.js), so those are safe to
    cache forever. index.html is NOT hashed — it names the current bundle, so it
    must revalidate on every load or the browser pins a stale bundle across
    deploys. Bare StaticFiles sets only an ETag (no Cache-Control), which lets
    browsers heuristically serve a stale index.html and load the old JS.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        ctype = response.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        elif path.startswith("assets"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


_FRONTEND_DIST = Path(AGENT_DIR) / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", _SpaStaticFiles(directory=str(_FRONTEND_DIST), html=True), name="spa")
    _logger.info("Serving React SPA from %s", _FRONTEND_DIST)


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
