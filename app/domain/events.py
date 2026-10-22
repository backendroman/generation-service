from typing import Any

import asyncpg


# TODO: чистить старые записи
async def record_webhook_event(
    conn: asyncpg.Connection, source: str, body: Any, outcome: str
) -> None:
    await conn.execute(
        "INSERT INTO webhook_events (source, body, outcome) VALUES ($1, $2, $3)",
        source, body, outcome,
    )
