import asyncio
import json
import os
import time
import uuid
from typing import Any

import httpx

from app.security import sign

APP_URL = os.environ.get("APP_URL", "http://localhost:8000")
ALPHA_URL = os.environ.get("ALPHA_URL", "http://localhost:8001")
BETA_URL = os.environ.get("BETA_URL", "http://localhost:8002")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:5432/app")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret")
PROVIDER_TIMEOUT = float(os.environ.get("PROVIDER_TIMEOUT", "60"))

TERMINAL = ("completed", "failed")


def watchdog_wait(n_legs: int) -> float:
    return n_legs * PROVIDER_TIMEOUT + 15


async def post_signed(client: httpx.AsyncClient, path: str, payload: Any,
                      signature: str | None = None) -> httpx.Response:
    # подпись строго от тех байт, что уходят в теле
    raw = json.dumps(payload).encode()
    return await client.post(
        path,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Signature": signature if signature is not None else sign(WEBHOOK_SECRET, raw),
        },
    )


async def pay(api: httpx.AsyncClient, user_id: str, tokens: int, status: str = "succeeded",
              payment_id: str | None = None) -> httpx.Response:
    return await post_signed(api, "/webhooks/payments", {
        "payment_id": payment_id or f"pay-{uuid.uuid4()}",
        "user_id": user_id,
        "tokens": tokens,
        "status": status,
    })


async def new_user(api: httpx.AsyncClient, tokens: int) -> str:
    user_id = str(uuid.uuid4())
    r = await pay(api, user_id, tokens)
    assert r.status_code == 200 and r.json() == {"credited": True}, r.text
    return user_id


async def generate(api: httpx.AsyncClient, user_id: str, prompt: str,
                   model: str = "image-fast", refs: list[str] | None = None) -> httpx.Response:
    return await api.post("/generate", json={
        "user_id": user_id, "model_slug": model, "prompt": prompt, "ref_images": refs or [],
    })


async def start_task(api: httpx.AsyncClient, user_id: str, prompt: str,
                     model: str = "image-fast", refs: list[str] | None = None) -> str:
    r = await generate(api, user_id, prompt, model, refs)
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


async def wait_until(fn, timeout: float, interval: float = 0.3, what: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await fn()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"timeout {timeout}s waiting for {what}; last={last!r}")


async def get_task(api: httpx.AsyncClient, task_id: str) -> dict:
    r = await api.get(f"/tasks/{task_id}")
    assert r.status_code == 200, r.text
    return r.json()


async def wait_for_task(api: httpx.AsyncClient, task_id: str, timeout: float = 30,
                        statuses=TERMINAL, predicate=None) -> dict:
    async def check():
        task = await get_task(api, task_id)
        if predicate is not None:
            return task if predicate(task) else None
        return task if task["status"] in statuses else None

    return await wait_until(check, timeout, what=f"task {task_id}")


async def balance(api: httpx.AsyncClient, user_id: str) -> int:
    r = await api.get(f"/users/{user_id}/balance")
    assert r.status_code == 200, r.text
    return r.json()["balance"]


async def script(ctl: httpx.AsyncClient, marker: str, *items: dict) -> None:
    body = [{"match_prompt": marker, **item} for item in (items or ({},))]
    r = await ctl.post("/_control/script", json=body)
    assert r.status_code == 200, r.text


async def provider_record(ctl: httpx.AsyncClient, marker: str, timeout: float = 15) -> dict:
    async def check():
        tasks = (await ctl.get("/_control/tasks", params={"match": marker})).json()["tasks"]
        return tasks[0] if tasks else None

    return await wait_until(check, timeout, what=f"provider task for {marker}")


async def deliver(ctl: httpx.AsyncClient, external_id: str, **opts) -> list[dict]:
    r = await ctl.post(f"/_control/tasks/{external_id}/deliver", json=opts)
    assert r.status_code == 200, r.text
    return r.json()["responses"]


async def task_ledger(db, task_id: str) -> list:
    return await db.fetch(
        "SELECT kind, delta FROM ledger WHERE task_id = $1 ORDER BY id", uuid.UUID(task_id)
    )


async def webhook_outcomes(db, attempt_id: str) -> list[str]:
    rows = await db.fetch(
        "SELECT outcome FROM webhook_events WHERE body->>'attempt_id' = $1 ORDER BY id",
        attempt_id,
    )
    return [r["outcome"] for r in rows]
