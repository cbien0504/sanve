# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""Chạy toàn bộ luồng nghiệp vụ trên stack đang chạy: `uv run scripts/demo.py`."""

import json
import time
import uuid
from collections.abc import Callable

import httpx
from sanve_client import API_URL, admin_token, bearer, create_published_event, login, new_key, post_with_retry

PASSWORD = "demo-password-123"


def step(message: str) -> None:
    print(f"\n\033[1;36m▶ {message}\033[0m")


def show(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def wait_for(description: str, check: Callable[[], dict | None], timeout: float = 20.0) -> tuple[dict, float]:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        result = check()
        if result:
            return result, time.perf_counter() - started
        time.sleep(0.2)
    raise SystemExit(f"Hết thời gian chờ: {description}")


def main() -> None:
    run = uuid.uuid4().hex[:6]
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        step("Đăng ký 2 tài khoản: ban tổ chức và khách hàng")
        organizer = client.post(
            "/api/v1/auth/register",
            json={"email": f"btc-{run}@example.com", "password": PASSWORD, "full_name": "Ban tổ chức Demo"},
        ).json()
        customer = client.post(
            "/api/v1/auth/register",
            json={"email": f"khach-{run}@example.com", "password": PASSWORD, "full_name": "Nguyễn Văn Khách"},
        ).json()
        print(f"organizer: {organizer['email']}\ncustomer:  {customer['email']}")

        step("Admin cấp quyền organizer (RBAC)")
        promoted = client.patch(
            f"/api/v1/admin/users/{organizer['id']}/role",
            json={"role": "organizer"},
            headers=bearer(admin_token(client)),
        )
        promoted.raise_for_status()
        print(f"{organizer['email']} → role = {promoted.json()['role']}")

        step("Ban tổ chức tạo sự kiện 2 hạng vé và mở bán")
        organizer_token = login(client, organizer["email"], PASSWORD)
        event = create_published_event(
            client,
            organizer_token,
            f"SanVe Live Concert {run}",
            [
                {"name": "VIP", "price": 2_500_000, "rows": ["A", "B"], "seats_per_row": 10},
                {"name": "GA", "price": 900_000, "rows": ["C", "D", "E", "F"], "seats_per_row": 20},
            ],
        )
        show({t["name"]: f"{t['capacity']} ghế × {t['price']:,}đ" for t in event["tiers"]})

        step("Khách giữ 2 ghế VIP (gửi kèm Idempotency-Key)")
        customer_token = login(client, customer["email"], PASSWORD)
        vip = next(t for t in event["tiers"] if t["name"] == "VIP")
        key = new_key()
        hold_request = {"event_id": event["id"], "tier_id": vip["id"], "quantity": 2}
        hold = client.post(
            "/api/v1/holds", json=hold_request, headers={**bearer(customer_token), "Idempotency-Key": key}
        ).json()
        show({"hold_id": hold["id"], "seats": hold["seats"], "expires_at": hold["expires_at"]})
        retry = client.post(
            "/api/v1/holds", json=hold_request, headers={**bearer(customer_token), "Idempotency-Key": key}
        )
        replayed = retry.headers.get("Idempotent-Replayed")
        print(f"Gửi lại cùng key → {retry.status_code}, Idempotent-Replayed: {replayed}")

        step("Tạo đơn hàng → nhận checkout_url từ cổng thanh toán (Go)")
        order = post_with_retry(
            client, "/api/v1/orders", json={"hold_id": hold["id"]}, headers=bearer(customer_token)
        ).json()
        show({k: order[k] for k in ("id", "status", "amount", "checkout_url")})

        step("Khách bấm 'Thanh toán' trên trang PayMock")
        paid = httpx.post(
            order["checkout_url"], data={"result": "succeeded"}, headers={"Accept": "application/json"}
        )
        print(f"PayMock: intent {paid.json()['id']} → {paid.json()['status']}; webhook đang được gửi...")

        step("Webhook (đã ký HMAC) chuyển đơn hàng sang 'paid'")

        def order_paid() -> dict | None:
            current = client.get(f"/api/v1/orders/{order['id']}", headers=bearer(customer_token)).json()
            return current if current["status"] == "paid" else None

        order, elapsed = wait_for("đơn hàng paid", order_paid)
        tickets = client.get(f"/api/v1/orders/{order['id']}/tickets", headers=bearer(customer_token)).json()
        print(f"Đơn hàng paid sau {elapsed:.1f}s")
        show([{"ghế": t["seat_label"], "mã vé": t["code"]} for t in tickets])

        step("Read model thống kê cập nhật bất đồng bộ (outbox → RabbitMQ → worker)")

        def projection_caught_up() -> dict | None:
            stats = client.get(f"/api/v1/events/{event['id']}/stats", headers=bearer(organizer_token)).json()
            return stats if stats["projection"]["tickets_sold"] == 2 else None

        stats, elapsed = wait_for("projection", projection_caught_up)
        show(stats)
        print(f"Projection bắt kịp sau {elapsed * 1000:.0f} ms (eventual consistency)")

    step("Xem tiếp")
    print("Email vé + QR:      http://localhost:8025")
    print("Grafana dashboard:  http://localhost:3000")
    print("Trace của request:  http://localhost:16686  (service sanve-api)")
    print("RabbitMQ:           http://localhost:15672")
    print("Swagger UI:         http://localhost:8080/docs")


if __name__ == "__main__":
    main()
