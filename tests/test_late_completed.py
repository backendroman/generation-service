import pytest

from tests.helpers import (
    balance,
    deliver,
    get_task,
    new_user,
    post_signed,
    provider_record,
    script,
    start_task,
    task_ledger,
    wait_for_task,
    watchdog_wait,
)


def _second_leg_started(task: dict) -> bool:
    return len(task["attempts"]) == 2 and task["attempts"][0]["status"] == "timed_out"


@pytest.mark.slow
async def test_late_task_alive(api, alpha_ctl, beta_ctl, db, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"hold": True})
    await script(beta_ctl, marker, {"hold": True})

    task_id = await start_task(api, user_id, f"late alive {marker}")
    alpha_rec = await provider_record(alpha_ctl, marker)
    task = await wait_for_task(api, task_id, timeout=watchdog_wait(1),
                               predicate=_second_leg_started)
    alpha_attempt = task["attempts"][0]
    assert (alpha_attempt["error_type"], alpha_attempt["error_source"]) == (
        "timeout", "watchdog")
    beta_rec = await provider_record(beta_ctl, marker)

    [resp] = await deliver(alpha_ctl, alpha_rec["external_id"], status="completed")
    assert resp["status_code"] == 200
    assert resp["body"] == {"outcome": "late_accepted"}

    task = await get_task(api, task_id)
    assert task["status"] == "completed"
    assert task["result_url"].startswith("https://files.fake/alpha/")
    assert task["refunded"] is False
    assert task["current_leg"]["provider"] == "alpha"
    alpha_attempt, beta_attempt = task["attempts"]
    assert alpha_attempt["status"] == "completed"
    assert (alpha_attempt["error_type"], alpha_attempt["error_source"]) == (
        "timeout", "watchdog")
    assert beta_attempt["status"] == "abandoned"
    assert beta_attempt["finished_at"] is not None

    # beta уже abandoned
    [resp] = await deliver(beta_ctl, beta_rec["external_id"], status="completed")
    assert resp["body"] == {"outcome": "late_ignored"}
    [resp] = await deliver(beta_ctl, beta_rec["external_id"], status="failed",
                           error_type="technical")
    assert resp["body"] == {"outcome": "stale"}

    task = await get_task(api, task_id)
    assert task["status"] == "completed"
    assert task["result_url"].startswith("https://files.fake/alpha/")
    assert [r["kind"] for r in await task_ledger(db, task_id)] == ["charge"]
    assert await balance(api, user_id) == 8


@pytest.mark.slow
async def test_late_after_refund(api, alpha_ctl, beta_ctl, db, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"hold": True})
    await script(beta_ctl, marker, {"hold": True})

    task_id = await start_task(api, user_id, f"late dead {marker}")
    alpha_rec = await provider_record(alpha_ctl, marker)
    task = await wait_for_task(api, task_id, timeout=watchdog_wait(2))

    assert task["status"] == "failed"
    assert task["failure_reason"] == "no_legs_left"
    assert task["refunded"] is True
    assert [(a["provider"], a["status"], a["error_type"], a["error_source"])
            for a in task["attempts"]] == [
        ("alpha", "timed_out", "timeout", "watchdog"),
        ("beta", "timed_out", "timeout", "watchdog"),
    ]
    ledger_before = [(r["kind"], r["delta"]) for r in await task_ledger(db, task_id)]
    assert ledger_before == [("charge", -2), ("refund", 2)]
    assert await balance(api, user_id) == 10

    [resp] = await deliver(alpha_ctl, alpha_rec["external_id"], status="completed")
    assert resp["status_code"] == 200
    assert resp["body"] == {"outcome": "late_ignored"}

    task = await get_task(api, task_id)
    assert task["status"] == "failed"
    assert task["result_url"] is None
    assert task["attempts"][0]["late_result_url"].startswith("https://files.fake/alpha/")
    assert task["attempts"][0]["status"] == "timed_out"
    assert [(r["kind"], r["delta"]) for r in await task_ledger(db, task_id)] == ledger_before
    assert await balance(api, user_id) == 10


async def test_late_after_sync_timeout(api, alpha_ctl, beta_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"sync": "timeout", "timeout_then_process": True,
                                      "outcome": "completed", "delay": 9})
    await script(beta_ctl, marker, {"hold": True})

    task_id = await start_task(api, user_id, f"sync timeout late {marker}")
    task = await wait_for_task(api, task_id, timeout=30)

    assert task["status"] == "completed"
    alpha_attempt, beta_attempt = task["attempts"]
    assert alpha_attempt["status"] == "completed"
    assert (alpha_attempt["error_source"], alpha_attempt["error_detail"]) == (
        "sync", "timeout")
    assert beta_attempt["status"] == "abandoned"
    assert task["result_url"].startswith("https://files.fake/alpha/")
    assert await balance(api, user_id) == 8


async def test_late_after_503(api, alpha_ctl, beta_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"sync": "503"})
    await script(beta_ctl, marker, {"hold": True})

    task_id = await start_task(api, user_id, f"503 late {marker}")
    task = await wait_for_task(api, task_id, predicate=lambda t: len(t["attempts"]) == 2)
    alpha_attempt = task["attempts"][0]
    assert alpha_attempt["error_detail"] == "http_503"

    r = await post_signed(api, "/webhooks/providers/alpha", {
        "attempt_id": alpha_attempt["attempt_id"], "external_id": "alpha-ghost",
        "status": "completed", "result_url": "https://files.fake/alpha/ghost.png",
    })
    assert r.status_code == 200 and r.json() == {"outcome": "late_ignored"}

    task = await get_task(api, task_id)
    assert task["status"] == "running"
    assert task["attempts"][0]["status"] == "failed"
    assert task["attempts"][0]["late_result_url"] == "https://files.fake/alpha/ghost.png"
    assert task["current_leg"]["provider"] == "beta"


async def test_late_after_failed(api, alpha_ctl, beta_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"hold": True})
    await script(beta_ctl, marker, {"hold": True})

    task_id = await start_task(api, user_id, f"failed then completed {marker}")
    alpha_rec = await provider_record(alpha_ctl, marker)
    [resp] = await deliver(alpha_ctl, alpha_rec["external_id"], status="failed",
                           error_type="technical")
    assert resp["body"] == {"outcome": "applied"}
    await provider_record(beta_ctl, marker)

    [resp] = await deliver(alpha_ctl, alpha_rec["external_id"], status="completed")
    assert resp["body"] == {"outcome": "late_ignored"}

    task = await get_task(api, task_id)
    assert task["status"] == "running"
    assert task["result_url"] is None
    assert task["attempts"][0]["status"] == "failed"
    assert task["attempts"][0]["late_result_url"] is not None


@pytest.mark.slow
async def test_no_webhook(api, alpha_ctl, beta_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"hold": True})
    await script(beta_ctl, marker, {"outcome": "completed", "delay": 0.3})

    task_id = await start_task(api, user_id, f"drop {marker}")
    task = await wait_for_task(api, task_id, timeout=watchdog_wait(1))

    assert task["status"] == "completed"
    assert [(a["provider"], a["status"], a["error_type"], a["error_source"])
            for a in task["attempts"]] == [
        ("alpha", "timed_out", "timeout", "watchdog"),
        ("beta", "completed", None, None),
    ]
    assert await balance(api, user_id) == 8
