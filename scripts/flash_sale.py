# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "aiohttp>=3.10"]
# ///
"""Giả lập flash sale: hàng trăm người mua cùng lúc tranh số ghế ít hơn.

    make flash-sale BUYERS=500 SEATS=100
(tạo tài khoản + token bằng `python -m app.cli seed-buyers`, rồi chạy script này)
"""

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from pathlib import Path

import aiohttp
import httpx
from sanve_client import API_URL, admin_token, bearer, create_published_event, new_key


async def attempt(
    session: aiohttp.ClientSession, token: str, event: dict, quantity: int, gate: asyncio.Semaphore
) -> tuple[int, str, float, list[str]]:
    async with gate:
        started = time.perf_counter()
        async with session.post(
            f"{API_URL}/api/v1/holds",
            json={"event_id": event["id"], "tier_id": event["tiers"][0]["id"], "quantity": quantity},
            headers={**bearer(token), "Idempotency-Key": new_key()},
        ) as response:
            body = await response.json(content_type=None)
            status = response.status
        elapsed = time.perf_counter() - started
    code = "created" if status == 201 else body.get("code", "?")
    return status, code, elapsed, body.get("seats", [])


async def run(tokens: list[str], seats: int, quantity: int, concurrency: int) -> None:
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        token = admin_token(client)
        # One row up to 500 seats, otherwise rows A..Z of 100 seats (max 2,600).
        rows, per_row = (
            (["A"], seats) if seats <= 500 else ([chr(65 + i) for i in range(-(-seats // 100))], 100)
        )
        event = create_published_event(
            client,
            token,
            f"Flash sale {int(time.time())}",
            [{"name": "GA", "price": 1_000_000, "rows": rows, "seats_per_row": per_row}],
        )
    capacity = event["tiers"][0]["capacity"]
    print(f"Sự kiện {event['id']}: {capacity} ghế, {len(tokens)} người mua, mỗi người {quantity} vé")

    # aiohttp for the load itself: a load generator must spend less CPU per request than the server,
    # otherwise it measures its own event loop (httpx costs several ms of CPU per request).
    gate = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=30)) as session:
        started = time.perf_counter()
        results = await asyncio.gather(*(attempt(session, t, event, quantity, gate) for t in tokens))
        wall = time.perf_counter() - started

    outcomes = Counter((status, code) for status, code, _, _ in results)
    latencies = sorted(elapsed for _, _, elapsed, _ in results)
    sold = [seat for *_, seat_list in results for seat in seat_list]
    print("\nKết quả:")
    for (status, code), count in sorted(outcomes.items()):
        print(f"  {status} {code:<24} {count}")
    print(f"\nThời gian: {wall:.2f}s — {len(results) / wall:.0f} req/s")
    p = statistics.quantiles(latencies, n=100)
    print(f"Latency: p50 {p[49] * 1000:.0f} ms, p95 {p[94] * 1000:.0f} ms, p99 {p[98] * 1000:.0f} ms")

    with httpx.Client(base_url=API_URL, timeout=10) as client:
        stats = client.get(f"/api/v1/events/{event['id']}/stats", headers=bearer(admin_token(client))).json()
    ok = len(sold) == len(set(sold)) == stats["seats_held"] <= capacity
    print(
        f"\nGhế đã giữ: {len(sold)} (không trùng: {len(set(sold))}), DB: held={stats['seats_held']}, "
        f"available={stats['seats_available']}"
    )
    print("✅ Không bán vượt, không trùng ghế" if ok else "❌ Phát hiện bán trùng hoặc vượt số ghế!")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tokens", type=Path, required=True, help="output of `app.cli seed-buyers` (JSON lines)"
    )
    parser.add_argument("--seats", type=int, default=100)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=200)
    args = parser.parse_args()
    if args.seats > 2600:
        parser.error("--seats must be <= 2600")
    tokens = [json.loads(line)["token"] for line in args.tokens.read_text().splitlines() if line.strip()]
    asyncio.run(run(tokens, args.seats, args.quantity, args.concurrency))


if __name__ == "__main__":
    main()
