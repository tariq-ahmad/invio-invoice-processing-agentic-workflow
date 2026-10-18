# Invio - Graph Based Agentic Workflow — A2UI demo UI

A polished, animation-rich chat interface for the invoice-processing ADK agent.
It connects to the agent's existing FastAPI stream and renders **agent-emitted
A2UI surfaces** (extraction card, human-approval gate, editable rejection email)
with the official [`@a2ui/react`](https://www.npmjs.com/package/@a2ui/react)
renderer, alongside an animated pipeline timeline.

Stack: Vite · React 19 · TypeScript · Tailwind v4 · Framer Motion · Zustand ·
`@a2ui/react` (v0.9 catalog) · react-markdown · canvas-confetti.

## Run the demo (two terminals)

**1. Start the ADK agent (backend) on :8000**

```bash
# from the repo root (needs Google ADC for Vertex AI, plus AGENTMAIL_API_KEY / AGENTMAIL_INBOX_ID):
uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port 8000
```

A2UI emission is on by default; toggle with `ENABLE_A2UI=false` to fall back to
plain markdown (e.g. for the bundled `adk web` playground).

Intake is **live AgentMail** (the inbox configured via the `AGENTMAIL_INBOX_ID` env var) — there is no mock intake
mode. Ensure `gcloud auth application-default login` is current for Vertex AI,
and have a fresh invoice email waiting in the inbox (see **Demo
prerequisites** below).

**2. Start the UI (frontend) on :5173**

```bash
cd frontend
npm install      # first time only
npm run dev
```

Open http://localhost:5173. Vite proxies `/apps`, `/run_sse`, … to the agent on
:8000, so the browser sees a single origin (no CORS config needed).

### Demo prerequisites (live AgentMail path)

The agent uses **live AgentMail intake**: click *Fetch & process latest invoice* and
it pulls the newest invoice email (subject contains "Invoice"/"INV-"/"Bill", has
a PDF, < 15 min old, not yet labelled `AP-Processed`) from the configured
AgentMail inbox. For a live run you need:

- Valid Google ADC (`gcloud auth application-default login`) for Vertex AI, plus AGENTMAIL_API_KEY and AGENTMAIL_INBOX_ID.
- A fresh invoice email in that inbox (send one just before the demo).

To force the interactive A2UI surfaces (approval gate + rejection email), use an
invoice whose total exceeds the AP threshold (> \$10,000) — this trips
`human_review` and pauses the pipeline for your decision.

## How it connects (architecture)

```
React SPA (this app)                         ADK FastAPI (app/fast_api_app.py)
  AdkClient.run()        ── POST /run_sse ──►  Workflow graph
  parseSse() ◄────────── text/event-stream ──  emits text + A2UI inline-data parts
  eventInterpreter:                            (application/a2ui+json)
    • node path -> stage  (timeline)
    • stateDelta          (summary card)
    • A2UI messages       -> MessageProcessor -> <A2uiSurface/>
    • RequestInput        -> awaiting-input
  Button action ─ POST /run_sse (functionResponse) ─► resumes the HITL node
```

- `src/api/` — typed client, SSE parser, event interpreter.
- `src/store/usePipelineStore.ts` — Zustand store + streaming loop + stage map.
- `src/a2ui/` — `MessageProcessor` wiring + `A2uiHost` + `actionBridge`.
- `src/components/` — shell, animated `PipelineTimeline`, conversation, results.

## Working with a run

- **Expand all / Auto-fold / Focus** (feed header): every step is shown in full by
  default. *Auto-fold* folds finished steps to a one-line summary (click to expand, or
  use the hover chevron to fold again); *Focus* shows only the current step. The choice
  is remembered. Clicking a node in the pipeline graph jumps to
  (and expands) its card; hover a node for model, latency, tokens and cost.
- **Why line**: the result card opens with one sentence naming the
  deciding factor, derived from the backend's rubric / policy snapshot (`src/lib/why.ts`).
- **Export** (feed header): *Download audit record* (JSON), *Print / Save as PDF*
  (light, paginated report layout), and the replay actions below.
- **Replay**: every live run is recorded in memory. *Export → Replay last run* plays it
  back through the real UI with no backend (play/pause, 0.5×–Max, scrubber). *Download
  recording* saves it; *Load a recording…* replays a saved file. A recorded human
  decision pauses at its decision point; **Continue** plays the decision that was made.
  Replays never count toward session stats. Recordings contain the invoice data shown.
- **Alerts**: when the tab is in the background, the title and favicon show that an
  approval needs you; the bell enables a desktop notification.
- **Stats** (chart icon): processing stats read live from Firestore through `GET /stats`
  (the backend writes one `run_outcomes` document per finished run, see `ENV_SETUP.md`).
  Shared by everyone, with a 24 h / 7 d / 30 d / All range; refreshes while open and after
  each run completes. Replays are never recorded.

Tests (`npm test`) cover the pure logic and run the replay engine and the whole app
shell headlessly in happy-dom.

## Build

```bash
npm run build      # tsc -b && vite build  ->  dist/
```

For a single-origin deployment, the built `dist/` can be mounted by the ADK
server (e.g. `app.mount("/ui", StaticFiles(directory="frontend/dist", html=True))`).
