import json
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.config import PROVIDERS, settings
from app.domain.events import record_webhook_event
from app.domain.payments import PaymentConflict, PaymentWebhook, apply_payment
from app.domain.transitions import complete_attempt, fail_attempt
from app.errors import ApiError
from app.security import verify

log = logging.getLogger(__name__)
router = APIRouter()


class ProviderWebhook(BaseModel):
    attempt_id: uuid.UUID | None = None
    external_id: str | None = Field(default=None, min_length=1)
    status: Literal["completed", "failed"]
    result_url: str | None = Field(default=None, min_length=1)
    error_type: Literal["technical", "moderation", "payload"] | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.attempt_id is None and self.external_id is None:
            raise ValueError("attempt_id or external_id is required")
        if self.status == "completed" and not self.result_url:
            raise ValueError("result_url is required for completed")
        if self.status == "failed" and not self.error_type:
            raise ValueError("error_type is required for failed")
        return self


async def _signed_body(request: Request) -> bytes:
    raw = await request.body()
    if not verify(settings.webhook_secret, raw, request.headers.get("X-Signature")):
        raise ApiError(401, "bad_signature", "X-Signature does not match body")
    return raw


def _parse(model: type[BaseModel], raw: bytes):
    # чтобы 422 выглядел как у /generate
    try:
        return model.model_validate_json(raw)
    except ValidationError as e:
        raise RequestValidationError(
            [{**err, "loc": ("body", *err["loc"])} for err in e.errors()]
        ) from None


async def _find_attempt(conn, name: str, hook: ProviderWebhook) -> uuid.UUID | None:
    # attempt_id может оказаться от другого провайдера
    if hook.attempt_id is not None:
        attempt_id = await conn.fetchval(
            "SELECT id FROM attempts WHERE id = $1 AND provider = $2", hook.attempt_id, name
        )
        if attempt_id is not None:
            return attempt_id
    if hook.external_id is not None:
        return await conn.fetchval(
            "SELECT id FROM attempts WHERE provider = $1 AND external_id = $2",
            name, hook.external_id,
        )
    return None


@router.post("/webhooks/providers/{name}")
async def provider_webhook(name: str, request: Request):
    raw = await _signed_body(request)
    if name not in PROVIDERS:
        raise ApiError(404, "unknown_provider", f"unknown provider {name!r}")
    hook = _parse(ProviderWebhook, raw)
    body = json.loads(raw)

    pool = request.app.state.pool
    new_attempt_id = None
    async with pool.acquire() as conn:
        attempt_id = await _find_attempt(conn, name, hook)
        if attempt_id is None:
            await record_webhook_event(conn, name, body, "not_found")
            raise ApiError(404, "attempt_not_found", "no attempt for this webhook")

        # 200 только после коммита
        async with conn.transaction():
            if hook.status == "completed":
                outcome = await complete_attempt(
                    conn, attempt_id, hook.result_url, hook.external_id
                )
            else:
                outcome, new_attempt_id = await fail_attempt(
                    conn, attempt_id, hook.error_type, "webhook", None, hook.external_id
                )
            await record_webhook_event(conn, name, body, outcome)

    if new_attempt_id is not None:
        request.app.state.dispatcher.dispatch_soon(new_attempt_id)
    return {"outcome": outcome}


@router.post("/webhooks/payments")
async def payment_webhook(request: Request):
    raw = await _signed_body(request)
    payment = _parse(PaymentWebhook, raw)
    body = json.loads(raw)

    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn, conn.transaction():
            outcome = await apply_payment(conn, payment)
            await record_webhook_event(conn, "payments", body, outcome)
    except PaymentConflict:
        async with pool.acquire() as conn:
            await record_webhook_event(conn, "payments", body, "conflict")
        log.warning("payment %s conflicts with stored data", payment.payment_id)
        raise ApiError(
            409, "payment_conflict", "payment_id already recorded with other user_id/tokens"
        ) from None
    return {"credited": outcome == "credited"}
