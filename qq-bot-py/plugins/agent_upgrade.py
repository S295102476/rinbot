"""Schema and worker lifecycle for the independently switchable Agent upgrade."""
from __future__ import annotations
from nonebot import get_driver
from nonebot.log import logger
from .db import Base, engine, ensure_tables
from . import agent_memory_models
from .agent_memory_v2 import MEMORY_V2
from .developer_tasks import DEVELOPER_TABLES, get_developer_service


async def initialize():
    from .console_state import CONSOLE_TABLES
    from . import model_routes
    memory_tables = [value.__table__ for value in vars(agent_memory_models).values()
                     if isinstance(value, type) and value is not Base and getattr(value, "__tablename__", "").startswith("agent_v2_")]
    async with engine.begin() as conn:
        await ensure_tables(conn, memory_tables + DEVELOPER_TABLES + CONSOLE_TABLES)
    await model_routes.load()
    await MEMORY_V2.initialize()
    await get_developer_service().recover()
    logger.info("[agent_upgrade] routes, memory and developer workers ready")


async def shutdown():
    await MEMORY_V2.shutdown()
    retry = getattr(MEMORY_V2, "_manual_retry", None)
    if retry and not retry.done():
        import asyncio
        retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)
    service = get_developer_service()
    for task in tuple(service.workers.values()):
        task.cancel()
    if service.workers:
        import asyncio
        await asyncio.gather(*tuple(service.workers.values()), return_exceptions=True)


driver = get_driver()
driver.on_startup(initialize)
driver.on_shutdown(shutdown)
