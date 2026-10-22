import uuid

import asyncpg

from app.config import settings
from app.domain.routing import Leg


async def create_attempt(conn: asyncpg.Connection, task_id: uuid.UUID, leg: Leg) -> uuid.UUID:
    attempt_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO attempts (id, task_id, leg_id, provider, status, deadline_at)
        VALUES ($1, $2, $3, $4, 'pending', now() + make_interval(secs => $5))
        """,
        attempt_id, task_id, leg.id, leg.provider, settings.provider_timeout,
    )
    return attempt_id
