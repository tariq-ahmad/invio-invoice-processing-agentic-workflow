"""Thin Firestore access layer for runtime configuration (policies + PO master data).

This is the ONLY module that imports ``google.cloud.firestore``. Everything else
(``app/policy.py``, ``app/po_store.py``) talks to a small ``Backend`` interface so
that tests and ``CONFIG_SOURCE=local`` can swap in an in-memory implementation
without GCP credentials.

Reads distinguish three outcomes, which callers rely on:

* document found      -> ``Doc(data, update_time)``
* document not found  -> ``None``
* read failed         -> ``ConfigStoreError`` (never conflated with "not found")

``query_equal`` returns every document whose field equals a value (empty list when
none match; ``ConfigStoreError`` when the query fails -- an outage is never reported
as "no match"). Writes (``create``) are insert-only: they fail rather than overwrite, and raise
``ConfigStoreError`` on any failure, so a caller can never report success for a
record that was not stored.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import CONFIG_SOURCE, FIRESTORE_DATABASE

logger = logging.getLogger(__name__)

# Per-read deadline. Config reads sit on the pipeline's hot path, so a hung
# connection must fail fast rather than stall an invoice.
_READ_TIMEOUT_S = 5.0


class ConfigStoreError(Exception):
    """A configuration read failed (Firestore unavailable) or returned bad data."""


@dataclass(frozen=True)
class Doc:
    """A fetched document: its fields and the server-side last-update time."""

    data: dict[str, Any]
    update_time: str | None = None
    id: str | None = None


class Backend(Protocol):
    def get(self, collection: str, doc_id: str) -> Doc | None: ...

    def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...

    def query_equal(
        self, collection: str, field: str, value: Any, limit: int = 10
    ) -> list[Doc]: ...

    def list_recent(
        self, collection: str, order_by: str, limit: int = 1000
    ) -> list[Doc]: ...


def valid_doc_id(doc_id: str) -> bool:
    """True when ``doc_id`` can be used as a Firestore document id.

    Firestore rejects ids containing "/", ids equal to "." / "..", and ids of the
    form "__.*__". A vendor-printed PO reference can contain a slash, so callers
    treat an unusable id as "no such document" instead of raising.
    """
    if not doc_id or "/" in doc_id or doc_id in (".", ".."):
        return False
    if doc_id.startswith("__") and doc_id.endswith("__"):
        return False
    return len(doc_id.encode("utf-8")) <= 1500


class FirestoreBackend:
    """Reads documents from Cloud Firestore (database ``FIRESTORE_DATABASE``)."""

    def __init__(self, database: str = FIRESTORE_DATABASE) -> None:
        self._database = database
        self._client: Any = None
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    import os

                    from google.cloud import firestore

                    self._client = firestore.Client(
                        project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None,
                        database=self._database,
                    )
        return self._client

    def get(self, collection: str, doc_id: str) -> Doc | None:
        if not valid_doc_id(doc_id):
            return None
        try:
            snap = (
                self._get_client()
                .collection(collection)
                .document(doc_id)
                .get(timeout=_READ_TIMEOUT_S)
            )
        except Exception as exc:
            raise ConfigStoreError(
                f"Firestore read failed for {collection}/{doc_id}: {exc}"
            ) from exc
        if not snap.exists:
            return None
        update_time = snap.update_time.isoformat() if snap.update_time else None
        return Doc(data=snap.to_dict() or {}, update_time=update_time, id=snap.id)

    def query_equal(
        self, collection: str, field: str, value: Any, limit: int = 10
    ) -> list[Doc]:
        """Documents whose ``field`` (dotted path allowed) equals ``value``.

        A single-field equality filter is served by Firestore's automatic
        single-field index, so no composite index has to be deployed."""
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            snaps = (
                self._get_client()
                .collection(collection)
                .where(filter=FieldFilter(field, "==", value))
                .limit(limit)
                .get(timeout=_READ_TIMEOUT_S)
            )
        except Exception as exc:
            raise ConfigStoreError(
                f"Firestore query failed for {collection} where {field}=={value!r}: {exc}"
            ) from exc
        return [
            Doc(
                data=snap.to_dict() or {},
                update_time=snap.update_time.isoformat() if snap.update_time else None,
                id=snap.id,
            )
            for snap in snaps
        ]

    def list_recent(
        self, collection: str, order_by: str, limit: int = 1000
    ) -> list[Doc]:
        """The newest ``limit`` documents, ordered by ``order_by`` descending.

        Ordering on one field is served by Firestore's automatic single-field
        index, so no composite index has to be deployed."""
        try:
            from google.cloud import firestore

            snaps = (
                self._get_client()
                .collection(collection)
                .order_by(order_by, direction=firestore.Query.DESCENDING)
                .limit(limit)
                .get(timeout=_READ_TIMEOUT_S)
            )
        except Exception as exc:
            raise ConfigStoreError(
                f"Firestore list failed for {collection} ordered by {order_by}: {exc}"
            ) from exc
        return [
            Doc(
                data=snap.to_dict() or {},
                update_time=snap.update_time.isoformat() if snap.update_time else None,
                id=snap.id,
            )
            for snap in snaps
        ]

    def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        """Insert a new document (fails if it already exists). Adds a server-side
        ``created_at`` timestamp."""
        if not valid_doc_id(doc_id):
            raise ConfigStoreError(f"invalid Firestore document id {doc_id!r}")
        try:
            from google.cloud import firestore

            self._get_client().collection(collection).document(doc_id).create(
                {**data, "created_at": firestore.SERVER_TIMESTAMP}, timeout=_READ_TIMEOUT_S
            )
        except Exception as exc:
            raise ConfigStoreError(
                f"Firestore write failed for {collection}/{doc_id}: {exc}"
            ) from exc


class InMemoryBackend:
    """Dict-backed stand-in used by tests and ``CONFIG_SOURCE=local``."""

    def __init__(self, collections: dict[str, dict[str, dict[str, Any]]] | None = None) -> None:
        self._collections = collections or {}

    def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._collections.setdefault(collection, {})[doc_id] = dict(data)

    def delete(self, collection: str, doc_id: str) -> None:
        self._collections.get(collection, {}).pop(doc_id, None)

    def get(self, collection: str, doc_id: str) -> Doc | None:
        if not valid_doc_id(doc_id):
            return None
        data = self._collections.get(collection, {}).get(doc_id)
        return Doc(data=dict(data), update_time="in-memory", id=doc_id) if data is not None else None

    def query_equal(
        self, collection: str, field: str, value: Any, limit: int = 10
    ) -> list[Doc]:
        matches: list[Doc] = []
        for doc_id, data in self._collections.get(collection, {}).items():
            current: Any = data
            for part in field.split("."):
                current = current.get(part) if isinstance(current, dict) else None
            if current == value:
                matches.append(Doc(data=dict(data), update_time="in-memory", id=doc_id))
            if len(matches) >= limit:
                break
        return matches

    def list_recent(
        self, collection: str, order_by: str, limit: int = 1000
    ) -> list[Doc]:
        rows = self._collections.get(collection, {}).items()
        ordered = sorted(rows, key=lambda kv: str(kv[1].get(order_by) or ""), reverse=True)
        return [
            Doc(data=dict(data), update_time="in-memory", id=doc_id)
            for doc_id, data in ordered[:limit]
        ]

    def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        if not valid_doc_id(doc_id):
            raise ConfigStoreError(f"invalid document id {doc_id!r}")
        if doc_id in self._collections.get(collection, {}):
            raise ConfigStoreError(f"{collection}/{doc_id} already exists")
        self.set(collection, doc_id, data)


class FailingBackend:
    """Backend whose every read fails — for exercising error handling in tests."""

    def get(self, collection: str, doc_id: str) -> Doc | None:
        raise ConfigStoreError(f"simulated Firestore outage reading {collection}/{doc_id}")

    def query_equal(
        self, collection: str, field: str, value: Any, limit: int = 10
    ) -> list[Doc]:
        raise ConfigStoreError(f"simulated Firestore outage querying {collection}")

    def list_recent(
        self, collection: str, order_by: str, limit: int = 1000
    ) -> list[Doc]:
        raise ConfigStoreError(f"simulated Firestore outage listing {collection}")

    def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        raise ConfigStoreError(f"simulated Firestore outage writing {collection}/{doc_id}")


_backend: Backend | None = None
_backend_lock = threading.Lock()


def _build_default_backend() -> Backend:
    if CONFIG_SOURCE == "local":
        from app.config import (
            MOCK_PO_DATABASE,
            MOCK_POSTED_INVOICES,
            POSTED_INVOICES_COLLECTION,
            PURCHASE_ORDERS_COLLECTION,
        )
        from app.duplicate_check import history_document

        logger.warning(
            "CONFIG_SOURCE=local: serving in-code defaults, Firestore is NOT read"
        )
        # No policy documents -> app/policy.py falls back to its defaults.
        return InMemoryBackend(
            {
                PURCHASE_ORDERS_COLLECTION: {k: dict(v) for k, v in MOCK_PO_DATABASE.items()},
                POSTED_INVOICES_COLLECTION: {
                    e["document_id"]: history_document(e) for e in MOCK_POSTED_INVOICES
                },
            }
        )
    return FirestoreBackend()


def get_backend() -> Backend:
    """Process-wide backend (built lazily so imports never need GCP credentials)."""
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = _build_default_backend()
    return _backend


def set_backend(backend: Backend | None) -> None:
    """Install a backend (tests) or reset to the lazily-built default (``None``)."""
    global _backend
    with _backend_lock:
        _backend = backend
