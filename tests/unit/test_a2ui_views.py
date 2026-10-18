"""Unit tests for app/a2ui_views.py — the deterministic A2UI surface builders.

Surfaces are validated against the bundled "basic" catalog JSON Schema (spec
v0.9). The a2ui-agent-sdk's own validator has two bugs on this combination
(Windows ``os.path.join`` mangles schema URIs with backslashes, and its custom
v0.9 path only triggers for the exact string "0.9", not "0.9.1"), so we build a
correctly-wired ``jsonschema`` validator from the catalog's schemas here.

The schema-dependent tests are skipped when a2ui-agent-sdk is not installed; the
structural/contract tests always run.
"""

from __future__ import annotations

import copy
import json

import pytest

from app import a2ui_views as v
from app import memory as mem

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

RECORD = {
    "vendor": {"value": "Acme Corp", "source_grounding": "grounded"},
    "invoice_no": {"value": "INV-2024-001", "source_grounding": "grounded"},
    "po_ref": {"value": "PO-2024-100", "source_grounding": "inferred"},
    "total": {"value": "12500.00", "source_grounding": "grounded"},
    "currency": {"value": "USD", "source_grounding": "grounded"},
    "line_items": [
        {"name": "Widgets", "amount": "12000.00",
         "fields": {"Description": "Widgets", "Qty": "100", "Amount": "12000.00"}},
        {"name": "Setup fee", "amount": "500.00",
         "fields": {"Description": "Setup fee", "Amount": "500.00"}},
    ],
}
DRAFT = {"subject": "Regarding Invoice INV-2024-001", "body": "Dear Acme Corp,\n\nWe are unable to process..."}

PRECEDENT = {
    "vendor": "Acme Corp",
    "flag_kinds": ["over_limit"],
    "total_count": 3,
    "approve_count": 3,
    "reject_count": 0,
    "recommendation": "approve",
    "approved_amounts": ["USD 4750.00"],
    "rejected_amounts": [],
    "narrative": mem._narrate("Acme Corp", ["over_limit"], 3, 3, 0, "approve", ["USD 4750.00"], []),
}
PRECEDENT_NO_RECOMMENDATION = {
    "vendor": "Acme Corp",
    "flag_kinds": ["over_limit", "duplicate"],
    "total_count": 1,
    "approve_count": 1,
    "reject_count": 0,
    "recommendation": None,
    "approved_amounts": [],
    "rejected_amounts": [],
    "narrative": mem._narrate("Acme Corp", ["over_limit", "duplicate"], 1, 1, 0, None, [], []),
}


# ---------------------------------------------------------------------------
# A correctly-wired schema validator (works around a2ui-agent-sdk bugs)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def a2ui_validator():
    pytest.importorskip("a2ui")
    pytest.importorskip("jsonschema")
    pytest.importorskip("referencing")

    from a2ui.basic_catalog.provider import BasicCatalog
    from a2ui.schema.manager import A2uiSchemaManager
    from a2ui.schema.utils import wrap_as_json_array
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    mgr = A2uiSchemaManager(version="0.9.1", catalogs=[BasicCatalog.get_config(version="0.9.1")])
    cat = mgr.get_selected_catalog()
    base_dir = cat.s2c_schema["$id"].rsplit("/", 1)[0]
    resources = [
        (base_dir + "/common_types.json", Resource.from_contents(cat.common_types_schema, default_specification=DRAFT202012)),
        (base_dir + "/catalog.json", Resource.from_contents(cat.catalog_schema, default_specification=DRAFT202012)),
        ("catalog.json", Resource.from_contents(cat.catalog_schema, default_specification=DRAFT202012)),
        ("common_types.json", Resource.from_contents(cat.common_types_schema, default_specification=DRAFT202012)),
        (cat.catalog_id, Resource.from_contents(cat.catalog_schema, default_specification=DRAFT202012)),
    ]
    registry = Registry().with_resources(resources)
    schema = copy.deepcopy(wrap_as_json_array(cat.s2c_schema))
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    validator = Draft202012Validator(schema, registry=registry)

    def _validate(messages: list[dict]) -> None:
        errors = list(validator.iter_errors(messages))
        if errors:
            raise AssertionError(f"A2UI schema validation failed: {errors[0].message}")

    return _validate


ALL_SURFACES = {
    "extraction": lambda: v.build_extraction_surface(RECORD),
    "extraction_empty": lambda: v.build_extraction_surface({}),
    "approval": lambda: v.build_approval_surface(RECORD, ["Total exceeds $10,000 threshold"]),
    "approval_noflags": lambda: v.build_approval_surface(RECORD, []),
    "approval_with_precedent": lambda: v.build_approval_surface(
        RECORD, ["Total exceeds $10,000 threshold"], PRECEDENT
    ),
    "email": lambda: v.build_email_review_surface(DRAFT, "vendor@acme.com", 0),
    "email_revision": lambda: v.build_email_review_surface({"subject": "s", "body": "b"}, "v@x.com", 3),
    "memory": lambda: v.build_memory_surface(PRECEDENT),
    "memory_no_recommendation": lambda: v.build_memory_surface(PRECEDENT_NO_RECOMMENDATION),
    "memory_empty": lambda: v.build_memory_surface({}),
}


# ---------------------------------------------------------------------------
# Schema validation (requires a2ui-agent-sdk)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(ALL_SURFACES))
def test_surface_passes_catalog_schema(a2ui_validator, name):
    a2ui_validator(ALL_SURFACES[name]())


# ---------------------------------------------------------------------------
# Structural / contract tests (always run)
# ---------------------------------------------------------------------------


def _components(messages: list[dict]) -> list[dict]:
    for m in messages:
        if "updateComponents" in m:
            return m["updateComponents"]["components"]
    return []


def _collect_child_refs(comp: dict) -> list[str]:
    refs: list[str] = []
    if "child" in comp:
        refs.append(comp["child"])
    children = comp.get("children")
    if isinstance(children, list):
        refs += children
    return refs


@pytest.mark.parametrize("name", list(ALL_SURFACES))
def test_surface_envelope_and_root(name):
    messages = ALL_SURFACES[name]()
    assert messages, "surface must produce at least one message"
    # First message creates the surface; every message carries the version.
    assert "createSurface" in messages[0]
    assert all(m["version"] == v.A2UI_VERSION for m in messages)
    # Exactly one component has id == "root".
    comps = _components(messages)
    roots = [c for c in comps if c.get("id") == "root"]
    assert len(roots) == 1, f"{name}: expected exactly one root, got {len(roots)}"


@pytest.mark.parametrize("name", list(ALL_SURFACES))
def test_component_refs_and_unique_ids(name):
    comps = _components(ALL_SURFACES[name]())
    ids = [c["id"] for c in comps]
    assert len(ids) == len(set(ids)), f"{name}: duplicate component ids"
    id_set = set(ids)
    for c in comps:
        for ref in _collect_child_refs(c):
            assert ref in id_set, f"{name}: component '{c['id']}' references unknown id '{ref}'"


def test_approval_buttons_carry_interrupt_and_decision():
    comps = _components(v.build_approval_surface(RECORD, ["flag"]))
    buttons = {c["id"]: c for c in comps if c.get("component") == "Button"}
    assert set(buttons) == {"approve_btn", "reject_btn"}
    for btn, expected in [("approve_btn", "approve"), ("reject_btn", "reject")]:
        ctx = buttons[btn]["action"]["event"]["context"]
        assert ctx["interrupt_id"] == v.INTERRUPT_APPROVAL == "approval_decision"
        assert ctx["decision"] == expected


def test_approval_precedent_note_present_only_when_provided():
    no_precedent = _components(v.build_approval_surface(RECORD, ["flag"]))
    assert not any(c.get("id") == "memory_note" for c in no_precedent)

    with_precedent = _components(v.build_approval_surface(RECORD, ["flag"], PRECEDENT))
    note = next(c for c in with_precedent if c.get("id") == "memory_note")
    assert "approved every time before (3 of 3)" in note["text"]
    assert "USD 4750.00" in note["text"]
    assert "recommends **approve**" in note["text"]

    zero_precedent = _components(v.build_approval_surface(RECORD, ["flag"], {"total_count": 0}))
    assert not any(c.get("id") == "memory_note" for c in zero_precedent)


def test_memory_surface_reco_text_reflects_recommendation():
    with_reco = _components(v.build_memory_surface(PRECEDENT))
    reco = next(c for c in with_reco if c.get("id") == "reco_body")
    assert "recommends **approve**" in reco["text"]

    without_reco = _components(v.build_memory_surface(PRECEDENT_NO_RECOMMENDATION))
    reco = next(c for c in without_reco if c.get("id") == "reco_body")
    assert "approved every time before (1 of 1)" in reco["text"]
    assert "recommends" not in reco["text"]


def test_memory_surface_falls_back_when_narrative_missing():
    # Defensive fallback path: a precedent dict without a narrative key.
    reco = next(
        c for c in _components(v.build_memory_surface({"total_count": 1, "vendor": "Acme Corp"}))
        if c.get("id") == "reco_body"
    )
    assert "No consistent precedent yet" in reco["text"]


def test_email_buttons_interrupt_id_is_dynamic_and_body_is_bound():
    comps = _components(v.build_email_review_surface(DRAFT, "v@x.com", 2))
    buttons = {c["id"]: c for c in comps if c.get("component") == "Button"}
    send_ctx = buttons["send_btn"]["action"]["event"]["context"]
    assert send_ctx["interrupt_id"] == "email_review_2"
    assert send_ctx["decision"] == "send"
    # Edited subject/body travel back via path bindings.
    assert send_ctx["body"] == {"path": "/email/body"}
    assert send_ctx["subject"] == {"path": "/email/subject"}
    # The body TextField is two-way bound to the data model.
    body_field = next(c for c in comps if c.get("id") == "body_field")
    assert body_field["value"] == {"path": "/email/body"}
    # Data model is seeded with the draft.
    dm = next(m for m in v.build_email_review_surface(DRAFT, "v@x.com", 2) if "updateDataModel" in m)
    assert dm["updateDataModel"]["value"]["email"]["subject"] == DRAFT["subject"]


def test_extraction_surface_renders_line_item_grounding():
    record = {
        **RECORD,
        "line_items": [
            {"name": "Widgets", "amount": "12000.00", "source_grounding": "grounded"},
            {"name": "Setup fee", "amount": "500.00", "source_grounding": "inferred"},
        ],
    }
    comps = _components(v.build_extraction_surface(record))
    li0_g = next(c for c in comps if c.get("id") == "li0_g")
    li1_g = next(c for c in comps if c.get("id") == "li1_g")
    assert li0_g["text"] == "Grounded"
    assert li1_g["text"] == "Inferred"
    # A line item with no source_grounding at all fails safe to "Not found".
    no_grounding = _components(
        v.build_extraction_surface({**RECORD, "line_items": [{"name": "X", "amount": "1.00"}]})
    )
    assert next(c for c in no_grounding if c.get("id") == "li0_g")["text"] == "Not found"


def test_extraction_surface_renders_line_item_attributes():
    record = {
        **RECORD,
        "line_items": [
            {
                "name": "Fiber Leased Line",
                "amount": "298.14",
                "attributes": [
                    {"label": "Circuit ID", "value": "IS-10001"},
                    {"label": "Site", "value": "Denver"},
                ],
                "source_grounding": "grounded",
            },
            {"name": "Setup fee", "amount": "500.00", "source_grounding": "inferred"},
        ],
    }
    comps = _components(v.build_extraction_surface(record))
    li0_attrs = next(c for c in comps if c.get("id") == "li0_attrs")
    assert li0_attrs["text"] == "Circuit ID: IS-10001 · Site: Denver"
    # A line item with no attributes renders no attrs component at all.
    assert not any(c.get("id") == "li1_attrs" for c in comps)


def test_extraction_surface_renders_incomplete_banner():
    record = {
        **RECORD,
        "line_items": [{"name": "A", "amount": "1.00", "source_grounding": "grounded"}],
        "line_items_complete": False,
    }
    comps = _components(v.build_extraction_surface(record))
    banner = next((c for c in comps if c.get("id") == "li_incomplete"), None)
    assert banner is not None and "incomplete" in banner["text"].lower()
    # Absent when line items are complete.
    ok = _components(v.build_extraction_surface({**record, "line_items_complete": True}))
    assert not any(c.get("id") == "li_incomplete" for c in ok)


def test_a2ui_part_wrapping_round_trips():
    msgs = v.build_approval_surface(RECORD, ["flag"])
    parts = v.a2ui_parts(msgs)
    assert len(parts) == len(msgs)
    for part, original in zip(parts, msgs, strict=True):
        assert part.inline_data.mime_type == v.A2UI_MIME == "application/a2ui+json"
        assert json.loads(part.inline_data.data.decode("utf-8")) == original


def test_a2ui_enabled_env_flag(monkeypatch):
    monkeypatch.delenv("ENABLE_A2UI", raising=False)
    assert v.a2ui_enabled() is True  # default on
    monkeypatch.setenv("ENABLE_A2UI", "false")
    assert v.a2ui_enabled() is False
    monkeypatch.setenv("ENABLE_A2UI", "0")
    assert v.a2ui_enabled() is False
    monkeypatch.setenv("ENABLE_A2UI", "yes")
    assert v.a2ui_enabled() is True
