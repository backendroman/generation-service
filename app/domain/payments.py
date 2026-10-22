from uuid import UUID

import asyncpg
from pydantic import BaseModel, Field


class PaymentWebhook(BaseModel):
    payment_id: str = Field(min_length=1)
    user_id: UUID
    tokens: int = Field(strict=True, gt=0)
    status: str = Field(min_length=1)


class PaymentConflict(Exception):
    pass


async def apply_payment(conn: asyncpg.Connection, p: PaymentWebhook) -> str:
    status = p.status.lower()
    # регистрации нет, юзер появляется с первым платежом
    await conn.execute("INSERT INTO users (id) VALUES ($1) ON CONFLICT DO NOTHING", p.user_id)
    await conn.execute(
        """
        INSERT INTO payments (payment_id, user_id, tokens, status)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (payment_id) DO NOTHING
        """,
        p.payment_id, p.user_id, p.tokens, status,
    )
    row = await conn.fetchrow(
        "SELECT user_id, tokens, credited_at FROM payments WHERE payment_id = $1 FOR UPDATE",
        p.payment_id,
    )
    if row["user_id"] != p.user_id or row["tokens"] != p.tokens:
        raise PaymentConflict(p.payment_id)

    # Поздний pending не должен затереть succeeded.
    await conn.execute(
        """
        UPDATE payments SET status = $2, updated_at = now()
        WHERE payment_id = $1 AND credited_at IS NULL
        """,
        p.payment_id, status,
    )
    if status != "succeeded" or row["credited_at"] is not None:
        return "recorded"

    inserted = await conn.fetchval(
        """
        INSERT INTO ledger (user_id, delta, kind, idempotency_key, payment_id)
        VALUES ($1, $2, 'payment', 'payment:' || $3::text, $3)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        p.user_id, p.tokens, p.payment_id,
    )
    await conn.execute(
        "UPDATE payments SET credited_at = now(), updated_at = now() WHERE payment_id = $1",
        p.payment_id,
    )
    return "credited" if inserted else "recorded"
