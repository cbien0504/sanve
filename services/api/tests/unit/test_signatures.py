import pytest

from app.core.signatures import sign, verify

# Shared with services/payment-gateway/gateway_test.go: the Go signer and this Python verifier must
# produce the same bytes, otherwise every real webhook would be rejected.
VECTOR_SECRET = "whsec_test_vector"
VECTOR_TIMESTAMP = 1700000000
VECTOR_BODY = b'{"id":"evt_test","type":"payment.succeeded"}'
VECTOR_SIGNATURE = "t=1700000000,v1=a8b1d1360fbc7664c5d95b259b4f3657daa0012dd7c8fbd903ac9fa192486916"


def test_matches_the_go_gateway_vector() -> None:
    assert sign(VECTOR_BODY, VECTOR_SECRET, VECTOR_TIMESTAMP) == VECTOR_SIGNATURE


def test_accepts_a_fresh_valid_signature() -> None:
    assert verify(VECTOR_BODY, VECTOR_SIGNATURE, VECTOR_SECRET, now=VECTOR_TIMESTAMP + 10)


@pytest.mark.parametrize(
    ("body", "header", "secret", "now"),
    [
        (b'{"id":"evt_test","type":"payment.failed"}', VECTOR_SIGNATURE, VECTOR_SECRET, VECTOR_TIMESTAMP),
        (VECTOR_BODY, VECTOR_SIGNATURE, "another-secret", VECTOR_TIMESTAMP),
        (VECTOR_BODY, VECTOR_SIGNATURE, VECTOR_SECRET, VECTOR_TIMESTAMP + 301),
        (VECTOR_BODY, "garbage", VECTOR_SECRET, VECTOR_TIMESTAMP),
        (VECTOR_BODY, "t=abc,v1=00", VECTOR_SECRET, VECTOR_TIMESTAMP),
        (VECTOR_BODY, None, VECTOR_SECRET, VECTOR_TIMESTAMP),
    ],
    ids=["tampered-body", "wrong-secret", "replayed-too-late", "garbage", "bad-timestamp", "missing"],
)
def test_rejects_invalid_signatures(body: bytes, header: str | None, secret: str, now: int) -> None:
    assert not verify(body, header, secret, now=now)
