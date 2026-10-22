import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


async def run_periodic(name: str, fn: Callable[[], Awaitable[None]], interval: float) -> None:
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s tick failed", name)
        await asyncio.sleep(interval * random.uniform(0.5, 1.5))
