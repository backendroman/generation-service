import json
import uuid

import asyncpg
import httpx
import pytest

from tests.helpers import ALPHA_URL, APP_URL, BETA_URL, DATABASE_URL


@pytest.fixture
async def api():
    async with httpx.AsyncClient(base_url=APP_URL, timeout=30) as client:
        yield client


@pytest.fixture
async def alpha_ctl():
    async with httpx.AsyncClient(base_url=ALPHA_URL, timeout=60) as client:
        yield client


@pytest.fixture
async def beta_ctl():
    async with httpx.AsyncClient(base_url=BETA_URL, timeout=60) as client:
        yield client


@pytest.fixture
async def db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                              schema="pg_catalog")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def marker(alpha_ctl, beta_ctl):
    # held в конце отпускаем, чтобы не висели до следующего теста
    value = f"t-{uuid.uuid4().hex[:12]}"
    yield value
    for ctl in (alpha_ctl, beta_ctl):
        tasks = (await ctl.get("/_control/tasks", params={"match": value})).json()["tasks"]
        for rec in tasks:
            if rec["state"] == "held":
                await ctl.post(f"/_control/tasks/{rec['external_id']}/deliver",
                               json={"status": "completed"})


@pytest.fixture(autouse=True)
async def stack_guard(alpha_ctl, beta_ctl, db):
    for ctl in (alpha_ctl, beta_ctl):
        assert (await ctl.post("/_control/reset")).status_code == 200
    yield
    await assert_invariants(db)


async def assert_invariants(db) -> None:
    mismatch = await db.fetch(
        """
        SELECT u.id FROM users u LEFT JOIN ledger l ON l.user_id = u.id
        GROUP BY u.id, u.balance HAVING u.balance <> COALESCE(SUM(l.delta), 0)
        """
    )
    assert mismatch == [], f"balance != sum(ledger): {mismatch}"

    both = await db.fetch(
        """
        SELECT t.id FROM tasks t
        WHERE t.result_url IS NOT NULL
          AND EXISTS (SELECT 1 FROM ledger l WHERE l.task_id = t.id AND l.kind = 'refund')
        """
    )
    assert both == [], f"tasks with both result and refund: {both}"
