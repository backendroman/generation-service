import asyncio
import random
import uuid

from tests.helpers import new_user, script, start_task, wait_for_task

SCENARIOS = [
    {"outcome": "completed", "delay": 0.2},
    {"outcome": "failed", "error_type": "technical", "delay": 0.2},
    {"outcome": "failed", "error_type": "moderation", "delay": 0.2},
    {"outcome": "failed", "error_type": "payload", "delay": 0.2},
    {"sync": "503"},
]


async def test_random_mix(api, alpha_ctl, beta_ctl, db, marker):
    rng = random.Random(7)
    users = [await new_user(api, 100) for _ in range(17)]
    task_ids = []
    for i in range(50):
        m = f"{marker}-{i}|"  # иначе -1 матчится на -10
        await script(alpha_ctl, m, rng.choice(SCENARIOS))
        await script(beta_ctl, m, rng.choice(SCENARIOS))
        task_ids.append(await start_task(api, users[i % len(users)], f"stress {m} "))
        if i % len(users) == len(users) - 1:
            await asyncio.sleep(1.5)  # лимит 3 активных

    tasks = await asyncio.gather(*(wait_for_task(api, t, timeout=30) for t in task_ids))

    for task in tasks:
        ledger = await db.fetch("SELECT kind FROM ledger WHERE task_id = $1",
                                uuid.UUID(task["id"]))
        kinds = sorted(r["kind"] for r in ledger)
        if task["status"] == "failed":
            assert kinds == ["charge", "refund"], task
            assert task["result_url"] is None
        else:
            assert kinds == ["charge"], task
            assert task["result_url"]
