import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api import generate, health, read, webhooks
from app.config import settings
from app.db import create_pool
from app.errors import install_error_handlers
from app.workers.dispatcher import Dispatcher
from app.workers.loop import run_periodic
from app.workers.watchdog import Watchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(process)d %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await create_pool()
    http = httpx.AsyncClient()
    dispatcher = Dispatcher(pool, http)
    app.state.pool = pool
    app.state.dispatcher = dispatcher
    # в каждом воркере, дубли отсекает SKIP LOCKED
    loops = [
        asyncio.create_task(run_periodic("dispatcher", dispatcher.tick,
                                         settings.dispatch_interval)),
        asyncio.create_task(run_periodic("watchdog", Watchdog(pool, dispatcher).tick,
                                         settings.watchdog_interval)),
    ]
    try:
        yield
    finally:
        for t in loops:
            t.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        await dispatcher.close()
        await http.aclose()
        await pool.close()


app = FastAPI(title="generation service", lifespan=lifespan)
install_error_handlers(app)
app.include_router(health.router)
app.include_router(generate.router)
app.include_router(webhooks.router)
app.include_router(read.router)
