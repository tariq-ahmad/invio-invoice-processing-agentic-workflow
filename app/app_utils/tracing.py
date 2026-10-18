# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Best-effort OpenTelemetry instrumentation for the invoice pipeline.

The FastAPI app is created with ``otel_to_cloud=True`` (see fast_api_app.py),
which makes ADK install a global TracerProvider with a Cloud Trace exporter and
emit built-in spans (``invocation`` -> ``invoke_workflow`` -> ``invoke_node`` ->
``call_llm`` / ``execute_tool``). ADK opens an ``invoke_node <name>`` span as the
*current* span for every FunctionNode, so we ENRICH that span with the
business/agentic decision (route taken, flags, PO match, HITL outcome, …) rather
than creating a child. This is what gives Cloud Trace complete transparency into
*why* each node routed the way it did.

Design rules (mirroring the _model_timing callbacks in app/agent.py):
  * Use the ambient global tracer — never build our own provider/exporter.
  * Instrumentation must NEVER break a pipeline run: every span operation is
    guarded and swallows its own exceptions.
  * Financial / vendor PII is gated behind INVOICE_TRACE_VERBOSE (default off).
"""

import functools
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

# Resolves against the same global provider ADK installed via otel_to_cloud=True.
_tracer = trace.get_tracer(__name__)

_TRUE = frozenset({"1", "true", "yes", "on"})


def _verbose() -> bool:
    """Whether to emit sensitive business values (vendor, total, …) on spans."""
    return os.environ.get("INVOICE_TRACE_VERBOSE", "false").strip().lower() in _TRUE


def _coerce(value: Any) -> Any:
    """Coerce an attribute value to an OTel-safe scalar (or list of scalars)."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [v if isinstance(v, (str, bool, int, float)) else str(v) for v in value]
    return str(value)


class _SpanProxy:
    """Thin, exception-safe wrapper over a span. All operations no-op silently
    if the span is not recording (e.g. INVALID_SPAN) or anything goes wrong."""

    __slots__ = ("_span",)

    def __init__(self, span: trace.Span):
        self._span = span

    def set(self, key: str, value: Any) -> None:
        """Set an always-on attribute. Skips None values."""
        try:
            if value is None:
                return
            if self._span.is_recording():
                self._span.set_attribute(key, _coerce(value))
        except Exception:  # never let instrumentation break a run
            logger.debug("tracing: set(%s) failed", key, exc_info=True)

    def set_verbose(self, key: str, value: Any) -> None:
        """Set a PII-sensitive attribute only when INVOICE_TRACE_VERBOSE is on."""
        if _verbose():
            self.set(key, value)

    def event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        try:
            if self._span.is_recording():
                attrs = {k: _coerce(v) for k, v in (attributes or {}).items()}
                self._span.add_event(name, attributes=attrs)
        except Exception:
            logger.debug("tracing: event(%s) failed", name, exc_info=True)

    def error(self, exc: BaseException) -> None:
        try:
            if self._span.is_recording():
                self._span.record_exception(exc)
                self._span.set_status(Status(StatusCode.ERROR, str(exc)))
        except Exception:
            logger.debug("tracing: error() failed", exc_info=True)

    def ok(self) -> None:
        """Mark the span as a successfully completed transaction (StatusCode.OK)."""
        try:
            if self._span.is_recording():
                self._span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.debug("tracing: ok() failed", exc_info=True)


@contextmanager
def enrich_node_span(node_name: str) -> Iterator[_SpanProxy]:
    """Enrich the current ADK ``invoke_node`` span with decision attributes.

    Yields a :class:`_SpanProxy`. If no span is currently recording (e.g. running
    a node outside the ADK runner), opens a fallback ``invoice_node <name>`` span
    so attributes are never silently dropped. If even that fails, yields a no-op
    proxy. Body exceptions always propagate unchanged. The span status is always
    set explicitly — ``OK`` when the node completes without error, ``ERROR``
    (with the exception recorded) when it doesn't — so Cloud Trace shows a real
    success/failure verdict instead of leaving every span ``Unset``.
    """
    span = trace.get_current_span()
    own_cm = None
    if span is None or not span.is_recording():
        try:
            own_cm = _tracer.start_as_current_span(f"invoice_node {node_name}")
            span = own_cm.__enter__()
        except Exception:
            logger.debug("tracing: failed to start span for %s", node_name, exc_info=True)
            span, own_cm = trace.INVALID_SPAN, None

    proxy = _SpanProxy(span)
    proxy.set("invoice.node", node_name)
    try:
        yield proxy
    except BaseException as exc:
        proxy.error(exc)
        if own_cm is not None:
            try:
                own_cm.__exit__(type(exc), exc, exc.__traceback__)
            except Exception:
                logger.debug("tracing: span __exit__ (error) failed", exc_info=True)
            own_cm = None
        raise
    else:
        proxy.ok()
    finally:
        if own_cm is not None:
            try:
                own_cm.__exit__(None, None, None)
            except Exception:
                logger.debug("tracing: span __exit__ failed", exc_info=True)


def traced_external(span_name: str, system: str = "agentmail") -> Callable:
    """Decorator: wrap a synchronous external call (AgentMail REST) in its own span.

    Records ``external.system`` and ``external.success``, and sets the span
    status explicitly (``OK`` on success, ``ERROR`` with the exception recorded
    on failure) so Cloud Trace shows a real verdict instead of ``Unset``. On
    failure the exception is re-raised so callers keep their existing handling.
    Span duration is the call latency (visible in Trace Explorer).
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                cm = _tracer.start_as_current_span(span_name)
                span = cm.__enter__()
            except Exception:  # tracing must never block the real call
                logger.debug("tracing: failed to start span %s", span_name, exc_info=True)
                return fn(*args, **kwargs)

            _SpanProxy(span).set("external.system", system)
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                proxy = _SpanProxy(span)
                proxy.set("external.success", False)
                proxy.error(exc)
                try:
                    cm.__exit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    logger.debug("tracing: external __exit__ (error) failed", exc_info=True)
                raise
            proxy = _SpanProxy(span)
            proxy.set("external.success", True)
            proxy.ok()
            try:
                cm.__exit__(None, None, None)
            except Exception:
                logger.debug("tracing: external __exit__ failed", exc_info=True)
            return result

        return wrapper

    return decorator
