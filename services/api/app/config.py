from functools import lru_cache
from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_WEAK_SECRET_MARKERS = ("change-me", "dev-", "test-")


class Settings(BaseSettings):
    """Process configuration, read from environment variables (12-factor)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["local", "test", "production"] = "local"
    service_name: str = "sanve-api"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+asyncpg://sanve:sanve@localhost:55432/sanve"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_statement_timeout_ms: int = 5000
    db_lock_timeout_ms: int = 3000

    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"

    jwt_secret: str
    jwt_issuer: str = "sanve"
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 14 * 24 * 3600

    payment_base_url: str = "http://localhost:8090"
    payment_api_key: str
    payment_webhook_secret: str
    payment_timeout_seconds: float = 2.0
    payment_max_attempts: int = 3
    payment_backoff_base_seconds: float = 0.2
    payment_max_concurrency: int = 50

    hold_ttl_seconds: int = 600
    order_ttl_seconds: int = 900

    smtp_host: str = "localhost"
    smtp_port: int = 1025
    mail_from: str = "SanVe <no-reply@sanve.local>"

    # Prometheus metrics live on their own internal port, never behind the public load balancer.
    metrics_port: int | None = 9100
    otel_exporter_otlp_endpoint: str | None = None
    sentry_dsn: str | None = None
    cors_origins: list[str] = []

    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

    @model_validator(mode="after")
    def _reject_weak_secrets_in_production(self) -> Self:
        if self.env != "production":
            return self
        for name in ("jwt_secret", "payment_api_key", "payment_webhook_secret"):
            value: str = getattr(self, name)
            if len(value) < 32 or any(marker in value for marker in _WEAK_SECRET_MARKERS):
                raise ValueError(f"{name.upper()} is too weak for production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment
