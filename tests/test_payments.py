import asyncio
import uuid

from tests.helpers import balance, new_user, pay, post_signed


async def test_parallel_same_payment(api, db):
    user_id = str(uuid.uuid4())
    payment_id = f"pay-{uuid.uuid4()}"
    payload = {"payment_id": payment_id, "user_id": user_id, "tokens": 50,
               "status": "succeeded"}

    responses = await asyncio.gather(
        *(post_signed(api, "/webhooks/payments", payload) for _ in range(20))
    )

    assert [r.status_code for r in responses] == [200] * 20
    assert sum(r.json()["credited"] for r in responses) == 1
    rows = await db.fetch(
        "SELECT delta FROM ledger WHERE idempotency_key = $1", f"payment:{payment_id}"
    )
    assert [r["delta"] for r in rows] == [50]
    assert await balance(api, user_id) == 50


async def test_pending_then_succeeded(api, db):
    user_id = str(uuid.uuid4())
    payment_id = f"pay-{uuid.uuid4()}"

    r = await pay(api, user_id, 30, "pending", payment_id)
    assert r.json() == {"credited": False}
    assert await balance(api, user_id) == 0

    r = await pay(api, user_id, 30, "succeeded", payment_id)
    assert r.json() == {"credited": True}
    r = await pay(api, user_id, 30, "succeeded", payment_id)
    assert r.json() == {"credited": False}
    # поздний pending
    r = await pay(api, user_id, 30, "pending", payment_id)
    assert r.json() == {"credited": False}

    assert await balance(api, user_id) == 30
    status = await db.fetchval("SELECT status FROM payments WHERE payment_id = $1", payment_id)
    assert status == "succeeded"


async def test_failed_payment(api):
    user_id = str(uuid.uuid4())
    r = await pay(api, user_id, 30, "failed")
    assert r.status_code == 200 and r.json() == {"credited": False}
    assert await balance(api, user_id) == 0


async def test_conflict(api, db):
    user_id = await new_user(api, 1)
    payment_id = f"pay-{uuid.uuid4()}"
    assert (await pay(api, user_id, 10, "succeeded", payment_id)).status_code == 200

    r = await pay(api, user_id, 99, "succeeded", payment_id)
    assert r.status_code == 409
    assert r.json()["error"] == "payment_conflict"

    other = str(uuid.uuid4())
    r = await pay(api, other, 10, "succeeded", payment_id)
    assert r.status_code == 409
    # пользователь из конфликта не создался
    assert await db.fetchval("SELECT count(*) FROM users WHERE id = $1", uuid.UUID(other)) == 0
    assert await balance(api, user_id) == 11


async def test_new_user(api):
    user_id = str(uuid.uuid4())
    assert (await api.get(f"/users/{user_id}/balance")).status_code == 404
    await pay(api, user_id, 7)
    assert await balance(api, user_id) == 7


async def test_validation(api):
    user_id = str(uuid.uuid4())
    for bad in (
        {"payment_id": "p", "user_id": user_id, "tokens": 0, "status": "succeeded"},
        {"payment_id": "p", "user_id": "not-a-uuid", "tokens": 1, "status": "succeeded"},
        {"payment_id": "", "user_id": user_id, "tokens": 1, "status": "succeeded"},
        {"user_id": user_id, "tokens": 1, "status": "succeeded"},
    ):
        r = await post_signed(api, "/webhooks/payments", bad)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"] == "validation_error"
