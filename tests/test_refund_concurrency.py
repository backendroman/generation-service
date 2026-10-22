from tests.helpers import (
    balance,
    deliver,
    new_user,
    provider_record,
    script,
    start_task,
    task_ledger,
    wait_for_task,
    webhook_outcomes,
)


async def test_parallel_failed(api, alpha_ctl, beta_ctl, db, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"outcome": "failed", "error_type": "technical",
                                      "delay": 0.2})
    await script(beta_ctl, marker, {"hold": True})

    task_id = await start_task(api, user_id, f"refund race {marker}")
    rec = await provider_record(beta_ctl, marker)

    responses = await deliver(beta_ctl, rec["external_id"], status="failed",
                              error_type="technical", copies=10, concurrent=True)

    assert [r["status_code"] for r in responses] == [200] * 10
    outcomes = sorted(r["body"]["outcome"] for r in responses)
    assert outcomes == ["applied"] + ["stale"] * 9

    task = await wait_for_task(api, task_id)
    assert task["status"] == "failed"
    assert task["failure_reason"] == "no_legs_left"
    assert task["refunded"] is True
    assert task["result_url"] is None
    assert [a["provider"] for a in task["attempts"]] == ["alpha", "beta"]

    ledger = [(r["kind"], r["delta"]) for r in await task_ledger(db, task_id)]
    assert ledger == [("charge", -2), ("refund", 2)]
    assert await balance(api, user_id) == 10
    assert sorted(await webhook_outcomes(db, rec["attempt_id"])) == ["applied"] + ["stale"] * 9
