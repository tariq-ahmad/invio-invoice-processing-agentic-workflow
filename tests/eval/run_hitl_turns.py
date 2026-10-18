"""Drive a real multi-turn HITL conversation against a live ADK server,
turn by turn, using the raw session API instead of `agents-cli eval
generate`'s Shape B ("N+1") shortcut.

Why this exists: `agents-cli eval generate`'s continued-conversation
handling (see `split_case_history` in
google.agents.cli.eval.cmd_generate) flattens every turn but the last into
fake pre-seeded session history — bare text, no real backend state behind
it — and sends only the LAST user message live. For a HITL case like
[process, reject, send], that means "reject" and "send" are never actually
delivered to a session paused at the human-approval interrupt: the agent
just reprocesses the whole thing from scratch and stops at the first HITL
prompt every time. Confirmed empirically 2026-09-21: draft_rejection_email
never once fired across a full 13-case eval suite that included two
reject-and-send cases.

This script instead creates ONE real session and calls /run_sse
sequentially against it, waiting for each turn's real response before
sending the next — exactly what the frontend does, and what
tests/eval/eval_config.yaml's hitl_message_quality /
vendor_rejection_email_quality metrics actually need real data for.

Usage:
    # Against a deployed Cloud Run URL (baseline / current production models):
    uv run python tests/eval/run_hitl_turns.py --url https://invoice-processing-agent-xxx.run.app

    # Against a local server (candidate / new models already the code default):
    uv run python tests/eval/run_hitl_turns.py

    # One specific case only:
    uv run python tests/eval/run_hitl_turns.py hitl_reject_send_missing_po
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


def _add_agents_cli_to_path() -> None:
    """`agents-cli` is a `uv tool install`, isolated from this project's own
    venv — its package isn't importable via a plain `uv run python` here
    unless we locate and add its site-packages directory ourselves."""
    try:
        import google.agents.cli  # noqa: F401

        return
    except ModuleNotFoundError:
        pass
    candidates = list(Path.home().glob("AppData/Roaming/uv/tools/google-agents-cli/Lib/site-packages"))
    candidates += list(Path.home().glob(".local/share/uv/tools/google-agents-cli/lib/*/site-packages"))
    if not candidates:
        raise SystemExit(
            "Could not locate the google-agents-cli tool install's site-packages. "
            "Is agents-cli installed via `uv tool install`?"
        )
    sys.path.insert(0, str(candidates[0]))


_add_agents_cli_to_path()

from google.agents.cli._adk_client import create_session, run_sse  # noqa: E402
from google.agents.cli.run._local_server import ensure_server, stop_server  # noqa: E402

DATASET = PROJECT_ROOT / "tests" / "eval" / "datasets" / "invoice-pipeline-scenarios.json"
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "traces" / "hitl_turns"
APP_NAME = "app"
USER_ID = "hitl-turn-driver"
SEED_WAIT_SECONDS = 8

# Every Shape B multi-turn case in the canonical dataset. Each maps to the
# fixture tests/fixtures/seed_inbox.py must seed immediately beforehand
# (same fixture the case's turn-0 prompt expects "the latest invoice" to be).
MULTI_TURN_CASES: list[tuple[str, str]] = [
    ("hitl_approve_high_value", "03_high_value_approval.pdf"),
    ("hitl_reject_send_missing_po", "06_malformed_po_missing_fields.pdf"),
    ("hitl_reject_revise_send_zero_total", "08_business_rule_violation_zero_total.pdf"),
    ("hitl_approve_confirm_default_sample", "sample_invoice.pdf"),
]


def _load_case_messages(case_id: str) -> list[str]:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    case = next(c for c in data["eval_cases"] if c["eval_case_id"] == case_id)
    turns = case["agent_data"]["turns"]
    return [t["events"][0]["content"]["parts"][0]["text"] for t in turns]


def _final_text(raw_events: list[dict]) -> str:
    for raw in reversed(raw_events):
        content = raw.get("content")
        if not content:
            continue
        texts = [p.get("text", "") for p in content.get("parts", []) if p.get("text")]
        if texts:
            return "".join(texts)
    return ""


def _pending_interrupt(raw_events: list[dict]) -> str | None:
    """Return the interrupt id of the LAST adk_request_input functionCall in
    this turn's events, or None if the turn ended without one -- mirroring
    how the real frontend decides run() vs resume() (usePipelineStore.ts)."""
    for raw in reversed(raw_events):
        content = raw.get("content") or {}
        for part in content.get("parts") or []:
            fc = part.get("functionCall")
            if fc and fc.get("name") == "adk_request_input":
                return fc.get("id") or fc.get("args", {}).get("interruptId")
    return None


def _build_message(text: str, interrupt_id: str | None) -> dict:
    """Plain new-turn text, or a functionResponse resuming a pending
    interrupt -- exactly frontend/src/api/client.ts's run() vs resume()."""
    if interrupt_id is None:
        return {"role": "user", "parts": [{"text": text}]}
    return {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "id": interrupt_id,
                    "name": "adk_request_input",
                    "response": {"result": text},
                }
            }
        ],
    }


def run_turns(case_id: str, base_url: str) -> dict:
    messages = _load_case_messages(case_id)
    headers = {"Content-Type": "application/json"}
    session_id = create_session(base_url, APP_NAME, USER_ID, headers=headers)
    print(f"[{case_id}] session={session_id}")

    turns_out: list[dict] = []
    last_final_text = ""
    pending_interrupt: str | None = None
    for i, text in enumerate(messages):
        mode = f"resume(interrupt={pending_interrupt!r})" if pending_interrupt else "run() (new turn)"
        print(f"[{case_id}] turn {i}: sending {text!r} via {mode}")
        user_message = _build_message(text, pending_interrupt)
        raw_events = list(
            run_sse(
                base_url,
                APP_NAME,
                session_id,
                user_message=user_message,
                headers=headers,
                user_id=USER_ID,
            )
        )
        events_out = [{"author": "user", "content": {"role": "user", "parts": [{"text": text}]}}]
        for raw in raw_events:
            content = raw.get("content")
            state_delta = raw.get("actions", {}).get("stateDelta")
            if not content and not state_delta:
                continue
            event = {"author": raw.get("author"), "content": content, "event_time": raw.get("timestamp")}
            if state_delta:
                event["state_delta"] = state_delta
            events_out.append(event)
        turns_out.append({"turn_index": i, "events": events_out})
        this_turn_text = _final_text(raw_events)
        last_final_text = this_turn_text or last_final_text
        print(f"[{case_id}] turn {i} response:\n{this_turn_text[:400]}\n")
        pending_interrupt = _pending_interrupt(raw_events)

    return {
        "eval_case_id": case_id,
        "agent_data": {"turns": turns_out},
        "responses": [{"response": {"role": "model", "parts": [{"text": last_final_text}]}}],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case_ids", nargs="*", help="Specific case ids to run; defaults to all multi-turn cases.")
    parser.add_argument("--url", default=None, help="Live server URL. Omit to boot a local server (new models, per app/models.py).")
    args = parser.parse_args()

    known = dict(MULTI_TURN_CASES)
    selected = args.case_ids or list(known)
    missing = [c for c in selected if c not in known]
    if missing:
        raise SystemExit(f"Unknown or non-multi-turn case id(s): {missing}. Known: {list(known)}")

    started_here = False
    pid = None
    if args.url:
        base_url = args.url.rstrip("/")
    else:
        info = ensure_server(PROJECT_ROOT, "app", language="python")
        base_url = f"http://127.0.0.1:{info.port}"
        started_here = info.started
        pid = info.pid

    from tests.fixtures.seed_inbox import seed  # local import: needs app.config, avoid at module load

    try:
        results = []
        for case_id in selected:
            fixture = known[case_id]
            print(f"[{case_id}] seeding {fixture} ...")
            seed(fixture)
            time.sleep(SEED_WAIT_SECONDS)
            results.append(run_turns(case_id, base_url))
    finally:
        if started_here and pid:
            stop_server(PROJECT_ROOT, pid=pid)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f"merged_{'-'.join(selected)[:80]}.json"
    out.write_text(json.dumps({"eval_cases": results}, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    print(f"Next: agents-cli eval grade --traces {out}")


if __name__ == "__main__":
    main()
