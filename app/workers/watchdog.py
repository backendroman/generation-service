"""Watchdog: двигает задачи без ответа дольше PROVIDER_TIMEOUT"""

import logging

import asyncpg

from app.config import settings
from app.domain.transitions import fail_attempt
from app.workers.dispatcher import Dispatcher

log = logging.getLogger(__name__)

EXPIRED_SQL = """
SELECT t.id AS task_id, a.id AS attempt_id
FROM tasks t JOIN attempts a ON a.id = t.current_attempt_id
WHERE t.status = 'running'
  AND a.status IN ('pending', 'sent')
  AND a.deadline_at < now()
ORDER BY a.deadline_at
LIMIT 1
FOR UPDATE OF t SKIP LOCKED
"""


class Watchdog:
    def __init__(self, pool: asyncpg.Pool, dispatcher: Dispatcher) -> None:
        self.pool = pool
        self.dispatcher = dispatcher

    async def tick(self) -> None:
        for _ in range(settings.watchdog_batch):
            async with self.pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(EXPIRED_SQL)
                if row is None:
                    return
                outcome, new_attempt_id = await fail_attempt(
                    conn, row["attempt_id"], "timeout", "watchdog", "deadline_exceeded"
                )
            log.info("watchdog: task %s attempt %s timed out (%s)",
                     row["task_id"], row["attempt_id"], outcome)
            if new_attempt_id is not None:
                self.dispatcher.dispatch_soon(new_attempt_id)
