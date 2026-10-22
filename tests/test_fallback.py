from datetime import datetime

from tests.helpers import (
    balance,
    new_user,
    script,
    start_task,
    task_ledger,
    wait_for_task,
)


async def test_fallback_to_second_leg(api, alpha_ctl, beta_ctl, db, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"outcome": "failed", "error_type": "technical",
                                      "delay": 0.2})
    await script(beta_ctl, marker, {"outcome": "completed", "delay": 0.2})

    task_id = await start_task(api, user_id, f"fallback {marker}")
    task = await wait_for_task(api, task_id)

    assert task["status"] == "completed"
    assert task["result_url"].startswith("https://files.fake/beta/")
    assert task["current_leg"]["provider"] == "beta"
    assert task["refunded"] is False

    first, second = task["attempts"]
    assert (first["provider"], first["priority"], first["status"]) == ("alpha", 1, "failed")
    assert (first["error_type"], first["error_source"]) == ("technical", "webhook")
    assert (second["provider"], second["priority"], second["status"]) == (
        "beta", 2, "completed")
    assert second["error_type"] is None
    for a in task["attempts"]:
        assert a["external_id"] and a["created_at"] and a["sent_at"] and a["finished_at"]
    assert datetime.fromisoformat(first["finished_at"]) <= datetime.fromisoformat(
        second["created_at"])

    assert await balance(api, user_id) == 8
    assert [r["kind"] for r in await task_ledger(db, task_id)] == ["charge"]


async def test_payload_no_fallback(api, alpha_ctl, beta_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"outcome": "failed", "error_type": "payload",
                                      "delay": 0.2})
    task_id = await start_task(api, user_id, f"payload {marker}")
    task = await wait_for_task(api, task_id)

    assert task["status"] == "failed"
    assert task["failure_reason"] == "payload"
    assert task["refunded"] is True
    assert [a["provider"] for a in task["attempts"]] == ["alpha"]
    assert await balance(api, user_id) == 10
    beta_tasks = (await beta_ctl.get("/_control/tasks", params={"match": marker})).json()
    assert beta_tasks["tasks"] == []


async def test_moderation_skip(api, alpha_ctl, beta_ctl, marker):
    # у alpha в video-long стоит skip_on_moderation
    user_id = await new_user(api, 30)
    await script(beta_ctl, marker, {"outcome": "failed", "error_type": "moderation",
                                     "delay": 0.2})
    task_id = await start_task(api, user_id, f"moderation {marker}", model="video-long")
    task = await wait_for_task(api, task_id)

    assert task["status"] == "failed"
    assert task["failure_reason"] == "no_legs_left"
    assert task["refunded"] is True
    assert [(a["provider"], a["error_type"]) for a in task["attempts"]] == [
        ("beta", "moderation")]
    assert await balance(api, user_id) == 30
    alpha_tasks = (await alpha_ctl.get("/_control/tasks", params={"match": marker})).json()
    assert alpha_tasks["tasks"] == []


async def test_moderation_no_flag(api, alpha_ctl, beta_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"outcome": "failed", "error_type": "moderation",
                                      "delay": 0.2})
    await script(beta_ctl, marker, {"outcome": "completed", "delay": 0.2})
    task_id = await start_task(api, user_id, f"moderation-next {marker}")
    task = await wait_for_task(api, task_id)

    assert task["status"] == "completed"
    assert [(a["provider"], a["status"]) for a in task["attempts"]] == [
        ("alpha", "failed"), ("beta", "completed")]
    assert await balance(api, user_id) == 8


async def test_sync_503(api, alpha_ctl, beta_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"sync": "503"})
    await script(beta_ctl, marker, {"outcome": "completed", "delay": 0.2})
    task_id = await start_task(api, user_id, f"sync503 {marker}")
    task = await wait_for_task(api, task_id)

    assert task["status"] == "completed"
    first, second = task["attempts"]
    assert (first["status"], first["error_type"], first["error_source"],
            first["error_detail"]) == ("failed", "technical", "sync", "http_503")
    assert first["external_id"] is None
    assert second["provider"] == "beta"


async def test_sync_timeout(api, alpha_ctl, beta_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"sync": "timeout"})
    await script(beta_ctl, marker, {"outcome": "completed", "delay": 0.2})
    task_id = await start_task(api, user_id, f"sync-timeout {marker}")
    task = await wait_for_task(api, task_id, timeout=30)

    assert task["status"] == "completed"
    first, second = task["attempts"]
    assert (first["error_type"], first["error_source"], first["error_detail"]) == (
        "technical", "sync", "timeout")
    assert second["provider"] == "beta"


async def test_all_legs_503(api, alpha_ctl, beta_ctl, marker):
    user_id = await new_user(api, 10)
    await script(alpha_ctl, marker, {"sync": "503"})
    await script(beta_ctl, marker, {"sync": "503"})
    task_id = await start_task(api, user_id, f"all-503 {marker}")
    task = await wait_for_task(api, task_id)

    assert task["status"] == "failed"
    assert task["refunded"] is True
    assert [a["error_source"] for a in task["attempts"]] == ["sync", "sync"]
    assert await balance(api, user_id) == 10


async def test_refs_routing(api, alpha_ctl, beta_ctl, marker):
    # alpha max 4 refs
    user_id = await new_user(api, 10)
    await script(beta_ctl, marker, {"outcome": "completed", "delay": 0.2})
    task_id = await start_task(api, user_id, f"refs {marker}",
                               refs=[f"https://img/{i}.png" for i in range(6)])
    task = await wait_for_task(api, task_id)

    assert task["status"] == "completed"
    assert [a["provider"] for a in task["attempts"]] == ["beta"]
    alpha_tasks = (await alpha_ctl.get("/_control/tasks", params={"match": marker})).json()
    assert alpha_tasks["tasks"] == []
