import sentry_sdk
from fastapi import FastAPI
from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings

_configured = False


def configure_tracing(settings: Settings) -> None:
    """Export spans over OTLP when an endpoint is configured; otherwise tracing stays a no-op."""
    global _configured
    if _configured or not settings.otel_exporter_otlp_endpoint:
        return
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": settings.service_name, "deployment.environment": settings.env}
        )
    )
    endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    _configured = True


def instrument_app(app: FastAPI) -> None:
    if _configured:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")


def instrument_engine(engine: AsyncEngine) -> None:
    if _configured:
        # The instrumentor's version pin stops at SQLAlchemy 2.0, but the engine events it hooks
        # into are unchanged in 2.1 (spans verified in Jaeger), so skip the dependency check.
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, skip_dep_check=True)


def configure_sentry(settings: Settings) -> None:
    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.env,
            send_default_pii=False,
            traces_sample_rate=0.0,  # tracing goes to OpenTelemetry; Sentry is for errors
        )


def current_trace_carrier() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def context_from_carrier(carrier: dict[str, object]) -> Context:
    return propagate.extract({k: str(v) for k, v in carrier.items()})
