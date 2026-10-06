"""PostgreSQL connection pool lifecycle and schema bootstrap."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import AsyncIterator

import psycopg
from psycopg_pool import AsyncConnectionPool

from .config import Settings

logger = logging.getLogger(__name__)

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

_pool: AsyncConnectionPool | None = None


async def init_pool(settings: Settings) -> AsyncConnectionPool:
    """Open the connection pool, retrying until the database is reachable."""
    global _pool
    last_error: Exception | None = None
    kwargs = settings.conninfo_kwargs
    for attempt in range(1, settings.db_connect_retries + 1):
        try:
            pool = AsyncConnectionPool(
                min_size=1,
                max_size=10,
                kwargs=kwargs,
                open=False,
                check=AsyncConnectionPool.check_connection,
            )
            await pool.open()
            await pool.wait()
            # Initialize schema once the database is confirmed reachable.
            async with pool.connection() as conn:
                await conn.execute(_SCHEMA_SQL)
                await conn.commit()
            _pool = pool
            logger.info("database pool ready (attempt %d)", attempt)
            return pool
        except Exception as exc:  # pragma: no cover - startup path
            last_error = exc
            logger.warning("database not ready (attempt %d): %s", attempt, exc)
            try:
                await pool.close()
            except Exception:
                pass
            time.sleep(1)
    raise RuntimeError(f"could not connect to database after retries: {last_error}")


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool is not initialized")
    return _pool


async def get_conn() -> AsyncIterator[psycopg.AsyncConnection]:
    """FastAPI dependency: one connection per request.

    The service layer manages the transaction boundaries explicitly
    (BEGIN on entry, COMMIT/ROLLBACK on success/error).
    """
    pool = get_pool()
    async with pool.connection() as conn:
        yield conn
