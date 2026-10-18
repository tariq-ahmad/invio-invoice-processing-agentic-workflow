# Invio - Graph Based Agentic Workflow — Dev Guide

## What this is
ADK 2.0 agentic workflow for Accounts Payable invoice automation. Fetches AgentMail invoices → Gemini multimodal extraction → AP rule validation → HITL approval → posts to SAP/Coupa (stubbed). Served as a FastAPI app with a React SPA frontend.

## Key structure
```
app/           # Agent code (agent.py = workflow graph, nodes/ = one module per pipeline stage, fast_api_app.py = server)
tests/unit     # AP rule engine, A2UI surface tests
tests/integration  # Workflow execution, FastAPI E2E
frontend/      # React + TypeScript SPA (Vite build)
deployment/    # deploy.sh (code redeploy to Cloud Run), deploy-infra.sh (first-time setup); Dockerfile is at the repo root
docs/          # architecture.html / .mmd diagrams
```

## Critical values — never change unless asked
- **Models:** `gemini-3.8-flash` (extraction_agent); `gemini-3.5-flash-lite` (screening_agent + draft_rejection_email); `gemini-3.1-pro-preview` (extraction size-tiering "pro" tier, unchanged)
- **GCP project / region:** set via `GCP_PROJECT_ID` (deployment scripts) / `GOOGLE_CLOUD_PROJECT` (runtime) env vars, **region:** `us-west1`. See `ENV_SETUP.md` for the full list of environment variables each deployer must fill in with their own values.
- **AgentMail inbox:** set via the `AGENTMAIL_INBOX_ID` env var; API key via `AGENTMAIL_API_KEY` (local) or `AGENTMAIL_API_KEY_SECRET` (Secret Manager, Cloud Run). New mail arrives over an outbound WebSocket (`app/agentmail_listener.py`), so Cloud Run needs `--no-cpu-throttling --min-instances=1`.
- **HITL threshold:** default `PO_AMOUNT_HITL_THRESHOLD_USD = 4_000` (single auto-approval limit; totals above it go to HITL). **Live value comes from Firestore `policies/ap_rules`** — `app/config.py` holds only the defaults/seed. Same for FX rates (`policies/fx_rates`), required fields, reconcile tolerance, screening signal minimum, unmatched-PO route, and the PO ledger (`purchase_orders/*`, via `app/po_store.py`). Read policy through `app/policy.get_policy()`, never import the constants at decision points. **Duplicate detection also reads Firestore** (`posted_invoices`, via `app/duplicate_check.py`), the collection the posting steps write to; `config.MOCK_POSTED_INVOICES` is only seed data. See `ENV_SETUP.md` → Firestore.
- **Verification (replaces self-graded confidence scores):** per-field source grounding (`grounded` / `inferred` / `not_found`) plus arithmetic reconciliation within the policy's `reconcile_tolerance_pct` (default ±1%) — see `app/nodes/validation.py`
- **Intake:** always live AgentMail (the `AGENTMAIL_INBOX_ID` inbox); no mock mode. The fetch node stashes the PDF and the extraction agent re-injects it into its model request.

## Dev commands
| Command | Purpose |
|---------|---------|
| `agents-cli playground` | Interactive local testing |
| `uv run pytest tests/unit tests/integration` | Run tests |
| `agents-cli eval generate` | Run agent on eval dataset |
| `agents-cli eval grade` | Grade traces |
| `agents-cli eval compare` | Regression diff between two grade results |
| `agents-cli eval analyze` | Cluster failure modes |
| `agents-cli eval optimize` | Auto-tune prompts |
| `agents-cli lint` | Code quality check |
| `agents-cli deploy` | Deploy to dev (**requires human approval**) |

## Critical always-on behaviours
- **Google Cloud grounding:** For any question about Google Cloud Platform, ADK, Vertex AI, Gemini, Cloud Run, Pub/Sub, or any other Google developer technology, ALWAYS call `mcp__google-developer-knowledge__answer_query` (or `search_documents` / `get_documents`) BEFORE answering. Never rely solely on training-time knowledge for GCP/ADK APIs, SDK behaviour, quotas, or configuration — ground every answer in the MCP server's response.

## Bug-fix / feature workflow (implement + test + deploy)
For a reported bug or small feature (not a large refactor), follow this sequence end to end rather than stopping after the code change:
1. **Root-cause first.** Read the actual code path before proposing a fix; ground any GCP/ADK/Gemini behavior claim via the MCP docs tool above rather than assuming.
2. **Implement** the fix across backend and frontend as needed — keep `app/models.py`'s `PRICING`/model tables and `frontend/src/lib/cost.ts` in sync when either changes (they're intentionally duplicated, not fetched from the backend).
3. **Test.** Run `uv run pytest tests/unit tests/integration` (must stay fully green) and, if any `frontend/` file changed, `npm run typecheck` from `frontend/` (equivalent to `tsc -b --noEmit`) — must be clean.
4. **Deploy.** Once tests are green, run `bash deployment/deploy.sh` to ship to Cloud Run and confirm the new revision is serving 100% of traffic.
5. Report the root cause, the fix, and the deployed revision — don't just say "done."

## Operational rules
- **Code preservation:** Only modify what the user asked to change. Preserve config values, comments, formatting.
- **Never change the model** unless explicitly asked.
- **Model 404 errors:** Fix `GOOGLE_CLOUD_LOCATION` (use `us-west1` or `global`), not the model name.
- **ADK tool imports:** Import the tool instance: `from google.adk.tools.load_web_page import load_web_page`
- **Run Python with `uv`:** `uv run python script.py` (run `agents-cli install` first)
- **Stop on repeated errors:** 3+ identical errors → fix the root cause, don't retry.
- **Terraform conflicts (409):** Use `terraform import`, not re-create.
- **Frontend build** is embedded in Docker (Node 20 stage → `frontend/dist`); served as static SPA by FastAPI.
