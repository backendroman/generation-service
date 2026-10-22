"""Outbox: шлет pending попытки провайдерам, HTTP вне транзакции"""

import asyncio
import logging
import uuid

import asyncpg
import httpx

from app.config import settings
from app.domain.transitions import fail_attempt

log = logging.getLogger(__name__)

CLAIM_SQL = """
UPDATE attempts a
SET dispatch_lease_until = now() + make_interval(secs => $1),
    dispatch_tries = a.dispatch_tries + 1
FROM tasks t, models m
WHERE a.id IN (
        SELECT id FROM attempts
        WHERE status = 'pending'
          AND (dispatch_lease_until IS NULL OR dispatch_lease_until < now())
          AND ($2::uuid IS NULL OR id = $2)
          AND created_at < now() - make_interval(secs => $3)
        ORDER BY created_at
        LIMIT $4
        FOR UPDATE SKIP LOCKED)
  AND t.id = a.task_id
  AND m.id = t.model_id
RETURNING a.id, a.provider, t.prompt, t.ref_images, m.slug AS model_slug
"""

# свежие и так уходят сразу из запроса
BACKGROUND_MIN_AGE = 2.0


class Dispatcher:
    def __init__(self, pool: asyncpg.Pool, http: httpx.AsyncClient) -> None:
        self.pool = pool
        self.http = http
        self._inflight: set[asyncio.Task] = set()

    def dispatch_soon(self, attempt_id: uuid.UUID) -> None:
        task = asyncio.create_task(self._dispatch_one(attempt_id))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def close(self) -> None:
        for t in list(self._inflight):
            t.cancel()
        await asyncio.gather(*self._inflight, return_exceptions=True)

    async def tick(self) -> None:
        rows = await self._claim(None, settings.dispatch_batch, BACKGROUND_MIN_AGE)
        if not rows:
            return
        log.info("dispatcher picked up %d pending attempts", len(rows))
        await asyncio.gather(*(self._send_safe(r) for r in rows))

    async def _dispatch_one(self, attempt_id: uuid.UUID) -> None:
        try:
            for row in await self._claim(attempt_id, 1, 0.0):
                await self._send(row)
        except Exception:
            log.exception("immediate dispatch of %s failed", attempt_id)

    async def _claim(self, attempt_id, limit: int, min_age: float) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                CLAIM_SQL, settings.dispatch_lease, attempt_id, min_age, limit
            )

    async def _send_safe(self, row: asyncpg.Record) -> None:
        try:
            await self._send(row)
        except Exception:
            log.exception("dispatch of %s failed", row["id"])

    async def _send(self, row: asyncpg.Record) -> None:
        attempt_id = row["id"]
        provider = row["provider"]
        payload = {
            "attempt_id": str(attempt_id),
            "model_slug": row["model_slug"],
            "prompt": row["prompt"],
            "ref_images": row["ref_images"],
            "callback_url": f"{settings.public_base_url}/webhooks/providers/{provider}",
        }
        try:
            r = await self.http.post(
                f"{settings.provider_url(provider)}/tasks",
                json=payload,
                headers={"Idempotency-Key": str(attempt_id)},
                timeout=httpx.Timeout(settings.provider_http_timeout),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            await self._sync_fail(attempt_id, "technical", "connect_error")
            return
        except httpx.TimeoutException:
            # провайдер мог успеть принять задачу
            await self._sync_fail(attempt_id, "technical", "timeout")
            return
        except httpx.TransportError as e:
            # same
            log.info("attempt %s transport error: %r", attempt_id, e)
            await self._sync_fail(attempt_id, "technical", "no_response")
            return

        code = r.status_code
        if 200 <= code < 300:
            external_id = _external_id(r)
            if external_id is None:
                await self._sync_fail(attempt_id, "technical", "bad_response")
            else:
                await self._mark_sent(attempt_id, provider, external_id)
        elif 400 <= code < 500 and code not in (408, 429):
            await self._sync_fail(attempt_id, "payload", f"http_{code}")
        else:
            await self._sync_fail(attempt_id, "technical", f"http_{code}")

    async def _mark_sent(self, attempt_id: uuid.UUID, provider: str, external_id: str) -> None:
        async with self.pool.acquire() as conn:
            # вебхук мог нас обогнать
            res = await conn.execute(
                """
                UPDATE attempts SET status = 'sent', external_id = $2, sent_at = now(),
                       deadline_at = now() + make_interval(secs => $3),
                       dispatch_lease_until = NULL
                WHERE id = $1 AND status = 'pending'
                """,
                attempt_id, external_id, settings.provider_timeout,
            )
            if res == "UPDATE 0":
                log.info("attempt %s already closed before 'sent' was recorded", attempt_id)
                await conn.execute(
                    """
                    UPDATE attempts SET external_id = $2
                    WHERE id = $1 AND external_id IS NULL
                      AND NOT EXISTS (SELECT 1 FROM attempts
                                      WHERE provider = $3 AND external_id = $2)
                    """,
                    attempt_id, external_id, provider,
                )

    async def _sync_fail(self, attempt_id: uuid.UUID, error_type: str, detail: str) -> None:
        log.info("attempt %s sync failure: %s %s", attempt_id, error_type, detail)
        async with self.pool.acquire() as conn, conn.transaction():
            _, new_attempt_id = await fail_attempt(conn, attempt_id, error_type, "sync", detail)
        if new_attempt_id is not None:
            self.dispatch_soon(new_attempt_id)


def _external_id(r: httpx.Response) -> str | None:
    try:
        value = r.json().get("external_id")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None
