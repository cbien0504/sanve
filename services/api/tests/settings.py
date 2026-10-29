import os
from typing import Any

from app.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://sanve:sanve-local-password@localhost:55432/sanve_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:56379/15")
WEBHOOK_SECRET = "whsec_test"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "env": "test",
        "log_json": False,
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "db_pool_size": 20,
        "db_max_overflow": 20,
        "jwt_secret": "test-jwt-secret-0123456789abcdef0123456789",
        "payment_api_key": "test-payment-key",
        "payment_webhook_secret": WEBHOOK_SECRET,
        "payment_base_url": "http://payment.test",
        "payment_backoff_base_seconds": 0.001,
        "metrics_port": None,
    }
    values.update(overrides)
    return Settings(**values)
