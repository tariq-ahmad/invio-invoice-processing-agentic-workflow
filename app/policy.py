"""AP policies, read from Firestore with a short-TTL cache and safe fallbacks.

Two documents in the ``policies`` collection (see ``app/config.py`` for names):

``policies/ap_rules``
    hitl_threshold_usd, reconcile_tolerance_pct, required_fields,
    screening_min_strong_signals, min_invoice_no_length, unmatched_po_route
``policies/fx_rates``
    rates (map of ISO currency code -> USD per 1 unit), default_rate

Anyone with Firestore access can edit these in the console; changes take effect
within ``POLICY_CACHE_TTL_SECONDS``. The in-code values in ``app/config.py`` are
the defaults. A policy read NEVER blocks or fails the pipeline:

* document missing            -> that document's in-code defaults
* Firestore unreachable       -> last known good value, else the defaults
* document fails validation   -> last known good value, else the defaults

Pipeline nodes read the policy once per decision and pass it down, so a single
decision is internally consistent even if the document changes mid-run.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import config
from app.firestore_store import ConfigStoreError, get_backend

logger = logging.getLogger(__name__)

AP_RULES_DOC = "ap_rules"
FX_RATES_DOC = "fx_rates"

# InvoiceRecord header fields that carry a FieldValue and so can be "required".
ALLOWED_REQUIRED_FIELDS = ("vendor", "invoice_no", "po_ref", "total", "currency")

# After a failed read, retry sooner than the normal TTL so an outage clears fast.
_RETRY_AFTER_FAILURE_S = 15.0


class ApRules(BaseModel):
    """Validated ``policies/ap_rules`` document. Unknown fields (e.g. updated_by)
    are ignored; every field has an in-code default."""

    model_config = ConfigDict(extra="ignore")

    hitl_threshold_usd: float = Field(default=config.PO_AMOUNT_HITL_THRESHOLD_USD, gt=0)
    reconcile_tolerance_pct: float = Field(default=config.RECONCILE_TOLERANCE_PCT, ge=0, le=1)
    required_fields: list[str] = Field(default_factory=lambda: list(config.REQUIRED_FIELDS))
    screening_min_strong_signals: int = Field(
        default=config.SCREENING_MIN_STRONG_SIGNALS, ge=1, le=3
    )
    min_invoice_no_length: int = Field(default=config.MIN_INVOICE_NO_LENGTH, ge=0, le=50)
    unmatched_po_route: Literal["direct", "indirect"] = config.UNMATCHED_PO_ROUTE
    po_vendor_min_similarity: float = Field(default=config.PO_VENDOR_MIN_SIMILARITY, ge=0, le=1)
    # null in Firestore disables the invoice-vs-PO amount check.
    po_amount_tolerance_pct: float | None = Field(default=config.PO_AMOUNT_TOLERANCE_PCT, ge=0, le=10)
    blocked_po_statuses: list[str] = Field(default_factory=lambda: list(config.BLOCKED_PO_STATUSES))

    @field_validator("blocked_po_statuses")
    @classmethod
    def _normalize_statuses(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(v).strip().lower() for v in value if str(v).strip()))

    @field_validator("required_fields")
    @classmethod
    def _check_required_fields(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(str(v).strip() for v in value))
        if not cleaned:
            raise ValueError("required_fields must not be empty")
        unknown = [v for v in cleaned if v not in ALLOWED_REQUIRED_FIELDS]
        if unknown:
            raise ValueError(
                f"unknown required_fields {unknown}; allowed: {list(ALLOWED_REQUIRED_FIELDS)}"
            )
        return cleaned


class FxRates(BaseModel):
    """Validated ``policies/fx_rates`` document."""

    model_config = ConfigDict(extra="ignore")

    rates: dict[str, float] = Field(default_factory=lambda: dict(config.FX_TO_USD))
    default_rate: float = Field(default=config.FX_DEFAULT_RATE, gt=0)

    @field_validator("rates")
    @classmethod
    def _check_rates(cls, value: dict[str, float]) -> dict[str, float]:
        cleaned = {str(k).strip().upper(): float(v) for k, v in value.items()}
        bad = [k for k, v in cleaned.items() if v <= 0]
        if bad:
            raise ValueError(f"FX rates must be > 0; invalid: {bad}")
        return cleaned


@dataclass(frozen=True)
class Policy:
    """An immutable snapshot of every AP policy value, plus where it came from."""

    hitl_threshold_usd: float
    reconcile_tolerance_pct: float
    required_fields: tuple[str, ...]
    screening_min_strong_signals: int
    min_invoice_no_length: int
    unmatched_po_route: str
    po_vendor_min_similarity: float = config.PO_VENDOR_MIN_SIMILARITY
    po_amount_tolerance_pct: float | None = config.PO_AMOUNT_TOLERANCE_PCT
    blocked_po_statuses: tuple[str, ...] = config.BLOCKED_PO_STATUSES
    fx_rates: dict[str, float] = field(default_factory=dict)
    fx_default_rate: float = 1.0
    version: str = "defaults"

    @classmethod
    def build(cls, rules: ApRules, fx: FxRates, version: str) -> Policy:
        return cls(
            hitl_threshold_usd=rules.hitl_threshold_usd,
            reconcile_tolerance_pct=rules.reconcile_tolerance_pct,
            required_fields=tuple(rules.required_fields),
            screening_min_strong_signals=rules.screening_min_strong_signals,
            min_invoice_no_length=rules.min_invoice_no_length,
            unmatched_po_route=rules.unmatched_po_route,
            po_vendor_min_similarity=rules.po_vendor_min_similarity,
            po_amount_tolerance_pct=rules.po_amount_tolerance_pct,
            blocked_po_statuses=tuple(rules.blocked_po_statuses),
            fx_rates=dict(fx.rates),
            fx_default_rate=fx.default_rate,
            version=version,
        )

    def to_usd(self, amount: float, currency: str | None) -> float:
        """Convert ``amount`` in ``currency`` to USD with this policy's FX table."""
        rate = self.fx_rates.get((currency or "USD").strip().upper(), self.fx_default_rate)
        return amount * rate

    def audit(self) -> dict[str, Any]:
        """The decision-relevant values, recorded in session state.

        Doubles as the audit trail and as what the UI reads to label policy
        values (e.g. "x/3 required fields grounded", "≥2 strong signals"), so
        a label can never drift from the rule that actually ran."""
        return {
            "version": self.version,
            "hitl_threshold_usd": self.hitl_threshold_usd,
            "reconcile_tolerance_pct": self.reconcile_tolerance_pct,
            "required_fields": list(self.required_fields),
            "screening_min_strong_signals": self.screening_min_strong_signals,
            "min_invoice_no_length": self.min_invoice_no_length,
            "unmatched_po_route": self.unmatched_po_route,
            "po_vendor_min_similarity": self.po_vendor_min_similarity,
            "po_amount_tolerance_pct": self.po_amount_tolerance_pct,
            "blocked_po_statuses": list(self.blocked_po_statuses),
        }


# ---------------------------------------------------------------------------
# Defaults (also the seed content for deployment/seed_firestore.py)
# ---------------------------------------------------------------------------


def default_ap_rules() -> dict[str, Any]:
    return ApRules().model_dump()


def default_fx_rates() -> dict[str, Any]:
    return FxRates().model_dump()


def default_policy() -> Policy:
    return Policy.build(ApRules(), FxRates(), "defaults")


# ---------------------------------------------------------------------------
# Loader with TTL cache
# ---------------------------------------------------------------------------


@dataclass
class _Loaded:
    model: Any
    version: str


_lock = threading.Lock()
_policy: Policy | None = None
_loaded: dict[str, _Loaded] = {}
_expires_at: float = 0.0


def reset_policy_cache() -> None:
    """Drop the cached policy so the next read hits the backend (tests, seeding)."""
    global _policy, _expires_at
    with _lock:
        _policy = None
        _loaded.clear()
        _expires_at = 0.0


def _load_doc(doc_id: str, model_cls: type[BaseModel]) -> tuple[_Loaded, bool]:
    """Fetch + validate one policy document. Returns (value, ok).

    ``ok`` is False when the read failed or the document was invalid, in which
    case the value is the last known good one (or the defaults on first load).
    """
    fallback = _loaded.get(doc_id) or _Loaded(model_cls(), "default")
    try:
        doc = get_backend().get(config.POLICIES_COLLECTION, doc_id)
    except ConfigStoreError as exc:
        logger.warning("policy: %s unreadable, keeping %s version: %s", doc_id, fallback.version, exc)
        return fallback, False
    if doc is None:
        # An absent document is a valid state (defaults apply), not a failure.
        return _Loaded(model_cls(), "default"), True
    try:
        return _Loaded(model_cls.model_validate(doc.data), doc.update_time or "unversioned"), True
    except ValidationError as exc:
        logger.error(
            "policy: %s/%s is invalid, keeping %s version: %s",
            config.POLICIES_COLLECTION, doc_id, fallback.version, exc,
        )
        return fallback, False


def get_policy() -> Policy:
    """The current AP policy (cached for ``POLICY_CACHE_TTL_SECONDS``). Never raises."""
    global _policy, _expires_at
    if _policy is not None and time.monotonic() < _expires_at:
        return _policy
    with _lock:
        if _policy is not None and time.monotonic() < _expires_at:
            return _policy
        rules, rules_ok = _load_doc(AP_RULES_DOC, ApRules)
        fx, fx_ok = _load_doc(FX_RATES_DOC, FxRates)
        _loaded[AP_RULES_DOC], _loaded[FX_RATES_DOC] = rules, fx
        version = f"{AP_RULES_DOC}@{rules.version};{FX_RATES_DOC}@{fx.version}"
        if _policy is None or _policy.version != version:
            logger.info("policy: loaded %s", version)
        _policy = Policy.build(rules.model, fx.model, version)
        ttl = config.POLICY_CACHE_TTL_SECONDS
        _expires_at = time.monotonic() + (ttl if rules_ok and fx_ok else min(ttl, _RETRY_AFTER_FAILURE_S))
        return _policy
