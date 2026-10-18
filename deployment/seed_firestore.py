#!/usr/bin/env python3
"""Seed the Firestore config collections from the in-code defaults.

Creates:
  policies/ap_rules        HITL threshold, reconcile tolerance, required fields, ...
  policies/fx_rates        FX table (USD per 1 unit)
  purchase_orders/{PO}     the 40 mock POs (20 direct, 20 indirect)
  posted_invoices/{doc}    a small posted-invoice history (config.MOCK_POSTED_INVOICES) so the
                           duplicate scenarios have something to collide with. In production the
                           pipeline itself writes this collection; skip the demo history with
                           --no-history

Safe to re-run: existing documents are LEFT UNTOUCHED (so edits made in the
console are never clobbered) unless --force is passed. After a release adds new
policy fields, run with --add-missing: it adds only the fields an existing
document does not have yet (with their default values) and never changes a value
that is already there.

Run from the repo root (needs ADC with Firestore write access on the project):

    uv run python deployment/seed_firestore.py --project <gcp-project-id>
    uv run python deployment/seed_firestore.py --project <id> --dry-run
    uv run python deployment/seed_firestore.py --project <id> --add-missing  # new fields only
    uv run python deployment/seed_firestore.py --project <id> --force   # overwrite
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config
from app.duplicate_check import history_document
from app.policy import (
    AP_RULES_DOC,
    FX_RATES_DOC,
    default_ap_rules,
    default_fx_rates,
)

SEEDED_BY = "deployment/seed_firestore.py"


def _documents(include_history: bool = True) -> list[tuple[str, str, dict]]:
    """(collection, doc_id, data) for everything this script manages."""
    docs: list[tuple[str, str, dict]] = [
        (config.POLICIES_COLLECTION, AP_RULES_DOC, default_ap_rules()),
        (config.POLICIES_COLLECTION, FX_RATES_DOC, default_fx_rates()),
    ]
    docs += [
        (config.PURCHASE_ORDERS_COLLECTION, po_no, dict(record))
        for po_no, record in config.MOCK_PO_DATABASE.items()
    ]
    if include_history:
        docs += [
            (config.POSTED_INVOICES_COLLECTION, e["document_id"], history_document(e))
            for e in config.MOCK_POSTED_INVOICES
        ]
    return docs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID"),
                        help="GCP project id (default: $GCP_PROJECT_ID)")
    parser.add_argument("--database", default=config.FIRESTORE_DATABASE,
                        help="Firestore database id (default: %(default)s)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite documents that already exist")
    parser.add_argument("--add-missing", action="store_true",
                        help="add fields missing from existing documents (never changes existing values)")
    parser.add_argument("--no-history", action="store_true",
                        help="do not seed the demo posted-invoice history (real deployments)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be written without writing")
    args = parser.parse_args()
    if not args.project:
        parser.error("--project (or GCP_PROJECT_ID) is required")

    docs = _documents(include_history=not args.no_history)
    if args.dry_run:
        for collection, doc_id, _ in docs:
            print(f"would write {collection}/{doc_id}")
        print(f"{len(docs)} document(s), dry run — nothing written")
        return 0

    import google.auth
    from google.api_core.exceptions import AlreadyExists
    from google.cloud import firestore

    credentials, _ = google.auth.default()
    if hasattr(credentials, "with_quota_project"):
        # User (gcloud ADC) credentials carry no quota project of their own.
        credentials = credentials.with_quota_project(args.project)
    client = firestore.Client(
        project=args.project, database=args.database, credentials=credentials
    )

    created = skipped = overwritten = extended = 0
    for collection, doc_id, data in docs:
        ref = client.collection(collection).document(doc_id)
        payload = {
            **data,
            "updated_at": firestore.SERVER_TIMESTAMP,
            "updated_by": SEEDED_BY,
        }
        if args.force:
            existed = ref.get().exists
            ref.set(payload)
            overwritten += existed
            created += not existed
        else:
            try:
                ref.create(payload)
                created += 1
            except AlreadyExists:
                missing = {}
                if args.add_missing:
                    existing = ref.get().to_dict() or {}
                    missing = {k: v for k, v in data.items() if k not in existing}
                if missing:
                    ref.update(missing)
                    extended += 1
                    print(f"  {collection}/{doc_id}: added {sorted(missing)}")
                else:
                    skipped += 1
    print(
        f"project={args.project} database={args.database}: "
        f"{created} created, {overwritten} overwritten, {extended} extended, "
        f"{skipped} skipped (already exist)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
