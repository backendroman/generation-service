import uuid

from fastapi import APIRouter, Query, Request

from app.errors import ApiError

router = APIRouter()


def _ts(value):
    return value.isoformat() if value is not None else None


@router.get("/tasks/{task_id}")
async def get_task(task_id: uuid.UUID, request: Request):
    async with request.app.state.pool.acquire() as conn:
        task = await conn.fetchrow(
            """
            SELECT t.*, m.slug AS model_slug,
                   EXISTS (SELECT 1 FROM ledger l
                           WHERE l.task_id = t.id AND l.kind = 'refund') AS refunded
            FROM tasks t JOIN models m ON m.id = t.model_id
            WHERE t.id = $1
            """,
            task_id,
        )
        if task is None:
            raise ApiError(404, "task_not_found", f"unknown task {task_id}")
        attempts = await conn.fetch(
            """
            SELECT a.*, l.priority FROM attempts a JOIN model_legs l ON l.id = a.leg_id
            WHERE a.task_id = $1 ORDER BY a.created_at, l.priority
            """,
            task_id,
        )

    history = [
        {
            "attempt_id": str(a["id"]),
            "provider": a["provider"],
            "priority": a["priority"],
            "status": a["status"],
            "error_type": a["error_type"],
            "error_source": a["error_source"],
            "error_detail": a["error_detail"],
            "external_id": a["external_id"],
            "result_url": a["result_url"],
            "late_result_url": a["late_result_url"],
            "created_at": _ts(a["created_at"]),
            "sent_at": _ts(a["sent_at"]),
            "finished_at": _ts(a["finished_at"]),
        }
        for a in attempts
    ]
    current = next((h for h in history if h["attempt_id"] == str(task["current_attempt_id"])),
                   None)
    return {
        "id": str(task["id"]),
        "user_id": str(task["user_id"]),
        "model_slug": task["model_slug"],
        "status": task["status"],
        "price": task["price"],
        "result_url": task["result_url"],
        "failure_reason": task["failure_reason"],
        "current_leg": current and {
            "provider": current["provider"],
            "priority": current["priority"],
            "attempt_id": current["attempt_id"],
        },
        "attempts": history,
        "refunded": task["refunded"],
        "created_at": _ts(task["created_at"]),
        "finished_at": _ts(task["finished_at"]),
    }


@router.get("/users/{user_id}/balance")
async def get_balance(user_id: uuid.UUID, request: Request,
                      limit: int = Query(default=20, ge=1, le=200)):
    async with request.app.state.pool.acquire() as conn:
        balance = await conn.fetchval("SELECT balance FROM users WHERE id = $1", user_id)
        if balance is None:
            raise ApiError(404, "user_not_found", f"unknown user {user_id}")
        rows = await conn.fetch(
            """
            SELECT id, delta, kind, task_id, payment_id, created_at FROM ledger
            WHERE user_id = $1 ORDER BY id DESC LIMIT $2
            """,
            user_id, limit,
        )
    return {
        "user_id": str(user_id),
        "balance": balance,
        "ledger": [
            {
                "id": r["id"],
                "delta": r["delta"],
                "kind": r["kind"],
                "task_id": str(r["task_id"]) if r["task_id"] else None,
                "payment_id": r["payment_id"],
                "created_at": _ts(r["created_at"]),
            }
            for r in rows
        ],
    }
