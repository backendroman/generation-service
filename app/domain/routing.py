from collections.abc import Iterable
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class Leg:
    id: int
    provider: str
    priority: int
    max_ref_images: int
    skip_on_moderation: bool


def suitable_legs(legs: Iterable[Leg], n_refs: int) -> list[Leg]:
    return sorted((leg for leg in legs if n_refs <= leg.max_ref_images), key=lambda x: x.priority)


def choose_next_leg(
    legs: Iterable[Leg], after_priority: int, n_refs: int, last_error: str | None
) -> Leg | None:
    # skip_on_moderation работает только после moderation
    for leg in suitable_legs(legs, n_refs):
        if leg.priority <= after_priority:
            continue
        if last_error == "moderation" and leg.skip_on_moderation:
            continue
        return leg
    return None


async def load_legs(conn: asyncpg.Connection, model_id: int) -> list[Leg]:
    rows = await conn.fetch(
        """
        SELECT id, provider, priority, max_ref_images, skip_on_moderation
        FROM model_legs WHERE model_id = $1 ORDER BY priority
        """,
        model_id,
    )
    return [Leg(**dict(r)) for r in rows]
