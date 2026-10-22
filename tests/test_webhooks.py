import json
import uuid

from app.security import sign
from tests.helpers import (
    WEBHOOK_SECRET,
    deliver,
    get_task,
    new_user,
    post_signed,
    provider_record,
    script,
    start_task,
    task_ledger,
    wait_for_task,
    webhook_outcomes,
)


async def test_bad_signature(api):
    payment = {"payment_id": "p-sig", "user_id": str(uuid.uuid4()), "tokens": 5,
               "status": "succeeded"}
    hook = {"attempt_id": str(uuid.uuid4()), "external_id": "x", "status": "failed",
            "error_type": "technical"}
    for path, body in (("/webhooks/payments", payment), ("/webhooks/providers/alpha", hook)):
        r = await post_signed(api, path, body, signature="deadbeef")
        assert r.status_code == 401 and r.json()["error"] == "bad_signature"

        r = await api.post(path, json=body)
        assert r.status_code == 401

        # подменили тело
        raw = json.dumps(body).encode()
        tampered = json.dumps({**body, "tokens": 500, "status": "completed"}).encode()
        r = await api.post(path, content=tampered,
                           headers={"X-Signature": sign(WEBHOOK_SECRET, raw),
                                    "Content-Type": "application/json"})
        assert r.status_code == 401


async def test_signature_uppercase(api):
    user_id = str(uuid.uuid4())
    raw = json.dumps({"payment_id": f"p-{uuid.uuid4()}", "user_id": user_id, "tokens": 3,
                      "status": "succeeded"}).encode()
    r = await api.post("/webhooks/payments", content=raw,
                       headers={"X-Signature": sign(WEBHOOK_SECRET, raw).upper()})
    assert r.status_code == 200 and r.json() == {"credited": True}


async def test_unknown_404(api, db):
    hook = {"attempt_id": str(uuid.uuid4()), "external_id": "nope", "status": "completed",
            "result_url": "https://x"}
    r = await post_signed(api, "/webhooks/providers/gamma", hook)
    assert r.status_code == 404 and r.json()["error"] == "unknown_provider"
    r = await post_signed(api, "/webhooks/providers/alpha", hook)
    assert r.status_code == 404 and r.json()["error"] == "attempt_not_found"
    assert await webhook_outcomes(db, hook["attempt_id"]) == ["not_found"]


async def test_validation(api):
    base = {"attempt_id": str(uuid.uuid4()), "external_id": "x"}
    for body in (
        {**base, "status": "failed"},
        {**base, "status": "completed"},
        {**base, "status": "weird"},
        {**base, "status": "failed", "error_type": "cosmic"},
        {"status": "failed", "error_type": "technical"},
    ):
        r = await post_signed(api, "/webhooks/providers/alpha", body)
        assert r.status_code == 422, (body, r.text)


async def test_wrong_provider(api, alpha_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"hold": True})
    await start_task(api, user_id, f"wrong provider {marker}")
    rec = await provider_record(alpha_ctl, marker)
    r = await post_signed(api, "/webhooks/providers/beta", {
        "attempt_id": rec["attempt_id"], "external_id": rec["external_id"],
        "status": "completed", "result_url": "https://evil",
    })
    assert r.status_code == 404


async def test_by_external_id(api, alpha_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"hold": True})
    task_id = await start_task(api, user_id, f"by external {marker}")
    rec = await provider_record(alpha_ctl, marker)
    await wait_for_task(api, task_id, predicate=lambda t: t["attempts"][0]["external_id"])
    r = await post_signed(api, "/webhooks/providers/alpha", {
        "external_id": rec["external_id"], "status": "completed", "result_url": "https://ok",
    })
    assert r.status_code == 200 and r.json() == {"outcome": "applied"}
    task = await wait_for_task(api, task_id)
    assert task["result_url"] == "https://ok"


async def test_foreign_attempt_id(api, alpha_ctl, beta_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, f"{marker}-a", {"hold": True})
    await script(alpha_ctl, f"{marker}-b", {"outcome": "failed", "error_type": "technical",
                                            "delay": 0.2})
    await script(beta_ctl, f"{marker}-b", {"hold": True})
    alpha_task = await start_task(api, user_id, f"own {marker}-a")
    beta_task = await start_task(api, user_id, f"foreign {marker}-b")
    alpha_rec = await provider_record(alpha_ctl, f"{marker}-a")
    beta_rec = await provider_record(beta_ctl, f"{marker}-b")
    await wait_for_task(api, alpha_task, predicate=lambda t: t["attempts"][0]["external_id"])

    # attempt_id от beta, external_id от alpha
    r = await post_signed(api, "/webhooks/providers/alpha", {
        "attempt_id": beta_rec["attempt_id"], "external_id": alpha_rec["external_id"],
        "status": "completed", "result_url": "https://ok",
    })
    assert r.status_code == 200 and r.json() == {"outcome": "applied"}
    assert (await wait_for_task(api, alpha_task))["result_url"] == "https://ok"
    assert (await get_task(api, beta_task))["status"] == "running"


async def test_duplicate_completed(api, alpha_ctl, db, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"hold": True})
    task_id = await start_task(api, user_id, f"dup {marker}")
    rec = await provider_record(alpha_ctl, marker)

    responses = await deliver(alpha_ctl, rec["external_id"], status="completed", copies=5,
                              concurrent=True)
    assert sorted(r["body"]["outcome"] for r in responses) == ["applied"] + ["duplicate"] * 4
    task = await wait_for_task(api, task_id)
    assert [a["status"] for a in task["attempts"]] == ["completed"]
    assert [r["kind"] for r in await task_ledger(db, task_id)] == ["charge"]
