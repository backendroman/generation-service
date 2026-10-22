"""Фейковый провайдер, один на alpha и beta"""

import asyncio
import json
import logging
import os
import random
import uuid
from typing import Any

import httpx
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

from app.security import sign

NAME = os.environ.get("PROVIDER_NAME", "alpha")
SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret")
FAILURE_RATE = float(os.environ.get("FAILURE_RATE", "0"))
FAILURE_TYPE = os.environ.get("FAILURE_TYPE", "technical")
DELAY_MIN = float(os.environ.get("DELAY_MIN", "2"))
DELAY_MAX = float(os.environ.get("DELAY_MAX", "20"))
SYNC_TIMEOUT_SLEEP = 10
WEBHOOK_RETRIES = 3

PLAN = {"outcome": "completed", "error_type": "technical", "delay": 0.5, "sync": "none",
        "timeout_then_process": False, "hold": False}

log = logging.getLogger(f"provider.{NAME}")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

app = FastAPI(title=f"fake provider {NAME}")

scripts: list[dict[str, Any]] = []
tasks: dict[str, dict[str, Any]] = {}  # attempt_id -> rec
background: set[asyncio.Task] = set()
http = httpx.AsyncClient(timeout=10)


def make_plan(prompt: str) -> dict[str, Any]:
    for i, s in enumerate(scripts):
        if s.get("match_prompt", "") in prompt:
            return {**PLAN, **scripts.pop(i)}
    failed = random.random() < FAILURE_RATE
    return {**PLAN, "outcome": "failed" if failed else "completed", "error_type": FAILURE_TYPE,
            "delay": random.uniform(DELAY_MIN, DELAY_MAX)}


def webhook_body(rec: dict[str, Any], status: str, error_type: str | None) -> bytes:
    payload = {"attempt_id": rec["attempt_id"], "external_id": rec["external_id"],
               "status": status}
    if status == "completed":
        payload["result_url"] = f"https://files.fake/{NAME}/{rec['external_id']}.png"
    else:
        payload["error_type"] = error_type or "technical"
    return json.dumps(payload).encode()


async def post_webhook(url: str, raw: bytes, retries: int) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "X-Signature": sign(SECRET, raw)}
    for i in range(retries + 1):
        try:
            r = await http.post(url, content=raw, headers=headers)
            if r.status_code < 500 or i == retries:
                try:
                    body = r.json()
                except ValueError:
                    body = r.text
                return {"status_code": r.status_code, "body": body}
        except httpx.TransportError as e:
            if i == retries:
                return {"status_code": None, "body": repr(e)}
        await asyncio.sleep(1)


async def send(rec: dict[str, Any], status: str, error_type: str | None, copies: int = 1,
               concurrent: bool = False, retries: int = 0) -> list[dict[str, Any]]:
    raw = webhook_body(rec, status, error_type)
    posts = [post_webhook(rec["callback_url"], raw, retries) for _ in range(copies)]
    if concurrent:
        results = list(await asyncio.gather(*posts))
    else:
        results = [await p for p in posts]
    log.info("webhook %s %s x%d -> %s", rec["external_id"], status, copies,
             [r["status_code"] for r in results])
    return results


async def deliver_later(rec: dict[str, Any]) -> None:
    plan = rec["plan"]
    await asyncio.sleep(float(plan["delay"]))
    await send(rec, plan["outcome"], plan["error_type"], retries=WEBHOOK_RETRIES)
    rec["state"] = "delivered"


def register(attempt_id: str, body: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    rec = {
        "attempt_id": attempt_id,
        "external_id": f"{NAME}-{uuid.uuid4()}",
        "prompt": body.get("prompt", ""),
        "callback_url": body["callback_url"],
        "plan": plan,
        "state": "held" if plan["hold"] else "scheduled",
    }
    tasks[attempt_id] = rec
    if not plan["hold"]:
        t = asyncio.create_task(deliver_later(rec))
        background.add(t)
        t.add_done_callback(background.discard)
    return rec


@app.post("/tasks")
async def create_task(request: Request):
    body = await request.json()
    attempt_id = request.headers.get("Idempotency-Key") or body.get("attempt_id")
    if not attempt_id or not body.get("callback_url"):
        return JSONResponse({"error": "attempt_id and callback_url required"}, status_code=422)

    if attempt_id in tasks:
        return JSONResponse({"external_id": tasks[attempt_id]["external_id"]}, status_code=202)

    plan = make_plan(body.get("prompt", ""))
    sync = str(plan["sync"])
    if sync == "503":
        return JSONResponse({"error": "unavailable"}, status_code=503)
    if sync == "timeout":
        # timeout_then_process: задачу берем, но отвечаем дольше, чем ждет клиент
        rec = register(attempt_id, body, plan) if plan["timeout_then_process"] else None
        await asyncio.sleep(SYNC_TIMEOUT_SLEEP)
        if rec is None:
            return JSONResponse({"error": "too slow"}, status_code=503)
        return JSONResponse({"external_id": rec["external_id"]}, status_code=202)

    rec = register(attempt_id, body, plan)
    return JSONResponse({"external_id": rec["external_id"]}, status_code=202)


@app.get("/health")
async def health():
    return {"provider": NAME}


# ручки для тестов

@app.post("/_control/script")
async def add_script(items: list[dict[str, Any]] = Body(...)):
    scripts.extend(items)
    return {"queued": len(scripts)}


@app.post("/_control/reset")
async def reset():
    scripts.clear()
    tasks.clear()
    return {"ok": True}


@app.get("/_control/tasks")
async def list_tasks(match: str | None = None):
    return {"tasks": [r for r in tasks.values() if not match or match in r["prompt"]]}


@app.post("/_control/tasks/{external_id}/deliver")
async def deliver(external_id: str, opts: dict[str, Any] | None = Body(default=None)):
    rec = next((r for r in tasks.values() if r["external_id"] == external_id), None)
    if rec is None:
        return JSONResponse({"error": "unknown external_id"}, status_code=404)
    opts = opts or {}
    responses = await send(rec, opts.get("status", rec["plan"]["outcome"]),
                           opts.get("error_type", rec["plan"]["error_type"]),
                           int(opts.get("copies", 1)), bool(opts.get("concurrent", False)))
    rec["state"] = "delivered"
    return {"responses": responses}
