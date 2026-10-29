"""Keyset (cursor) pagination.

OFFSET pagination gets slower with every page and skips/duplicates rows when data changes between
requests. A cursor encodes the sort key of the last row instead: WHERE (sort_key, id) > (:last, :id).
"""

import base64
import json
import uuid
from datetime import datetime

from app.errors import UnprocessableEntity


def encode_cursor(sort_value: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps([sort_value.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        sort_value, row_id = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(sort_value), uuid.UUID(row_id)
    except (ValueError, TypeError) as exc:
        raise UnprocessableEntity("Invalid pagination cursor", code="invalid_cursor") from exc
