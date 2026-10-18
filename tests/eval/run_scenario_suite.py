"""Run tests/eval/datasets/invoice-pipeline-scenarios.json against the live
demo inbox, one case at a time, seeding the matching fixture PDF before each.

Intake is live-AgentMail-only (no mock mode, see tests/fixtures/invoices/README.md),
so "process the latest invoice" is only deterministic if exactly one matching
fixture is sitting in the inbox when that case runs. This script automates the
seed -> run -> next-seed sequence documented in
tests/eval/datasets/README.md, then merges the per-case traces into one file
ready for `agents-cli eval grade`.

Usage:
    uv run python tests/eval/run_scenario_suite.py
    uv run python tests/eval/run_scenario_suite.py --url https://invoice-processing-agent-xxx.run.app
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.fixtures.seed_inbox import seed  # noqa: E402

DATASET = Path(__file__).parent / "datasets" / "invoice-pipeline-scenarios.json"
OUTPUT_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "traces" / "scenario_suite"
SEED_WAIT_SECONDS = 8

# Ordered (eval_case_id, fixture_filename | None) pairs. None means "run
# against whatever is currently in the inbox" -- only inbox_empty_no_invoice
# uses this, and only because it must run first, before any fixture is
# seeded, to have a chance of finding a genuinely empty inbox.
SEED_MAP: list[tuple[str, str | None]] = [
    ("inbox_empty_no_invoice", None),
    ("straight_through_direct_spend", "01_direct_straight_through.pdf"),
    ("straight_through_indirect_spend", "02_indirect_straight_through.pdf"),
    ("hitl_approve_high_value", "03_high_value_approval.pdf"),
    ("duplicate_invoice_number_escalation", "04_duplicate_invoice_number.pdf"),
    ("duplicate_po_escalation", "05_duplicate_po.pdf"),
    ("hitl_reject_send_missing_po", "06_malformed_po_missing_fields.pdf"),
    ("low_confidence_blurry_scan_review", "07_blurry_scan.pdf"),
    ("hitl_reject_revise_send_zero_total", "08_business_rule_violation_zero_total.pdf"),
    ("hitl_approve_confirm_default_sample", "sample_invoice.pdf"),
    ("trigger_phrase_casual_confirm", "sample_invoice.pdf"),
    ("trigger_phrase_urgent_eom", "sample_invoice.pdf"),
    ("trigger_phrase_explicit_mailbox", "sample_invoice.pdf"),
]


def _load_cases() -> dict[str, dict]:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    return {c["eval_case_id"]: c for c in data["eval_cases"]}


def run_suite(url: str | None, app_name: str) -> Path:
    cases = _load_cases()
    known_ids = {case_id for case_id, _ in SEED_MAP}
    missing = set(cases) - known_ids
    if missing:
        raise SystemExit(f"SEED_MAP is missing case(s) present in the dataset: {sorted(missing)}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    trace_files: list[Path] = []

    for case_id, fixture in SEED_MAP:
        if fixture is not None:
            print(f"[{case_id}] seeding {fixture} ...")
            seed(fixture)
            time.sleep(SEED_WAIT_SECONDS)
        else:
            print(f"[{case_id}] no fixture to seed (must run against the current inbox state)")

        case_file = OUTPUT_DIR / f"_case_{case_id}.json"
        case_file.write_text(json.dumps({"eval_cases": [cases[case_id]]}, indent=2), encoding="utf-8")

        trace_file = OUTPUT_DIR / f"trace_{case_id}.json"
        cmd = [
            "agents-cli", "eval", "generate",
            "--dataset", str(case_file),
            "--output", str(trace_file),
            "--concurrency", "1",
        ]
        if url:
            cmd += ["--url", url, "--app-name", app_name]
        print(f"[{case_id}] running: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)
        trace_files.append(trace_file)
        case_file.unlink(missing_ok=True)

    merged_cases = []
    for f in trace_files:
        merged_cases.extend(json.loads(f.read_text(encoding="utf-8"))["eval_cases"])

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    merged_path = OUTPUT_DIR / f"merged_{timestamp}.json"
    merged_path.write_text(json.dumps({"eval_cases": merged_cases}, indent=2), encoding="utf-8")
    print(f"\nMerged {len(merged_cases)} case traces -> {merged_path}")
    print(f"Next: agents-cli eval grade --traces {merged_path}")
    return merged_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="Run against a deployed agent instead of a local server.")
    parser.add_argument("--app-name", default="app", help="Agent app name (only used with --url).")
    args = parser.parse_args()
    run_suite(args.url, args.app_name)


if __name__ == "__main__":
    main()
