# Environment setup

This repo previously had a few real, personal deployment details (a specific
GCP project id, a specific mailbox address, live resource ids) hardcoded in
config and scripts. Those have been externalized to environment variables so
each person/team running their own copy plugs in their own values instead of
silently reusing the original author's.

Copy [`.env.example`](.env.example) to `.env` and fill in the **Required**
section below before running anything. `.env` is already gitignored — never
commit your filled-in copy.

## Required — every deployer must set these with their own values

| Variable | Used by | What to put in it |
|---|---|---|
| `GCP_PROJECT_ID` | `deployment/deploy-infra.sh`, `deployment/deploy.sh`, `deployment/toggle-agentmail-intake.sh`, `deployment/cleanup-gmail-infra.sh` | Your own GCP project id (billing enabled, not the original demo project). |
| `AGENTMAIL_INBOX_ID` | `app/config.py`, `deployment/deploy-infra.sh` | The AgentMail inbox address the service monitors for invoices (e.g. `invoices@agentmail.to`). Create it in the [AgentMail console](https://console.agentmail.to) first. It is both the WebSocket subscription target and the sender of rejection emails. |
| `VENDOR_FALLBACK_EMAIL` | `app/config.py` | Where rejection-email drafts go when a vendor's own address can't be parsed from their invoice. Any address you control (e.g. your AP team's inbox). |
| `AGENTMAIL_API_KEY` | local dev; `deployment/deploy-infra.sh` (one-time) | Your AgentMail API key. Locally it is read straight from the env. `deploy-infra.sh` stores it in Secret Manager (`agentmail-api-key`) and the Cloud Run service reads it at runtime through `AGENTMAIL_API_KEY_SECRET`, so the key is never an env var on the service. |
| `SERVICE_URL` | convenience | Your deployed Cloud Run URL, e.g. `https://invio-invoice-processing-agent-demo-xxxxx.run.app` (printed by `deploy-infra.sh` on first deploy). `GET ${SERVICE_URL}/intake/status` shows whether the AgentMail listener is connected. |
| `AGENT_CARD_URL` | `app/fast_api_app.py` (overrides the placeholder in `app/agent.json`) | `${SERVICE_URL}/a2a/app` — the public URL your A2A agent card advertises. |

## Prerequisites (not env vars, but also personal to you)

- `gcloud auth application-default login` — local dev reads Vertex AI credentials from your own ADC, not a shared account.
- An AgentMail account and inbox. Vendors (or your forwarding rule) must send invoices to that inbox address.

## Optional — safe defaults, only touch these to customize behavior

Optional AgentMail knobs: `AGENTMAIL_CATCHUP_MINUTES` (default 30 — how far back to pick up unhandled mail after each reconnect; 0 disables), `AGENTMAIL_INTAKE_ENABLED` (kill switch, see `deployment/toggle-agentmail-intake.sh`) and `AGENTMAIL_SEED_INBOX_ID` (test fixtures only).

See the bottom two sections of [`.env.example`](.env.example) for the full list
(Gemini model tiers, Agent Memory Bank flags, GCS transport bucket, telemetry,
CORS, etc.) — all of them already have working defaults in `app/config.py` /
`app/models.py` and don't need to be set for a first run.

## Firestore: AP policies and PO master data

Two things are configured in Cloud Firestore instead of code, so anyone with the
right GCP access can change them in the console without a redeploy:

| Collection / document | Holds |
|---|---|
| `policies/ap_rules` | `hitl_threshold_usd` (auto-approval limit), `reconcile_tolerance_pct`, `required_fields`, `screening_min_strong_signals`, `min_invoice_no_length`, `unmatched_po_route`, and the PO checks: `po_vendor_min_similarity` (0 disables), `po_amount_tolerance_pct` (null disables), `blocked_po_statuses` |
| `policies/fx_rates` | `rates` (currency → USD per unit) and `default_rate` |
| `purchase_orders/{PO-number}` | One doc per PO: `type` (`direct` → SAP / `indirect` → Coupa), `vendor`, `amount`, `currency`, `category`, `cost_center`, `gl_account`, `requisitioner`, `status`, `created` |
| `posted_invoices/{document id}` | **Written by the pipeline** (and the source of duplicate detection, below): one document per invoice posted by the "Post Invoice to SAP / Coupa" steps (SAP and Coupa are not connected, so the posting is recorded here). Holds `system`, `document_id`, `status`, `posted_at`, `approval` (`straight_through` / `human_approved`), the invoice header (`vendor`, `invoice_no`, `po_ref`, `total`, `total_amount`, `currency`, `line_item_count`), the matched PO details, a `dedupe` map of normalized lookup keys (`invoice_key`, `po_key`), `validation_flags`, `policy_version` and the source message id. The collection appears on the first posting; `seed_firestore.py` adds a small demo history (skip it with `--no-history`). |
| `run_outcomes/{run id}` | **Written by the pipeline** when a run finishes (`pipeline_complete`), and read by `GET /stats` for the UI's stats drawer: one small document per run that extracted an invoice. Holds `status`, `posted`, `exception`, `human_review`, `touchless`, `recorded_at`, the invoice header (`vendor`, `invoice_no`, `amount`, `currency`, `amount_usd`), `decision_ms`, `cost_usd`, `policy_version` and the source message id. Never the PDF, line items, sender address or email text. Recording is best-effort: a Firestore outage is logged and never fails an invoice. Only runs recorded after this was deployed appear (nothing is back-filled). Override the name with `FIRESTORE_RUN_OUTCOMES_COLLECTION`. |

- **Setup:** `deploy-infra.sh` enables the API, creates the `(default)` database
  (location is permanent; defaults to the Cloud Run region) and grants the runtime
  SA `roles/datastore.user`. Then seed it once:
  `uv run python deployment/seed_firestore.py --project <id>` (re-runnable;
  existing documents are never overwritten unless `--force`). The seed includes a
  small posted-invoice history that the duplicate demo scenarios (fixtures 04 and 05)
  collide with; pass `--no-history` for a real deployment.
- **Edits take effect** within `POLICY_CACHE_TTL_SECONDS` (default 60s). Policy
  edits are validated; an invalid value is ignored (last known good / the in-code
  default stays in force) and an error is logged. A missing policy document means
  the in-code defaults in `app/config.py` apply.
- **Duplicate detection reads `posted_invoices`.** `validate_invoice` queries the same collection the posting
  steps write to, so every invoice the pipeline posts is protected the next time it arrives (there is no
  hard-coded duplicate list). Two checks, first hit wins and routes to the ServiceNow escalation instead of
  posting: the **same invoice number from the same vendor** already posted (matched on a normalized key, so
  `inv-2024-001` = `INV 2024 001`; vendor names compared the way the PO vendor check does), or the **same vendor,
  PO and amount** (same currency) already posted under a different invoice number. A different amount against
  the same PO is a normal partial billing and is not flagged. If the query fails, the run goes to the error
  path (`AP-Error`, retried) instead of letting a possible duplicate through. Only the status `posted`
  counts, and each lookup is a single-field equality query, so no composite index is needed. Not covered: two
  copies of the same invoice processed at the same moment (neither is posted yet when the other is checked).
- **Posting is real or it fails:** the SAP / Coupa steps report "posted" only after the `posted_invoices` write succeeds; a failed write routes to the error path. The step's write latency is shown in the UI on the timeline node and the posting card.
- **PO lookup runs before validation.** `classify_spend` matches the PO right after extraction, so the
  validation checks and the human approver both see it. When the PO is found, validation compares the invoice
  against it: **vendor** (similarity ≥ `po_vendor_min_similarity`, ignoring Inc/LLC etc.), **amount** (invoice
  total in USD may exceed the PO amount by at most `po_amount_tolerance_pct`; a lower amount is a normal partial
  billing) and **status** (not in `blocked_po_statuses`). Any failure routes the invoice to human review. With no
  PO match these checks are skipped (shown as n/a).
- **Failed runs are retried, not marked done.** If a step errors (for example a Firestore outage during the PO
  lookup) the email is labelled `AP-Error` instead of `AP-Processed`, and the UI shows a failure card. `AP-Error`
  is not a "handled" label, so the next intake reconnect catch-up (`AGENTMAIL_CATCHUP_MINUTES`, or
  `toggle-agentmail-intake.sh resume <minutes>` for a longer window) picks the message up again. The old `AP-Error`
  label stays on the message after a successful retry as a record that it failed once.
- **PO lookup failures are errors, not "unmatched":** if Firestore can't be read,
  `classify_spend` routes to its error path rather than defaulting to SAP.
- Each invoice's decision records the policy it was made under in session state
  (`policy_snapshot`) and on the `validate_invoice` span (`policy.version`).
- **Offline dev:** `CONFIG_SOURCE=local` serves the in-code defaults without GCP.
- **Who can edit:** `roles/datastore.user` (or a custom role with
  `datastore.entities.update`) on the project; read-only reviewers need
  `roles/datastore.viewer`.

## Model Armor: screening untrusted invoice content

Invoices are attacker-controlled input (the sender picks the subject, body and PDF), and that content
reaches Gemini prompts whose output drives a posting decision and an outbound vendor email.
[Model Armor](https://cloud.google.com/security-command-center/docs/model-armor-overview) can screen it at three points
(`app/model_armor.py`). **Everything is a no-op until `MODEL_ARMOR_ENABLED=true`.** By default only the **intake**
scan is active, so the one security scan runs right after the email is fetched and before any AI reads it. The
**extraction** and **rejection email** scans below are off unless you add them with
`MODEL_ARMOR_SCOPES=intake,extraction,outbound` (comma-separated: `intake`, `extraction`, `outbound`).

| Where | What is scanned | On a flag |
|---|---|---|
| **Intake** (`fetch_invoice_email`) | email subject + body, and the attachment (PDF, DOCX, text; images only if `MODEL_ARMOR_SCAN_IMAGES=true`) | The content is withheld from every model, the run goes to `quarantine_invoice`, and the email is labelled `AP-Quarantined` (counts as handled, so it is not retried). |
| **Extraction** (`emit_extraction_card`) | vendor, identifiers, line-item names and attribute labels/values the model copied out of the document | `validate_invoice` quarantines the invoice (same terminal `quarantine_invoice` node, `AP-Quarantined`) before any rule, approval or posting. It is **not** sent to a human approver, because the extracted values can't be trusted. |
| **Rejection email** (`review_email_draft`, `send_rejection_email`) | the drafted subject + body (outbound: also checks for sensitive-data leaks) | The review card shows a warning; the **final** text is re-scanned at send time and is not sent while still flagged. |

- **Setup:** create the template in the same region you configure (`MODEL_ARMOR_LOCATION`, default `us-west1`;
  Model Armor is called on its own regional endpoint, independent of the Gemini `global` endpoint). `deploy-infra.sh` enables
  `modelarmor.googleapis.com` and grants the runtime SA `roles/modelarmor.user`; enable it on the service with
  `MODEL_ARMOR_ENABLED=true bash deployment/deploy-infra.sh`, or on an existing service with
  `MODEL_ARMOR_ENABLED=true bash deployment/deploy.sh` (routine `deploy.sh` runs leave the setting alone).
- **Roll out in inspect-only mode first:** `MODEL_ARMOR_ENFORCE=false` records every verdict (session state `armor_*`, span
  attributes `armor.*`, logs) but never blocks. Review them against real invoices, then set it back to `true`.
  Rollback is `MODEL_ARMOR_ENABLED=false` on the service, no redeploy of code.
- **Sensitive Data Protection:** invoices legitimately carry bank details, tax IDs and addresses, so inbound `sdp` matches are
  recorded but not acted on (`MODEL_ARMOR_INBOUND_IGNORE_FILTERS`, default `sdp`). Outbound content is never exempted.
- **When Model Armor can't give a verdict** (API error, attachment over its 4 MB scan limit, partial result) the default is to
  stop (`MODEL_ARMOR_FAIL_CLOSED=true`): at intake and after extraction the email is labelled `AP-Error` and retried on the
  next catch-up; an unscanned rejection email is not sent. Set it to `false` to let such content
  through with a warning. Attachment types that are not scanned are noted on the verdict and not treated as a failure; only the
  email text is scanned for those: TIFF, WebP, HTML, legacy `.doc`, and **JPEG/PNG images** (`MODEL_ARMOR_SCAN_IMAGES`, default off:
  on the `us-west1` template an image scan returns a failure, which under fail-closed would loop every image invoice through
  AP-Error; turn it on only if your template's location supports image scanning). So an injection inside a *photographed* invoice
  is not covered by Model Armor.
- Not covered: reviewer free-text feedback, and the two out-of-band Gemini calls (the large-invoice line-item pass and the
  review-summary card). They consume content already screened at intake / extraction.

## PII masking: what the LLMs read of the email

Sensitive values in the email's subject, body and header summary are replaced (by your DLP de-identify template, e.g.
`[IBAN_CODE]`) in the text the **screening and extraction** agents are given. It uses the *de-identified text Model Armor
returns* when its template has the advanced Sensitive Data Protection setting, so it adds no extra API call: the same scan that
checks for injection also masks. **No-op until `PII_MASKING_ENABLED=true`, and it needs `MODEL_ARMOR_ENABLED=true`.**

- **What is masked, and how, is configured in your DLP templates, not in code.** `invoice_processing_dlp_inspect_template` picks
  the info types (currently the categories `FINANCIAL_ID`, `SECURITY_DATA`, `GOVERNMENT_ID`, `GEOGRAPHIC_DATA`, `PHONE_NUMBER`,
  `PASSPORT`, `DEMOGRAPHIC_DATA`, `CREDIT_CARD_DATA`, `MEDICAL_ID`) and `invoice_processing_dlp_deidentify_template` how
  (replace with the info-type name). Edit them in Sensitive Data Protection; no redeploy.
- **Model Armor template setup** (all in the project that holds the DLP templates, same location as the Model Armor template):
  1. Set the template's Sensitive Data Protection filter to *advanced* and point it at the inspect and de-identify templates.
  2. Grant the **Model Armor service agent** (`service-<project number>@gcp-sa-modelarmor.iam.gserviceaccount.com`)
     `roles/dlp.user` and `roles/dlp.reader`. (`deploy-infra.sh` does this when `PII_MASKING_ENABLED=true`.)
  3. `dlp.googleapis.com` must be enabled (`deploy-infra.sh` does this).
- **A template that is not set up this way is an error, not a silent pass-through.** If masking is on and the template returns no
  de-identified text, the scan fails with a message saying so, and `MODEL_ARMOR_FAIL_CLOSED=true` holds the email (`AP-Error`,
  retried on the next catch-up) instead of sending raw PII to a model. `MODEL_ARMOR_FAIL_CLOSED=false` sends it unmasked with a
  warning.
- **What is *not* masked:** the UI, session state and the reply address keep the original text, and **the attachment (PDF, DOCX,
  image) still goes to Gemini unmasked** — Model Armor cannot de-identify files. Masking the attachment would mean extracting its
  text first and sending that instead of the document, which loses layout and cannot handle scans.
- **Inbound Sensitive Data Protection matches are still not blocked** (`MODEL_ARMOR_INBOUND_IGNORE_FILTERS`, default `sdp`): an
  invoice legitimately carries bank details, so a match is recorded and the text is masked, not rejected. Outbound (the rejection
  email) still blocks on a match.
- **Coverage is whatever the DLP templates detect.** DLP validates formats (obviously fake test values such as `123-45-6789` are
  not treated as real), and broad categories such as `GEOGRAPHIC_DATA` / `DEMOGRAPHIC_DATA` also mask addresses and places in the
  email text. Tested live against the templates as first created: an IBAN, a street address, a phone number and an EIN were
  masked (the EIN is labelled `PHONE_NUMBER`, but masked); **email addresses, plain account numbers (`Account no.: 0001…`), ABA
  routing numbers and SWIFT codes were not.** Add `EMAIL_ADDRESS`, `US_BANK_ROUTING_MICR`, `SWIFT_CODE` and a custom regex info
  type for labelled account numbers to the inspect template if you need them; no code change is needed.
- **Responsible-AI false positives:** the template's `dangerous` filter (HIGH) flagged an email that put a passport number next to
  a live-looking API key, and quarantined it. Ordinary contact lines, tax IDs, SSNs and bank details were not flagged.
- **Where to look:** session state `pii_masking` holds the masked info-type *names* only, never the values; logs show
  `model_armor[intake/inbound] … masked_types=[…]`.

## A note on the live Cloud Run service

`deployment/deploy-infra.sh` deploys with `--allow-unauthenticated`, and no route
checks auth (AgentMail intake is an outbound WebSocket, so there is no public
push endpoint to protect). Every route (the agent API, `/invoices/{id}/pdf`,
`/intake/status`, the SPA) is reachable by anyone who has the service URL. That's a deliberate demo
simplification, not a bug this change fixes — if you deploy your own instance
and share the URL or the repo, treat that URL as effectively public, and don't
point `AGENTMAIL_INBOX_ID` at an inbox you wouldn't want an anonymous visitor to
trigger fetches/PDF reads against.

## Cloud Run settings the AgentMail listener depends on

Intake is a long-lived outbound WebSocket, not a request, so the service must keep
CPU allocated outside requests and stay warm. `deploy-infra.sh` and `deploy.sh`
both pass `--no-cpu-throttling --min-instances=1`; keep `--max-instances=1` too
(one listener, and the SSE broadcaster / HITL sessions live in process memory).
AgentMail does not replay events missed while the service was down, so each
(re)connect runs a catch-up over mail from the last `AGENTMAIL_CATCHUP_MINUTES`
that carries no `AP-*` label.
