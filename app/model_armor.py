"""Model Armor screening for untrusted invoice content.

Invoices are attacker-controlled input: the sender picks the email subject, body
and PDF, and all of it ends up in Gemini prompts whose output drives a posting
decision and an outbound vendor email. This module wraps Google Cloud Model
Armor (https://cloud.google.com/security-command-center/docs/model-armor-overview)
so the pipeline can screen that content at three points:

  - intake      : the email subject/body and the attachment, before any LLM sees
                  them (``fetch_invoice_email``)
  - extraction  : free-text fields the model copied out of the document, before
                  they flow into later prompts / cards / memory
                  (scanned in ``emit_extraction_card``; ``validate_invoice``
                  quarantines the invoice if the scan blocked)
  - outbound    : the drafted vendor rejection email, before it is shown to the
                  reviewer and again before it is sent (``rejection_email``)

Inbound content goes through ``SanitizeUserPrompt`` (prompt-injection / jailbreak,
malicious URLs, ...); the outbound draft goes through ``SanitizeModelResponse``
(adds sensitive-data leak checks).

Everything is a no-op unless ``MODEL_ARMOR_ENABLED`` is true, so rollback is a
config flip. The template is regional, so the client talks to the regional
endpoint ``modelarmor.<MODEL_ARMOR_LOCATION>.rep.googleapis.com`` regardless of
the ``global`` endpoint the Gemini calls use.

Nothing here raises: a failure becomes ``ArmorVerdict.error``, and whether that
stops the invoice is the policy of ``MODEL_ARMOR_FAIL_CLOSED``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from app import config

logger = logging.getLogger(__name__)

# FilterResult is a oneof; these are its members, mapped to the short names used
# in verdicts, logs, MODEL_ARMOR_INBOUND_IGNORE_FILTERS and the UI.
_FILTER_FIELDS: dict[str, str] = {
    "rai_filter_result": "rai",
    "sdp_filter_result": "sdp",
    "pi_and_jailbreak_filter_result": "pi_and_jailbreak",
    "malicious_uri_filter_result": "malicious_uri",
    "csam_filter_filter_result": "csam",
    "virus_scan_filter_result": "virus_scan",
}

# Attachment MIME -> ByteDataItem.ByteItemType member name. Model Armor does not
# auto-detect the file type, so it must be set explicitly. Anything not listed
# (TIFF, WebP, HTML, legacy .doc) can't be scanned and is reported as a note.
_BYTE_TYPES: dict[str, str] = {
    "application/pdf": "PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "WORD_DOCUMENT",
    "image/jpeg": "IMAGE",
    "image/png": "IMAGE",
    "text/plain": "PLAINTEXT_UTF8",
}

_client: Any = None


@dataclass
class ArmorVerdict:
    """Outcome of one Model Armor scan (possibly several API calls)."""

    scope: str  # "intake" | "extraction" | "rejection_email" | "rejection_email_send"
    direction: str = "inbound"  # "inbound" -> SanitizeUserPrompt, "outbound" -> SanitizeModelResponse
    scanned: bool = False
    # Non-exempt filters that matched. Filters listed in
    # MODEL_ARMOR_INBOUND_IGNORE_FILTERS that matched are in ``ignored_filters``.
    filters: list[str] = field(default_factory=list)
    ignored_filters: list[str] = field(default_factory=list)
    # Every filter that ran during the scan (matched or clean), for the UI's checklist.
    checked: list[str] = field(default_factory=list)
    # Why each matched filter fired (see _filter_details), tagged with where it was found.
    findings: list[dict[str, Any]] = field(default_factory=list)
    # One entry per scanned input (email header, body, attachment) and how it fared; see
    # _finish_inputs. Only for scans the caller labelled.
    inputs: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    errors: list[str] = field(default_factory=list)  # collected during the scan; joined into ``error``
    notes: list[str] = field(default_factory=list)
    latency_ms: int | None = None
    enforce: bool = True
    fail_closed: bool = True
    # De-identified text per input text (aligned with ``[text, *texts]``), only when
    # ``scan(mask=True)``; None for a text that could not be masked. In memory only:
    # it is the template's output for the LLM, never written to state.
    masked_texts: list[str | None] = field(default_factory=list)
    # Info types the template's de-identify step replaced (names only).
    masked_info_types: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return bool(self.filters)

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def blocked(self) -> bool:
        """True when this verdict should stop the content (policy applied).

        Inspect-only mode (``MODEL_ARMOR_ENFORCE=false``) never blocks.
        """
        return self.enforce and (self.matched or (self.failed and self.fail_closed))

    def reason(self) -> str:
        """One human-readable line for cards, flags and logs."""
        if self.matched:
            return "Model Armor flagged: " + ", ".join(self.filters)
        if self.failed:
            return f"Model Armor could not scan the content: {self.error}"
        return ""

    def to_state(self) -> dict[str, Any]:
        """JSON-safe snapshot for session state / the UI / the audit trail."""
        return {
            "scope": self.scope,
            "direction": self.direction,
            "scanned": self.scanned,
            "matched": self.matched,
            "blocked": self.blocked,
            "filters": list(self.filters),
            "ignored_filters": list(self.ignored_filters),
            "checked": list(self.checked),
            "findings": [dict(f) for f in self.findings],
            "inputs": [dict(i) for i in self.inputs],
            "masked_info_types": list(self.masked_info_types),
            "error": self.error,
            "notes": list(self.notes),
            "latency_ms": self.latency_ms,
            "enforce": self.enforce,
            "template": config.MODEL_ARMOR_TEMPLATE,
        }


def scope_enabled(scope: str) -> bool:
    """Whether this scan point is switched on (MODEL_ARMOR_SCOPES). The two outbound
    scopes (the draft check and the final pre-send check) share the "outbound" switch."""
    group = "outbound" if scope.startswith("rejection_email") else scope
    return group in config.MODEL_ARMOR_SCOPES


def is_enabled(scope: str | None = None) -> bool:
    """Model Armor is on and, when a scan point is given, that point is switched on."""
    return config.MODEL_ARMOR_ENABLED and (scope is None or scope_enabled(scope))


def _get_client() -> Any:
    """Process-wide regional Model Armor client (built lazily)."""
    global _client
    if _client is None:
        from google.api_core.client_options import ClientOptions
        from google.cloud import modelarmor_v1

        _client = modelarmor_v1.ModelArmorClient(
            transport="rest",
            client_options=ClientOptions(
                api_endpoint=f"modelarmor.{config.MODEL_ARMOR_LOCATION}.rep.googleapis.com"
            ),
        )
    return _client


def _template_path() -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    return (
        f"projects/{project}/locations/{config.MODEL_ARMOR_LOCATION}"
        f"/templates/{config.MODEL_ARMOR_TEMPLATE}"
    )


def _sdp_matched(sdp_result: Any) -> bool:
    """True when the SDP filter matched. Basic mode reports it on ``inspect_result``;
    advanced mode (inspect + de-identify templates) reports it on ``deidentify_result``."""
    from google.cloud import modelarmor_v1 as ma

    found = int(ma.FilterMatchState.MATCH_FOUND)
    return any(
        getattr(sub, "match_state", None) is not None and int(sub.match_state) == found
        for sub in (sdp_result.inspect_result, sdp_result.deidentify_result)
    )


def _matched_filters(sanitization_result: Any) -> list[str]:
    """Short names of every filter that reported MATCH_FOUND."""
    from google.cloud import modelarmor_v1 as ma

    found: list[str] = []
    for fr in dict(getattr(sanitization_result, "filter_results", None) or {}).values():
        raw = ma.FilterResult.pb(fr)
        for fname, short in _FILTER_FIELDS.items():
            if not raw.HasField(fname):
                continue
            sub = getattr(fr, fname)
            if short == "sdp":
                if _sdp_matched(sub):
                    found.append(short)
                continue
            state = getattr(sub, "match_state", None)
            if state is not None and int(state) == int(ma.FilterMatchState.MATCH_FOUND):
                found.append(short)
    return found


_CONFIDENCE = {"LOW_AND_ABOVE": "low", "MEDIUM_AND_ABOVE": "medium", "HIGH": "high"}
_CONFIDENCE_ORDER = {"low": 1, "medium": 2, "high": 3}
_LIKELIHOOD_RANK = ["VERY_UNLIKELY", "UNLIKELY", "POSSIBLE", "LIKELY", "VERY_LIKELY"]
_MAX_URIS = 3
_MAX_URI_CHARS = 200
_MAX_INFO_TYPES = 6


def _like_rank(name: str) -> int:
    return _LIKELIHOOD_RANK.index(name) if name in _LIKELIHOOD_RANK else -1


def _filter_details(sanitization_result: Any) -> list[dict[str, Any]]:
    """Why each matched filter fired, for the UI: one dict per matched filter.

    Only names, categories and levels are kept -- never the flagged text or the
    sensitive values themselves (Model Armor returns info-type names and byte
    ranges, not quotes). The one exception is a malicious link, which is shown as
    plain text so a reviewer can see what was linked. Keys by filter:
      pi_and_jailbreak: ``confidence`` ("low" | "medium" | "high")
      malicious_uri:    ``uris``
      sdp:              ``info_types`` [{name, likelihood}]
      rai:              ``categories``
      virus_scan:       ``threats``
    """
    from google.cloud import modelarmor_v1 as ma

    found = int(ma.FilterMatchState.MATCH_FOUND)
    details: list[dict[str, Any]] = []
    for fr in dict(getattr(sanitization_result, "filter_results", None) or {}).values():
        raw = ma.FilterResult.pb(fr)
        for fname, short in _FILTER_FIELDS.items():
            if not raw.HasField(fname):
                continue
            sub = getattr(fr, fname)
            if short == "sdp":
                if not _sdp_matched(sub):
                    continue
                best: dict[str, str] = {}
                for finding in sub.inspect_result.findings:
                    name = str(finding.info_type)
                    like = ma.SdpFindingLikelihood(finding.likelihood).name
                    if name and (name not in best or _like_rank(like) > _like_rank(best[name])):
                        best[name] = like
                for name in sub.deidentify_result.info_types:
                    best.setdefault(str(name), "")
                details.append(
                    {
                        "filter": short,
                        "info_types": [
                            {"name": n, "likelihood": lk.lower() if lk and not lk.endswith("UNSPECIFIED") else None}
                            for n, lk in list(best.items())[:_MAX_INFO_TYPES]
                        ],
                    }
                )
                continue
            state = getattr(sub, "match_state", None)
            if state is None or int(state) != found:
                continue
            detail: dict[str, Any] = {"filter": short}
            if short == "pi_and_jailbreak":
                level = _CONFIDENCE.get(ma.DetectionConfidenceLevel(sub.confidence_level).name)
                if level:
                    detail["confidence"] = level
            elif short == "malicious_uri":
                detail["uris"] = [
                    str(item.uri)[:_MAX_URI_CHARS]
                    for item in list(sub.malicious_uri_matched_items)[:_MAX_URIS]
                    if item.uri
                ]
            elif short == "rai":
                detail["categories"] = [
                    ma.RaiFilterType(r.filter_type).name.lower()
                    for r in sub.rai_filter_type_results.values()
                    if int(r.match_state) == found
                ]
            elif short == "virus_scan":
                detail["threats"] = [str(n) for v in sub.virus_details for n in v.names][:_MAX_URIS]
            details.append(detail)
    return details


def _merge_findings(into: list[dict[str, Any]], details: list[dict[str, Any]], source: str | None) -> None:
    """Fold one call's details into the verdict, one entry per (filter, source)."""
    for d in details:
        d = {**d, **({"source": source} if source else {})}
        match = next((e for e in into if e["filter"] == d["filter"] and e.get("source") == d.get("source")), None)
        if match is None:
            into.append(d)
            continue
        for key in ("uris", "categories", "threats"):
            for item in d.get(key, []):
                if item not in match.setdefault(key, []):
                    match[key].append(item)
        for it in d.get("info_types", []):
            if all(it["name"] != e["name"] for e in match.setdefault("info_types", [])):
                match["info_types"].append(it)
        if _CONFIDENCE_ORDER.get(d.get("confidence"), 0) > _CONFIDENCE_ORDER.get(match.get("confidence"), 0):
            match["confidence"] = d["confidence"]


def _executed_filters(sanitization_result: Any) -> list[str]:
    """Short names of every filter that actually ran (EXECUTION_SUCCESS), match or not.

    The response only carries the filters the template enables, so this is the
    list the UI shows as "checked" -- never a hard-coded guess at the template.
    """
    from google.cloud import modelarmor_v1 as ma

    ok = int(ma.FilterExecutionState.EXECUTION_SUCCESS)
    ran: list[str] = []
    for fr in dict(getattr(sanitization_result, "filter_results", None) or {}).values():
        raw = ma.FilterResult.pb(fr)
        for fname, short in _FILTER_FIELDS.items():
            if not raw.HasField(fname):
                continue
            sub = getattr(fr, fname)
            if short == "sdp":
                states = [getattr(s, "execution_state", None) for s in (sub.inspect_result, sub.deidentify_result)]
            else:
                states = [getattr(sub, "execution_state", None)]
            if any(s is not None and int(s) == ok for s in states):
                ran.append(short)
    return ran


def _deidentified(sanitization_result: Any) -> tuple[str, str | None, list[str]]:
    """The template's de-identification outcome: (status, masked text, info types).

    ``status`` is one of:
      - ``"masked"``: the de-identify step ran and found something; ``masked text``
        is the whole de-identified text.
      - ``"clean"``: the SDP filter found nothing sensitive, so the original text is
        already safe. (With nothing to de-identify the template returns only an
        ``inspect_result`` -- verified live -- so a missing de-identify result is NOT
        an error on its own.)
      - ``"failed"``: sensitive data was found but not de-identified (a basic-mode
        template, or an empty/failed de-identify step), or the template has no SDP
        filter at all so "clean" can't be established. Raw text must not be used.
    """
    from google.cloud import modelarmor_v1 as ma

    found = int(ma.FilterMatchState.MATCH_FOUND)
    for fr in dict(getattr(sanitization_result, "filter_results", None) or {}).values():
        raw = ma.FilterResult.pb(fr)
        if not raw.HasField("sdp_filter_result"):
            continue
        sdp = fr.sdp_filter_result
        if raw.sdp_filter_result.HasField("deidentify_result"):
            de = sdp.deidentify_result
            if int(de.execution_state) != int(ma.FilterExecutionState.EXECUTION_SUCCESS):
                return "failed", None, []
            if int(de.match_state) == found:
                return ("masked", de.data.text, list(de.info_types)) if de.data.text else ("failed", None, [])
            return "clean", None, []
        inspect = sdp.inspect_result
        if int(inspect.match_state) == found:
            return "failed", None, []  # found, but no de-identify step produced text
        return "clean", None, []
    return "failed", None, []  # no SDP filter in the template


@dataclass
class _Call:
    """One sanitize call."""

    matched: list[str]
    error: str | None = None
    mask_status: str = "failed"  # "masked" | "clean" | "failed", see _deidentified
    masked_text: str | None = None
    info_types: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)  # filters that ran, see _executed_filters
    details: list[dict[str, Any]] = field(default_factory=list)  # why filters matched, see _filter_details


def _call(direction: str, data_item: Any) -> _Call:
    from google.cloud import modelarmor_v1 as ma

    client = _get_client()
    if direction == "inbound":
        request = ma.SanitizeUserPromptRequest(name=_template_path(), user_prompt_data=data_item)
        response = client.sanitize_user_prompt(request=request, timeout=config.MODEL_ARMOR_TIMEOUT_S)
    else:
        request = ma.SanitizeModelResponseRequest(name=_template_path(), model_response_data=data_item)
        response = client.sanitize_model_response(request=request, timeout=config.MODEL_ARMOR_TIMEOUT_S)
    result = response.sanitization_result
    matched = _matched_filters(result)
    error = None
    # PARTIAL/FAILURE means some filters did not run; a match is still a match,
    # but "no match" is not trustworthy.
    if not matched and int(result.invocation_result) != int(ma.InvocationResult.SUCCESS):
        error = f"invocation_result={ma.InvocationResult(result.invocation_result).name}"
    status, masked_text, info_types = _deidentified(result)
    return _Call(matched, error, status, masked_text, info_types, _executed_filters(result), _filter_details(result))


def _split_text(text: str, limit: int) -> list[str]:
    """Split on line breaks into pieces of at most ``limit`` chars (hard-splitting a
    single oversize line), so no value is cut in half at a boundary."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    cur: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                out.append("".join(cur))
                cur, size = [], 0
            out.append(line[:limit])
            line = line[limit:]
        if size + len(line) > limit and cur:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        out.append("".join(cur))
    return out


# Chunks scanned per text; beyond this a text is truncated (scan only) or, when
# masking is required, refused -- masking only a prefix would leak the rest.
_MAX_CHUNKS = 10

_NO_DEID_HINT = (
    "sensitive data was found but the template returned no de-identified text -- it needs "
    "the advanced Sensitive Data Protection setting with an inspect AND a de-identify "
    "template, and the Model Armor service agent needs roles/dlp.user and roles/dlp.reader")


def _note_checked(verdict: ArmorVerdict, call: _Call) -> None:
    """Fold the filters a call ran (and any it matched) into ``verdict.checked``."""
    for name in (*call.executed, *call.matched):
        if name not in verdict.checked:
            verdict.checked.append(name)


def _scan_text(
    direction: str, text: str, mask: bool, verdict: ArmorVerdict, label: str | None = None
) -> tuple[list[str], str | None]:
    """Scan one text (in chunks). Returns (matched filters, masked text or None).

    With ``mask`` the returned text is the de-identified version of the WHOLE text
    (the original when nothing was found); an error is recorded on ``verdict`` and
    None returned if any part could not be masked.
    """
    pieces = _split_text(text, config.MODEL_ARMOR_MAX_TEXT_CHARS)
    if len(pieces) > _MAX_CHUNKS:
        if mask:
            verdict.errors.append(f"text of {len(text):,} characters is too long to mask")
            return [], None
        verdict.notes.append(f"text truncated to {_MAX_CHUNKS} chunks for scanning")
        pieces = pieces[:_MAX_CHUNKS]
    matched: list[str] = []
    masked_pieces: list[str] = []
    for piece in pieces:
        from google.cloud import modelarmor_v1 as ma

        try:
            call = _call(direction, ma.DataItem(text=piece))
        except Exception as exc:  # network/auth/quota: becomes a verdict error
            logger.warning("model_armor[%s] call failed: %s", verdict.scope, exc)
            verdict.errors.append(f"{type(exc).__name__}: {exc}")
            return matched, None
        matched += call.matched
        _note_checked(verdict, call)
        _merge_findings(verdict.findings, call.details, label)
        if call.error:
            verdict.errors.append(call.error)
        for name in call.info_types:
            if name not in verdict.masked_info_types:
                verdict.masked_info_types.append(name)
        if mask:
            if call.mask_status == "failed":
                verdict.errors.append(_NO_DEID_HINT)
                return matched, None
            masked_pieces.append(call.masked_text if call.mask_status == "masked" else piece)
    return matched, ("".join(masked_pieces) if mask else None)


def _attachment_item(att_bytes: bytes, mime: str, verdict: ArmorVerdict) -> Any | None:
    """DataItem for an attachment, or None (with a note / error) when it can't be scanned."""
    from google.cloud import modelarmor_v1 as ma

    mime = (mime or "").lower()
    type_name = _BYTE_TYPES.get(mime)
    if type_name == "IMAGE" and not config.MODEL_ARMOR_SCAN_IMAGES:
        verdict.notes.append(
            f"attachment not scanned: image scanning is off (MODEL_ARMOR_SCAN_IMAGES), type {mime}"
        )
        return None
    if type_name is None:
        verdict.notes.append(f"attachment not scanned: unsupported type {mime or 'unknown'}")
        return None
    if len(att_bytes) > config.MODEL_ARMOR_MAX_FILE_BYTES:
        mb = config.MODEL_ARMOR_MAX_FILE_BYTES / (1024 * 1024)
        verdict.errors.append(f"attachment is {len(att_bytes):,} bytes, above the {mb:g} MB scan limit")
        return None
    if len(att_bytes) < 69:
        # Model Armor rejects files under 69 bytes; nothing meaningful to scan.
        verdict.notes.append("attachment not scanned: file is under 69 bytes")
        return None
    return ma.DataItem(
        byte_item=ma.ByteDataItem(
            byte_data_type=ma.ByteDataItem.ByteItemType[type_name],
            # Raw bytes: the client library base64-encodes this field itself
            # (encoding it here too makes Model Armor return FAILURE).
            byte_data=att_bytes,
        )
    )


_TYPE_NAMES = {
    "application/pdf": "PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word document",
    "text/plain": "Text file",
}


def _size_label(n: int) -> str:
    return f"{max(1, round(n / 1024))} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"


def _attachment_detail(att_bytes: bytes, mime: str, name: str | None) -> str:
    """"PDF · 142 KB · invoice.pdf" -- type, size and (display-only) file name."""
    mime = (mime or "").lower()
    kind = _TYPE_NAMES.get(mime) or ("Image" if mime.startswith("image/") else (mime or "Unknown type"))
    parts = [kind, _size_label(len(att_bytes))]
    if name:
        parts.append(name[:80])
    return " · ".join(parts)


def _finish_inputs(verdict: ArmorVerdict, ignored: frozenset[str]) -> None:
    """Settle each input's status once the ignored filters are known: ``flagged`` when a
    non-exempt filter matched, else ``clean``. Inputs already marked ``not_scanned`` or
    ``error`` keep that status."""
    for entry in verdict.inputs:
        if entry["status"] == "pending":
            entry["status"] = "flagged" if any(f not in ignored for f in entry["matched"]) else "clean"


def _attachment_label(mime: str) -> str:
    mime = (mime or "").lower()
    if mime == "application/pdf":
        return "PDF attachment"
    if mime.startswith("image/"):
        return "Image attachment"
    return "Attachment"


def masking_enabled() -> bool:
    """PII masking uses the template's de-identified text, so it needs Model Armor on."""
    return config.PII_MASKING_ENABLED and config.MODEL_ARMOR_ENABLED


def scan(
    scope: str,
    *,
    direction: str = "inbound",
    text: str | None = None,
    texts: list[str] | None = None,
    attachment: tuple[bytes, str] | None = None,
    mask: bool = False,
    labels: list[str] | None = None,
    attachment_name: str | None = None,
) -> ArmorVerdict:
    """Screen text(s) and/or an ``(bytes, mime)`` attachment. Never raises.

    ``text`` / ``texts`` are scanned one call each (long ones in chunks);
    ``direction="outbound"`` uses SanitizeModelResponse (text only). With ``mask``
    the template's de-identified version of each text is returned in
    ``verdict.masked_texts`` (aligned with ``[text, *texts]``); a template that
    returns none is an error, never a silent pass-through. Files cannot be masked.
    ``labels`` names each text (aligned with ``[text, *texts]``) so a finding can say
    where it was found, e.g. "Email body", and each input gets a result in
    ``verdict.inputs``. ``attachment_name`` is shown as plain text only.
    Returns an unscanned verdict when Model Armor is disabled.
    """
    verdict = ArmorVerdict(
        scope=scope,
        direction=direction,
        enforce=config.MODEL_ARMOR_ENFORCE,
        fail_closed=config.MODEL_ARMOR_FAIL_CLOSED,
    )
    if not is_enabled(scope):
        return verdict

    t0 = time.perf_counter()
    try:
        all_texts = ([text] if text is not None else []) + list(texts or [])
        matched: list[str] = []
        scanned_any = False
        for i, t in enumerate(all_texts):
            label = labels[i] if labels and i < len(labels) else None
            if not t or not t.strip():
                verdict.masked_texts.append(t)  # nothing to scan or mask
                if label:
                    verdict.inputs.append(
                        {"label": label, "kind": "text", "status": "not_scanned", "note": "Empty", "matched": []}
                    )
                continue
            scanned_any = True
            errors_before = len(verdict.errors)
            m, masked = _scan_text(direction, t, mask, verdict, label)
            matched += m
            verdict.masked_texts.append(masked)
            if label:
                failed = len(verdict.errors) > errors_before
                verdict.inputs.append(
                    {
                        "label": label,
                        "kind": "text",
                        "status": "error" if failed else "pending",
                        **({"note": verdict.errors[-1]} if failed else {}),
                        "matched": list(dict.fromkeys(m)),
                    }
                )
        if attachment and attachment[0]:
            notes_before, errors_before = len(verdict.notes), len(verdict.errors)
            item = _attachment_item(attachment[0], attachment[1], verdict)
            entry: dict[str, Any] = {
                "label": _attachment_label(attachment[1]),
                "kind": "attachment",
                "detail": _attachment_detail(attachment[0], attachment[1], attachment_name),
                "status": "pending",
                "matched": [],
            }
            if item is None:
                # Not scanned: say why (an oversize file is an error, the rest are notes).
                if len(verdict.errors) > errors_before:
                    entry.update(status="error", note=verdict.errors[-1])
                else:
                    entry.update(status="not_scanned")
                    if len(verdict.notes) > notes_before:
                        entry["note"] = verdict.notes[-1]
            else:
                scanned_any = True
                try:
                    call = _call(direction, item)
                    matched += call.matched
                    entry["matched"] = list(dict.fromkeys(call.matched))
                    _note_checked(verdict, call)
                    _merge_findings(verdict.findings, call.details, _attachment_label(attachment[1]))
                    if call.error:
                        verdict.errors.append(call.error)
                        entry.update(status="error", note=call.error)
                except Exception as exc:
                    logger.warning("model_armor[%s] attachment call failed: %s", scope, exc)
                    verdict.errors.append(f"{type(exc).__name__}: {exc}")
                    entry.update(status="error", note=f"{type(exc).__name__}: {exc}")
            verdict.inputs.append(entry)
        verdict.scanned = scanned_any

        ignored = config.MODEL_ARMOR_INBOUND_IGNORE_FILTERS if direction == "inbound" else frozenset()
        for name in dict.fromkeys(matched):  # de-duplicate, keep order
            (verdict.ignored_filters if name in ignored else verdict.filters).append(name)
        for finding in verdict.findings:
            finding["ignored"] = finding["filter"] in ignored
        _finish_inputs(verdict, ignored)
    except Exception as exc:  # never let screening break the pipeline turn
        logger.warning("model_armor[%s] scan failed: %s", scope, exc)
        verdict.errors.append(f"{type(exc).__name__}: {exc}")
    verdict.error = "; ".join(dict.fromkeys(verdict.errors)) or None
    verdict.latency_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "model_armor[%s/%s] scanned=%s matched=%s ignored=%s masked_types=%s error=%s ms=%s",
        scope,
        direction,
        verdict.scanned,
        verdict.filters,
        verdict.ignored_filters,
        verdict.masked_info_types,
        verdict.error,
        verdict.latency_ms,
    )
    return verdict


async def ascan(scope: str, **kwargs: Any) -> ArmorVerdict:
    """``scan`` off the event loop, for async nodes."""
    return await asyncio.to_thread(scan, scope, **kwargs)


def flatten_record_text(record: dict) -> str:
    """The free-text a model copied out of an invoice, as one scannable string.

    Covers the fields that flow into later prompts, cards and memory: vendor,
    invoice/PO identifiers, line-item names and the per-line ``attributes``
    labels/values the extraction agent records verbatim from the document.
    """

    def val(field_obj: Any) -> str:
        return str(field_obj.get("value") or "") if isinstance(field_obj, dict) else ""

    lines = [val(record.get(k)) for k in ("vendor", "invoice_no", "po_ref")]
    for li in record.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        lines.append(str(li.get("name") or ""))
        for attr in li.get("attributes") or []:
            if isinstance(attr, dict):
                lines.append(f"{attr.get('label') or ''}: {attr.get('value') or ''}")
    return "\n".join(line for line in lines if line.strip())
