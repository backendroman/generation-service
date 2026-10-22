import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.domain.attempts import create_attempt
from app.domain.routing import load_legs, suitable_legs
from app.errors import ApiError

router = APIRouter()


class GenerateRequest(BaseModel):
    user_id: uuid.UUID
    model_slug: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    ref_images: list[str] = Field(default_factory=list)

    @field_validator("prompt")
    @classmethod
    def _prompt_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt must not be blank")
        return v


@router.post("/generate", status_code=202)
async def generate(req: GenerateRequest, request: Request):
    pool = request.app.state.pool
    n_refs = len(req.ref_images)

    async with pool.acquire() as conn:
        model = await conn.fetchrow(
            "SELECT id, slug, price FROM models WHERE slug = $1", req.model_slug
        )
        if model is None:
            raise ApiError(404, "model_not_found", f"unknown model {req.model_slug!r}")
        legs = suitable_legs(await load_legs(conn, model["id"]), n_refs)
        if not legs:
            raise ApiError(400, "no_suitable_leg", f"no leg accepts {n_refs} reference images")

        task_id = uuid.uuid4()
        async with conn.transaction():
            # Лочим юзера, иначе параллельные /generate проскочат лимит
            user = await conn.fetchrow(
                "SELECT id, balance FROM users WHERE id = $1 FOR UPDATE", req.user_id
            )
            if user is None:
                raise ApiError(404, "user_not_found", f"unknown user {req.user_id}")
            active = await conn.fetchval(
                "SELECT count(*) FROM tasks WHERE user_id = $1 AND status = 'running'",
                req.user_id,
            )
            if active >= settings.max_active_tasks:
                raise ApiError(429, "too_many_active_tasks",
                               f"user already has {active} running tasks")
            if user["balance"] < model["price"]:
                raise ApiError(402, "insufficient_tokens",
                               f"balance {user['balance']} < price {model['price']}")

            await conn.execute(
                """
                INSERT INTO tasks (id, user_id, model_id, prompt, ref_images, price, status)
                VALUES ($1, $2, $3, $4, $5, $6, 'running')
                """,
                task_id, req.user_id, model["id"], req.prompt, req.ref_images, model["price"],
            )
            await conn.execute(
                """
                INSERT INTO ledger (user_id, delta, kind, idempotency_key, task_id)
                VALUES ($1, $2, 'charge', $3, $4)
                """,
                req.user_id, -model["price"], f"charge:{task_id}", task_id,
            )
            attempt_id = await create_attempt(conn, task_id, legs[0])
            await conn.execute(
                "UPDATE tasks SET current_attempt_id = $2 WHERE id = $1", task_id, attempt_id
            )

    # не ждем тика диспетчера
    request.app.state.dispatcher.dispatch_soon(attempt_id)
    return JSONResponse({"task_id": str(task_id), "status": "running"}, status_code=202)
