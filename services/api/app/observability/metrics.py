from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests handled", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

HOLDS = Counter("sanve_holds_total", "Seat hold attempts", ["outcome"])
ORDER_TRANSITIONS = Counter("sanve_order_transitions_total", "Order state transitions", ["to_status"])
PAYMENT_CALLS = Counter("sanve_payment_gateway_calls_total", "Payment gateway calls", ["outcome"])
WEBHOOKS = Counter("sanve_payment_webhooks_total", "Payment webhooks received", ["outcome"])
RATE_LIMITED = Counter("sanve_rate_limited_total", "Requests rejected by rate limiting", ["limit"])
CACHE_REQUESTS = Counter("sanve_cache_requests_total", "Cache lookups", ["cache", "result"])

OUTBOX_PUBLISHED = Counter("sanve_outbox_published_total", "Outbox messages published to RabbitMQ")
OUTBOX_LAG = Gauge("sanve_outbox_lag_seconds", "Age of the oldest unpublished outbox message")
MESSAGES = Counter("sanve_messages_processed_total", "Queue messages processed", ["queue", "outcome"])
JANITOR_ITEMS = Counter("sanve_janitor_items_total", "Rows cleaned up by periodic jobs", ["job"])
