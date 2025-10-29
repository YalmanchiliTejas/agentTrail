"""
agenttrail/otel.py

Optional OpenTelemetry wiring. If OTEL isn't installed, all APIs are safe no-ops.
- init_tracing() sets a basic provider with console export (human-friendly for demos).
- start_span() returns a context manager used in runtime._execute_step().
"""

from typing import Optional

def init_tracing(service_name: str) -> Optional[object]:
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)
        return trace.get_tracer(service_name)
    except Exception:
        # OpenTelemetry not installed or failed to init: silently degrade
        return None

def start_span(tracer, name: str):
    if tracer is None:
        # Minimal context manager that does nothing.
        class Dummy:
            def __enter__(self): return None
            def __exit__(self, exc_type, exc, tb): return False
        return Dummy()
    return tracer.start_as_current_span(name)
