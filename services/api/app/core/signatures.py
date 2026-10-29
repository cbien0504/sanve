"""HMAC-SHA256 webhook signatures: header `t=<unix ts>,v1=<hex(hmac(secret, "<ts>.<body>"))>`.

The timestamp is part of the signed content, so a captured request cannot be replayed after
`tolerance` seconds. The Go payment gateway signs with exactly the same scheme.
"""

import hashlib
import hmac
import time


def sign(payload: bytes, secret: str, timestamp: int) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(
    payload: bytes, header: str | None, secret: str, *, tolerance: int = 300, now: float | None = None
) -> bool:
    if not header:
        return False
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        signature = parts["v1"]
    except (KeyError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance:
        return False
    expected = sign(payload, secret, timestamp).split("v1=", 1)[1]
    return hmac.compare_digest(expected, signature)
