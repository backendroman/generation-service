import asyncio
import uuid

from tests.helpers import balance, generate, new_user, script, start_task


async def test_402(api, db):
    user_id = await new_user(api, 1)
    r = await generate(api, user_id, "too poor")
    assert r.status_code == 402
    assert r.json()["error"] == "insufficient_tokens"
    assert await balance(api, user_id) == 1
    assert await db.fetchval("SELECT count(*) FROM tasks WHERE user_id = $1",
                             uuid.UUID(user_id)) == 0


async def test_404(api):
    r = await generate(api, str(uuid.uuid4()), "who")
    assert r.status_code == 404 and r.json()["error"] == "user_not_found"
    user_id = await new_user(api, 10)
    r = await generate(api, user_id, "what", model="no-such-model")
    assert r.status_code == 404 and r.json()["error"] == "model_not_found"


async def test_no_leg(api, db):
    user_id = await new_user(api, 10)
    r = await generate(api, user_id, "many refs", refs=[f"r{i}" for i in range(11)])
    assert r.status_code == 400
    assert r.json()["error"] == "no_suitable_leg"
    assert await balance(api, user_id) == 10
    assert await db.fetchval("SELECT count(*) FROM tasks WHERE user_id = $1",
                             uuid.UUID(user_id)) == 0


async def test_validation(api):
    user_id = await new_user(api, 10)
    for body in (
        {"user_id": user_id, "model_slug": "image-fast", "prompt": ""},
        {"user_id": user_id, "model_slug": "image-fast", "prompt": "   "},
        {"user_id": "nope", "model_slug": "image-fast", "prompt": "x"},
        {"user_id": user_id, "model_slug": "image-fast", "prompt": "x", "ref_images": "a"},
        {"model_slug": "image-fast", "prompt": "x"},
    ):
        r = await api.post("/generate", json=body)
        assert r.status_code == 422, (body, r.text)
        assert r.json()["error"] == "validation_error"


async def test_active_limit(api, alpha_ctl, marker):
    user_id = await new_user(api, 20)
    await script(alpha_ctl, marker, *({"hold": True} for _ in range(3)))
    for i in range(3):
        await start_task(api, user_id, f"active {i} {marker}")
    r = await generate(api, user_id, f"active 4 {marker}")
    assert r.status_code == 429
    assert r.json()["error"] == "too_many_active_tasks"
    assert await balance(api, user_id) == 14


async def test_active_limit_parallel(api, alpha_ctl, db, marker):
    user_id = await new_user(api, 12)
    await script(alpha_ctl, marker, *({"hold": True} for _ in range(6)))

    responses = await asyncio.gather(
        *(generate(api, user_id, f"limit {i} {marker}") for i in range(6))
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [202] * 3 + [429] * 3
    charges = await db.fetchval(
        "SELECT count(*) FROM ledger WHERE user_id = $1 AND kind = 'charge'", uuid.UUID(user_id)
    )
    assert charges == 3
    assert await balance(api, user_id) == 6


async def test_balance_race(api, alpha_ctl, db, marker):
    user_id = await new_user(api, 2)
    await script(alpha_ctl, marker, {"hold": True})

    responses = await asyncio.gather(
        *(generate(api, user_id, f"race {i} {marker}") for i in range(10))
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [202] + [402] * 9
    assert await balance(api, user_id) == 0
    charges = await db.fetchval(
        "SELECT count(*) FROM ledger WHERE user_id = $1 AND kind = 'charge'", uuid.UUID(user_id)
    )
    assert charges == 1


async def test_balance_ledger(api, alpha_ctl, marker):
    user_id = await new_user(api, 5)
    await script(alpha_ctl, marker, {"hold": True})
    task_id = await start_task(api, user_id, f"ledger {marker}")

    r = await api.get(f"/users/{user_id}/balance", params={"limit": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["balance"] == 3
    assert len(body["ledger"]) == 1
    assert body["ledger"][0]["kind"] == "charge"
    assert body["ledger"][0]["delta"] == -2
    assert body["ledger"][0]["task_id"] == task_id

    r = await api.get(f"/users/{user_id}/balance")
    assert [row["kind"] for row in r.json()["ledger"]] == ["charge", "payment"]
