# Evaluation Datasets

`invoice-pipeline-scenarios.json` is the single dataset for this repo — one
`agents-cli eval` case per real pipeline outcome, each mapped to a fixture in
[`tests/fixtures/invoices/`](../../fixtures/invoices/README.md). There is no
generic scaffold dataset here: intake is live-AgentMail-only (no mock mode), so a
"process the latest invoice" prompt is only a meaningful, repeatable test case
when it's paired with a known fixture seeded into the inbox immediately
beforehand.

## Running the full scenario suite (recommended)

```bash
uv run python tests/eval/run_scenario_suite.py
# or, against a deployed Cloud Run URL instead of a local server:
uv run python tests/eval/run_scenario_suite.py --url https://invoice-processing-agent-xxx.run.app
```

This seeds each fixture (via `tests/fixtures/seed_inbox.py`) and runs its
matching case with `--concurrency 1`, one at a time — required because every
case's prompt resolves to "whatever the newest unprocessed invoice email is,"
so cases must never race each other or an earlier case's leftover fixture.
It merges the per-case traces into one `artifacts/traces/scenario_suite/merged_<timestamp>.json`,
ready for:

```bash
uv run agents-cli eval grade --traces artifacts/traces/scenario_suite/merged_<timestamp>.json
```

## Scenario coverage

| `eval_case_id` | Fixture seeded first | Outcome under test |
|---|---|---|
| `inbox_empty_no_invoice` | *(none — must run first, before any seeding)* | INBOX EMPTY handled gracefully |
| `straight_through_direct_spend` | `01_direct_straight_through.pdf` | STRAIGHT-THROUGH → SAP CIMS |
| `straight_through_indirect_spend` | `02_indirect_straight_through.pdf` | STRAIGHT-THROUGH → Coupa AP |
| `hitl_approve_high_value` | `03_high_value_approval.pdf` | HUMAN REVIEW (>$4k) → approve → SAP CIMS |
| `duplicate_invoice_number_escalation` | `04_duplicate_invoice_number.pdf` | DUPLICATE DETECTED → ServiceNow ticket |
| `duplicate_po_escalation` | `05_duplicate_po.pdf` | DUPLICATE DETECTED → ServiceNow ticket |
| `hitl_reject_send_missing_po` | `06_malformed_po_missing_fields.pdf` | HUMAN REVIEW → reject → REJECTION SENT |
| `low_confidence_blurry_scan_review` | `07_blurry_scan.pdf` | Degraded/scanned input → low confidence → HUMAN REVIEW |
| `hitl_reject_revise_send_zero_total` | `08_business_rule_violation_zero_total.pdf` | Invalid amount → HUMAN REVIEW → reject → revise tone → REJECTION SENT |
| `hitl_approve_confirm_default_sample` | `sample_invoice.pdf` | HUMAN REVIEW (indirect, over limit) → approve → confirm posting → Coupa AP |
| `trigger_phrase_casual_confirm` | `sample_invoice.pdf` | Casual phrasing still triggers intake |
| `trigger_phrase_urgent_eom` | `sample_invoice.pdf` | Urgency-flavored phrasing still triggers intake |
| `trigger_phrase_explicit_mailbox` | `sample_invoice.pdf` | Formal/explicit phrasing still triggers intake |

`inbox_empty_no_invoice` is best-effort: it only proves out the INBOX EMPTY
path if the inbox genuinely has no unprocessed invoice email when the suite
starts (it runs first for exactly this reason), which depends on the demo
mailbox's real state rather than anything this suite controls.

The deterministic AP rule-engine outcome for each fixture (PO matching,
duplicate detection, routing) is separately asserted without any LLM/AgentMail
call in [`tests/unit/test_scenarios.py`](../../unit/test_scenarios.py) — this
suite is for judging the *agentic* behavior on top of that (routing decisions,
HITL message quality, rejection email quality, overall pipeline completion),
via the LLM-judge metrics in [`eval_config.yaml`](../eval_config.yaml).

## Running a single case manually

```bash
uv run python tests/fixtures/seed_inbox.py 03_high_value_approval.pdf
uv run agents-cli eval generate --dataset tests/eval/datasets/invoice-pipeline-scenarios.json --output artifacts/traces/
```

(This runs *every* case in the file against whatever you just seeded, so it's
only meaningful for ad hoc single-case checks — use `run_scenario_suite.py`
for a real multi-scenario run.)

## Dataset format

Each dataset file follows the Gemini Enterprise Agent Platform Evaluation
dataset format. An eval case may use **either** of two shapes — both are
valid input to `agents-cli eval generate`:

**Shape A — single-prompt case:**

```json
{
  "eval_cases": [
    {
      "eval_case_id": "unique_case_id",
      "prompt": {
        "role": "user",
        "parts": [{"text": "User message"}]
      }
    }
  ]
}
```

**Shape B — continued-conversation case (the "N+1" pattern):**
The case carries prior turns in `agent_data` and the last turn ends with a
user message; `eval generate` appends the next agent response.

```json
{
  "eval_cases": [
    {
      "eval_case_id": "unique_case_id",
      "agent_data": {
        "turns": [
          {
            "turn_index": 0,
            "events": [
              {"author": "user",  "content": {"role": "user",  "parts": [{"text": "First user message"}]}},
              {"author": "agent", "content": {"role": "model", "parts": [{"text": "First agent reply"}]}},
              {"author": "user",  "content": {"role": "user",  "parts": [{"text": "Follow-up user message"}]}}
            ]
          }
        ]
      }
    }
  ]
}
```
