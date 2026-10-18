"""Cross-node module state for the invoice-processing pipeline.

Kept in its own module so the intake node (writer), the extraction node (reader),
the extraction ``before_model_callback`` in ``app/agent.py``, and the tests all
share the exact same object via ``app.nodes._INVOICE_PDF``.
"""

# PDF hand-off channel: the extraction agent runs as a single-turn Workflow node
# (ADK forces include_contents="none"), so the fetch node's user-role PDF turn is
# windowed out of extraction's request and the model never sees the document.
# We stash the raw PDF bytes here keyed by invocation_id; the extraction
# before_model_callback (app/agent.py) re-injects them into its request and pops
# the entry. Kept off ctx.state deliberately so ~100 KB of PDF never rides the
# SSE/state wire to the browser.
_INVOICE_PDF: dict[str, bytes] = {}

# PII-masked email text for the LLMs, keyed by invocation_id: {original text part:
# masked text part}. The intake node fills it (Model Armor's de-identified text,
# app/model_armor.py); the screening
# and extraction before_model_callback swaps the masked text into the model
# request (app/model_callbacks.py) so the UI/state keep the original. Popped by
# pipeline_complete. Held in memory only -- it contains the unmasked originals as
# keys, so it must never be written to session state.
_MASKED_TEXT: dict[str, dict[str, str]] = {}
