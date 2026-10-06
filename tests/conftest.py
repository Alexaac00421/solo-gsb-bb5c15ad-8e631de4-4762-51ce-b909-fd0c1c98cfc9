"""Pytest fixtures: userspace PostgreSQL + in-process FastAPI app.

The integration suite runs against a real PostgreSQL instance (provided by
the ``pgserver`` package, no Docker/root needed). Set ``TEST_DATABASE_DSN``
to run against an external instance instead, e.g.::

    TEST_DATABASE_DSN="host=localhost dbname=fare_test user=fare password=farepass" \
        pytest tests/test_api.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _start_pgserver():
    import pgserver

    runtime_dir = os.environ.setdefault("XDG_RUNTIME_DIR", tempfile.mkdtemp(prefix="runtime-"))
    os.chmod(runtime_dir, 0o700)
    pgdata = tempfile.mkdtemp(prefix="pgdata-")
    server = pgserver.get_server(pgdata)
    server.ensure_postgres_running()
    # The unix socket is created inside the data directory.
    return {
        "host": pgdata,
        "port": 5432,
        "dbname": "postgres",
        "user": server.postgres_user,
        "password": "",
    }, server


@pytest.fixture(scope="session")
def db_kwargs():
    dsn = os.getenv("TEST_DATABASE_DSN")
    server = None
    if dsn:
        import psycopg
        kw = psycopg.conninfo.conninfo_to_dict(dsn)
        kw.setdefault("port", 5432)
    else:
        kw, server = _start_pgserver()
    yield kw
    if server is not None:
        server.cleanup()


@pytest.fixture()
async def client(db_kwargs):
    """Fresh schema + ASGI client for every test (full isolation)."""
    import psycopg

    from app import db as db_module
    from app.config import Settings
    from app.main import app

    # Reset the public schema so each test starts from an empty database.
    admin_kwargs = dict(db_kwargs)
    admin_kwargs.pop("options", None)
    with psycopg.connect(**admin_kwargs) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()

    settings = Settings(
        postgres_host=db_kwargs["host"],
        postgres_port=db_kwargs.get("port", 5432),
        postgres_db=db_kwargs["dbname"],
        postgres_user=db_kwargs["user"],
        postgres_password=db_kwargs.get("password", ""),
        daily_cap_cents=2000,
        transfer_gap_minutes=90,
        db_connect_retries=10,
    )
    app.state.settings = settings
    await db_module.init_pool(settings)

    import httpx
    from app.db import close_pool

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client

    await close_pool()
