"""TracingProvider — OpenTelemetry tracing as a component.

Provides: ITracer
Requires: IConfig

This demonstrates the BRIDGE pattern: wrapping an external system
(OpenTelemetry + Arize Phoenix) as a kernel component without
forcing the external system to know about the kernel.

The bridge has three responsibilities:
  1. LIFECYCLE — start/stop the external system with the kernel
  2. SURFACE — expose the external system's API through the kernel contract
  3. INSTRUMENT — auto-instrument kernel internals (bus calls, lifecycle)
     using the external system's native mechanism

The external system's native API remains available for direct use
when the kernel contract isn't enough.  Components can always
escape-hatch to the raw OTel/Phoenix API.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle

log = logging.getLogger(__name__)


# ── Null implementation (zero-dep default) ──────────────────────

class _NullSpan:
    """No-op span for when tracing is disabled."""
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def set_attribute(self, key: str, value: Any) -> None: pass
    def add_event(self, name: str, attributes: dict | None = None) -> None: pass


class _NullTracer:
    """No-op tracer — ITracer contract satisfied with zero overhead."""
    def span(self, name: str, **attributes: Any):
        return _NullSpan()

    @property
    def native(self):
        """Escape hatch to the raw OTel tracer.  None when no backend."""
        return None


# ── OTel + Phoenix bridge ───────────────────────────────────────

class _OTelPhoenixTracer:
    """Bridge: wraps OTel TracerProvider + Phoenix collector.

    This class does NOT reimplement tracing.  It delegates entirely
    to the OTel SDK.  The kernel contract (ITracer.span()) is a thin
    wrapper; the native OTel tracer is always available via .native.
    """

    def __init__(self, tracer, provider):
        self._tracer = tracer       # opentelemetry.trace.Tracer
        self._provider = provider   # TracerProvider (for shutdown)

    def span(self, name: str, **attributes: Any):
        """Start a span.  Returns a context manager (OTel native span)."""
        otel_span = self._tracer.start_span(name, attributes=attributes or None)
        return _OTelSpanWrapper(otel_span)

    @property
    def native(self):
        """Escape hatch: the raw OTel Tracer for direct SDK use.

        Components that need OTel-specific features (links, events,
        span processors) can use this directly.  The kernel doesn't
        wrap every OTel feature — it wraps the common path and
        exposes the native API for everything else.
        """
        return self._tracer

    def shutdown(self):
        if self._provider:
            self._provider.shutdown()


class _OTelSpanWrapper:
    """Thin wrapper that makes an OTel span a context manager."""

    def __init__(self, otel_span):
        self._span = otel_span

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._span.set_status(
                # StatusCode.ERROR if available
                getattr(
                    __import__("opentelemetry.trace", fromlist=["StatusCode"]),
                    "StatusCode", type("S", (), {"ERROR": 2})
                ).ERROR,
                str(exc_val),
            )
            self._span.record_exception(exc_val)
        self._span.end()
        return False

    def set_attribute(self, key: str, value: Any) -> None:
        self._span.set_attribute(key, value)

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        self._span.add_event(name, attributes=attributes)


# ── The component ───────────────────────────────────────────────

@component("tracing", version="0.1")
@provides("ITracer")
@requires(config="IConfig")
class TracingProvider:
    """Tracing component — bridges OTel + Phoenix into the kernel.

    Config keys:
      tracing.enabled:    true/false (default: true)
      tracing.backend:    "phoenix" | "jaeger" | "console" | "none"
      tracing.endpoint:   collector URL (e.g. http://localhost:6006/v1/traces)
      tracing.service_name: OTel service name

    When backend is "none" or deps missing, falls back to _NullTracer
    (zero overhead, ITracer contract still satisfied).
    """

    @lifecycle.activate
    def activate(self, rt):
        enabled = rt.config.get("tracing.enabled", True)
        backend = rt.config.get("tracing.backend", "none")

        if not enabled or backend == "none":
            self._impl = _NullTracer()
            return

        # Try to set up OTel + backend
        try:
            self._impl = self._setup_otel(rt.config, backend)
        except ImportError as exc:
            log.warning("Tracing backend %s unavailable (%s), using null tracer", backend, exc)
            self._impl = _NullTracer()

    def _setup_otel(self, config, backend: str) -> _OTelPhoenixTracer:
        """Set up OTel TracerProvider with the specified backend."""
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.resources import Resource

        service_name = config.get("tracing.service_name", "microkernel")
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)

        # ── Backend-specific exporter ───────────────────────

        if backend == "phoenix":
            # Phoenix uses OTLP/HTTP exporter pointed at its collector
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            endpoint = config.get("tracing.endpoint", "http://localhost:6006/v1/traces")
            exporter = OTLPSpanExporter(endpoint=endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))

        elif backend == "jaeger":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            endpoint = config.get("tracing.endpoint", "http://localhost:4317")
            exporter = OTLPSpanExporter(endpoint=endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))

        elif backend == "console":
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)
        tracer = trace.get_tracer(service_name)

        return _OTelPhoenixTracer(tracer, provider)

    # ── ITracer contract ────────────────────────────────────────

    def span(self, name: str, **attributes: Any):
        """Start a trace span.  Returns a context manager."""
        return self._impl.span(name, **attributes)

    @property
    def native(self):
        """Escape hatch: the raw OTel Tracer (or None).

        Use this when the kernel's ITracer.span() isn't enough:
          tracer = rt.tracer.native
          if tracer:
              with tracer.start_as_current_span("custom") as span:
                  span.add_link(...)
                  span.set_status(...)
        """
        return self._impl.native

    @lifecycle.deactivate
    def deactivate(self, rt):
        if hasattr(self._impl, "shutdown"):
            self._impl.shutdown()
