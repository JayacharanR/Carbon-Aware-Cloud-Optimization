"""Small optional OpenTelemetry facade used by controller stages."""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any, Mapping, Protocol


class Telemetry(Protocol):
    def span(self, name: str, attributes: Mapping[str, Any] | None = None) -> AbstractContextManager[Any]: ...


class NoopTelemetry:
    """Keeps local/offline code runnable without observability dependencies."""

    def span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> AbstractContextManager[Any]:
        del name, attributes
        return nullcontext()


class OpenTelemetryTracer:
    """OTLP-backed stage tracer; construct only in a configured controller pod."""

    def __init__(self, service_name: str = "carbon-scheduler-controller") -> None:
        try:
            from opentelemetry import trace
        except ImportError as error:  # pragma: no cover - optional deployment dependency
            raise RuntimeError(
                "OpenTelemetry packages are required for OpenTelemetryTracer"
            ) from error
        self._trace = trace
        self._tracer = trace.get_tracer(service_name)

    def span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> AbstractContextManager[Any]:
        @contextmanager
        def managed_span() -> Any:
            with self._tracer.start_as_current_span(name) as span:
                if attributes:
                    for key, value in attributes.items():
                        if value is not None:
                            span.set_attribute(key, value)
                yield span

        return managed_span()


def configure_otlp(
    *,
    endpoint: str,
    service_name: str = "carbon-scheduler-controller",
    insecure: bool = True,
) -> OpenTelemetryTracer:
    """Configure a single OTLP exporter for the controller process.

    Configuration is deliberately explicit so local runs do not emit telemetry
    to an accidental endpoint.
    """

    if not endpoint.strip():
        raise ValueError("OTLP endpoint must not be empty")
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as error:  # pragma: no cover - optional deployment dependency
        raise RuntimeError("OpenTelemetry OTLP packages are required") from error
    provider = TracerProvider(Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure))
    )
    trace.set_tracer_provider(provider)
    return OpenTelemetryTracer(service_name)
