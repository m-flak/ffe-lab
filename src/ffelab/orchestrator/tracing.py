"""Tracing: a timeline of every agent step, tool call, token count and dollar.

A **trace** is the record of one orchestrator run; it is made of **spans**,
timed steps nested inside each other:

    agents.run
      round 1
        invoke_agent blind-labeller (FUNC 412)
          chat claude-opus-5-5          <- one model call, with its tokens
          execute_tool blind_view       <- input, output, how long it took
          ...
        invoke_agent red-team (claim bet 17)
        experiment logging_pass         <- the run that resolves the bet

Spans are OpenTelemetry (OTel), the vendor-neutral standard, so the viewer is
a choice made at run time (``--trace``):

* ``phoenix`` (default viewer): Arize Phoenix, local, ``pip install
  arize-phoenix && phoenix serve``, then open http://localhost:6006.
* ``langfuse``: Langfuse cloud or self-hosted (LANGFUSE_HOST,
  LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY).
* ``otlp``: any OTLP collector (OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS).
* ``console``: print spans to stderr. ``none``: tracing off.

Attributes follow the OTel GenAI conventions (``gen_ai.*``), which Langfuse
reads, plus the OpenInference ones (``openinference.span.kind``, ``llm.*``)
that Phoenix reads. Without the ``orchestrator`` extra installed every call
here is a no-op, so the orchestrator runs the same either way.
"""
from __future__ import annotations

import base64
import json
import os
from contextlib import contextmanager
from typing import Any

try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
    HAVE_OTEL = True
except ImportError:                      # pragma: no cover - exercised only without the extra
    HAVE_OTEL = False

TRACER_NAME = "ffelab.orchestrator"
TARGETS = ("phoenix", "langfuse", "otlp", "console", "none")
MAX_ATTR = 4000                          # long tool outputs are truncated in span attributes


def setup(target: str = "none", service: str = "ffelab", provider=None):
    """Install a tracer provider that exports to ``target``. Returns it (or None for 'none').

    Pass ``provider`` to use one you built yourself (tests use an in-memory exporter)."""
    if target not in TARGETS:
        raise ValueError(f"trace target must be one of {TARGETS}")
    if target == "none" and provider is None:
        return None
    if not HAVE_OTEL:
        raise RuntimeError("tracing needs the orchestrator extra: pip install -e '.[orchestrator]'")
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    if provider is None:
        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        if target == "console":
            exporter = ConsoleSpanExporter()
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(**_otlp_args(target))
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def _otlp_args(target: str) -> dict:
    if target == "phoenix":
        host = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
        return {"endpoint": host.rstrip("/") + "/v1/traces"}
    if target == "langfuse":
        host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
        try:
            pk, sk = os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]
        except KeyError as e:
            raise RuntimeError(f"langfuse tracing needs {e.args[0]} set") from None
        auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
        return {"endpoint": host.rstrip("/") + "/api/public/otel/v1/traces",
                "headers": {"Authorization": f"Basic {auth}"}}
    return {}                            # otlp: the exporter reads the standard OTEL_* variables


def shutdown(provider) -> None:
    """Flush buffered spans. Call at the end of a run or the last spans are lost."""
    if provider is not None:
        provider.shutdown()


# ---------------------------------------------------------------- spans
class _NoSpan:
    def set_attribute(self, *a): pass
    def set_attributes(self, *a): pass
    def add_event(self, *a, **k): pass
    def set_status(self, *a): pass
    def record_exception(self, *a): pass
    def is_recording(self): return False
    def end(self, *a): pass


def _clean(v: Any):
    if v is None:
        return None
    if isinstance(v, (bool, int, float, str)):
        return v if not isinstance(v, str) else v[:MAX_ATTR]
    return json.dumps(v, default=str)[:MAX_ATTR]


def set_attrs(span, attrs: dict[str, Any]) -> None:
    for k, v in attrs.items():
        v = _clean(v)
        if v is not None:
            span.set_attribute(k, v)


@contextmanager
def span(name: str, attrs: dict[str, Any] | None = None, parent=None):
    """A span; ``parent`` is a span to nest under explicitly (needed across tasks/callbacks)."""
    if not HAVE_OTEL:
        yield _NoSpan()
        return
    ctx = trace.set_span_in_context(parent) if parent is not None else otel_context.get_current()
    with trace.get_tracer(TRACER_NAME).start_as_current_span(name, context=ctx) as s:
        set_attrs(s, attrs or {})
        try:
            yield s
        except Exception as e:
            s.record_exception(e)
            s.set_status(Status(StatusCode.ERROR, str(e)))
            raise


def start(name: str, attrs: dict[str, Any] | None = None, parent=None):
    """A span you end yourself (``s.end()``), for steps that don't fit a ``with`` block."""
    if not HAVE_OTEL:
        return _NoSpan()
    ctx = trace.set_span_in_context(parent) if parent is not None else None
    s = trace.get_tracer(TRACER_NAME).start_span(name, context=ctx)
    set_attrs(s, attrs or {})
    return s


def mark_error(s, message: str) -> None:
    if HAVE_OTEL and s.is_recording():
        s.set_status(Status(StatusCode.ERROR, message))


# Attribute helpers, so names are spelled one way everywhere.
def agent_attrs(agent: str, model: str | None, task: str) -> dict:
    return {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent,
            "gen_ai.request.model": model, "openinference.span.kind": "AGENT", "input.value": task}


def usage_attrs(input_tokens: int, output_tokens: int, cost_usd: float | None = None,
                cache_read: int = 0, cache_write: int = 0) -> dict:
    return {"gen_ai.usage.input_tokens": input_tokens, "gen_ai.usage.output_tokens": output_tokens,
            "llm.token_count.prompt": input_tokens, "llm.token_count.completion": output_tokens,
            "llm.token_count.total": input_tokens + output_tokens,
            "gen_ai.usage.cache_read_input_tokens": cache_read or None,
            "gen_ai.usage.cache_creation_input_tokens": cache_write or None,
            "gen_ai.usage.cost": cost_usd, "ffelab.cost_usd": cost_usd}


def chat_attrs(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0,
               cache_write: int = 0, text: str = "", tool_calls: list[str] | None = None) -> dict:
    return {"gen_ai.operation.name": "chat", "gen_ai.request.model": model, "gen_ai.response.model": model,
            "llm.model_name": model, "openinference.span.kind": "LLM", "output.value": text or None,
            "ffelab.tool_calls": tool_calls or None,
            **usage_attrs(input_tokens, output_tokens, None, cache_read, cache_write)}


def tool_attrs(name: str, args: dict) -> dict:
    return {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "tool.name": name,
            "openinference.span.kind": "TOOL", "input.value": args}
