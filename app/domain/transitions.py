# все функции тут зовутся внутри транзакции

import uuid

import asyncpg

from app.domain.attempts import create_attempt
from app.domain.routing import choose_next_leg, load_legs

ACTIVE_STATUSES = ("pending", "sent")


async def lock_task_and_attempt(
    conn: asyncpg.Connection, attempt_id: uuid.UUID
) -> tuple[asyncpg.Record | None, asyncpg.Record | None]:
    task_id = await conn.fetchval("SELECT task_id FROM attempts WHERE id = $1", attempt_id)
    if task_id is None:
        return None, None
    # сначала task, потом attempt - везде в таком порядке
    task = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1 FOR UPDATE", task_id)
    attempt = await conn.fetchrow(
        """
        SELECT a.*, l.priority FROM attempts a JOIN model_legs l ON l.id = a.leg_id
        WHERE a.id = $1 FOR UPDATE OF a
        """,
        attempt_id,
    )
    return task, attempt


async def set_external_id(
    conn: asyncpg.Connection, attempt: asyncpg.Record, external_id: str | None
) -> None:
    if not external_id or attempt["external_id"] is not None:
        return
    await conn.execute(
        """
        UPDATE attempts SET external_id = $2
        WHERE id = $1 AND external_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM attempts WHERE provider = $3 AND external_id = $2)
        """,
        attempt["id"], external_id, attempt["provider"],
    )


def is_active(task: asyncpg.Record, attempt: asyncpg.Record) -> bool:
    return (
        task["status"] == "running"
        and task["current_attempt_id"] == attempt["id"]
        and attempt["status"] in ACTIVE_STATUSES
    )


async def finish_failed(conn: asyncpg.Connection, task: asyncpg.Record, reason: str) -> None:
    # current_attempt_id оставляем, чтобы было видно последнюю ногу
    await conn.execute(
        """
        UPDATE tasks SET status = 'failed', failure_reason = $2,
                         updated_at = now(), finished_at = now()
        WHERE id = $1
        """,
        task["id"], reason,
    )
    await conn.execute(
        """
        INSERT INTO ledger (user_id, delta, kind, idempotency_key, task_id)
        VALUES ($1, $2, 'refund', $3, $4)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        task["user_id"], task["price"], f"refund:{task['id']}", task["id"],
    )


async def fail_attempt(
    conn: asyncpg.Connection,
    attempt_id: uuid.UUID,
    error_type: str,
    source: str,
    detail: str | None = None,
    external_id: str | None = None,
) -> tuple[str, uuid.UUID | None]:
    task, attempt = await lock_task_and_attempt(conn, attempt_id)
    if task is None:
        return "not_found", None
    await set_external_id(conn, attempt, external_id)
    if not is_active(task, attempt):
        return "stale", None

    await conn.execute(
        """
        UPDATE attempts SET status = $2, error_type = $3, error_source = $4,
                            error_detail = $5, finished_at = now(), dispatch_lease_until = NULL
        WHERE id = $1
        """,
        attempt_id, "timed_out" if error_type == "timeout" else "failed",
        error_type, source, detail,
    )

    if error_type == "payload":
        # с payload другая нога упадет так же
        await finish_failed(conn, task, "payload")
        return "applied", None

    legs = await load_legs(conn, task["model_id"])
    n_refs = len(task["ref_images"])
    next_leg = choose_next_leg(legs, attempt["priority"], n_refs, error_type)
    if next_leg is None:
        await finish_failed(conn, task, "no_legs_left")
        return "applied", None

    new_attempt_id = await create_attempt(conn, task["id"], next_leg)
    await conn.execute(
        "UPDATE tasks SET current_attempt_id = $2, updated_at = now() WHERE id = $1",
        task["id"], new_attempt_id,
    )
    return "applied", new_attempt_id


def _gave_up_without_answer(attempt: asyncpg.Record) -> bool:
    # мы сами бросили ждать, явного отказа не было
    if attempt["status"] == "timed_out":
        return True
    return (
        attempt["status"] == "failed"
        and attempt["error_source"] == "sync"
        and attempt["error_detail"] in ("timeout", "no_response")
    )


async def complete_attempt(
    conn: asyncpg.Connection,
    attempt_id: uuid.UUID,
    result_url: str,
    external_id: str | None = None,
) -> str:
    task, attempt = await lock_task_and_attempt(conn, attempt_id)
    if task is None:
        return "not_found"
    await set_external_id(conn, attempt, external_id)

    if is_active(task, attempt):
        await conn.execute(
            """
            UPDATE attempts SET status = 'completed', result_url = $2,
                                finished_at = now(), dispatch_lease_until = NULL
            WHERE id = $1
            """,
            attempt_id, result_url,
        )
        await conn.execute(
            """
            UPDATE tasks SET status = 'completed', result_url = $2,
                             updated_at = now(), finished_at = now()
            WHERE id = $1
            """,
            task["id"], result_url,
        )
        return "applied"

    if task["status"] == "running" and _gave_up_without_answer(attempt):
        # поздний результат, а задача еще жива - берем его
        await conn.execute(
            """
            UPDATE attempts SET status = 'abandoned', finished_at = now(),
                                dispatch_lease_until = NULL
            WHERE id = $1 AND status IN ('pending', 'sent')
            """,
            task["current_attempt_id"],
        )
        await conn.execute(
            """
            UPDATE attempts SET status = 'completed', result_url = $2, finished_at = now()
            WHERE id = $1
            """,
            attempt_id, result_url,
        )
        await conn.execute(
            """
            UPDATE tasks SET status = 'completed', result_url = $2, current_attempt_id = $3,
                             updated_at = now(), finished_at = now()
            WHERE id = $1
            """,
            task["id"], result_url, attempt_id,
        )
        return "late_accepted"

    if attempt["status"] == "completed":
        return "duplicate"

    await conn.execute(
        "UPDATE attempts SET late_result_url = $2 WHERE id = $1 AND late_result_url IS NULL",
        attempt_id, result_url,
    )
    return "late_ignored"
