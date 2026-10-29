import structlog
from fastapi import APIRouter, Request
from pydantic import ValidationError

from app.api import API_V1
from app.api.deps import ResourcesDep, SessionDep
from app.core import signatures
from app.domain import booking
from app.errors import Unauthorized, UnprocessableEntity
from app.observability.metrics import WEBHOOKS
from app.schemas import PaymentWebhook

router = APIRouter(prefix=f"{API_V1}/webhooks", tags=["webhooks"])
log = structlog.get_logger(__name__)

SIGNATURE_HEADER = "X-PayMock-Signature"


@router.post("/payments")
async def payment_webhook(request: Request, session: SessionDep, resources: ResourcesDep) -> dict[str, str]:
    """Called by the payment gateway. Authenticated by an HMAC signature over the raw body, not a JWT."""
    raw = await request.body()
    if not signatures.verify(
        raw, request.headers.get(SIGNATURE_HEADER), resources.settings.payment_webhook_secret
    ):
        WEBHOOKS.labels("invalid_signature").inc()
        raise Unauthorized("Invalid webhook signature", code="invalid_signature")
    try:
        event = PaymentWebhook.model_validate_json(raw)
    except ValidationError as exc:
        raise UnprocessableEntity("Malformed webhook payload") from exc

    outcome = await booking.handle_payment_event(session, event)
    await session.commit()
    WEBHOOKS.labels(outcome).inc()
    log.info("payment_webhook_processed", webhook_id=event.id, type=event.type, outcome=outcome)
    # 2xx for every outcome we decided on: a non-2xx would make the gateway retry forever.
    return {"outcome": outcome}
