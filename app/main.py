"""FastAPI application entrypoint: uvicorn app.main:app."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import load_settings
from .db import close_pool, init_pool
from .errors import ServiceError
from .routers import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    await init_pool(settings)
    yield
    await close_pool()


app = FastAPI(
    title="Bus Transfer Fare Clearing Service",
    version="1.0.0",
    description=(
        "Ingest bus rides, group consecutive legs into transfer chains, "
        "cap the daily amount per account, and expose an append-only ledger."
    ),
    lifespan=lifespan,
)

app.include_router(router)


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "VALIDATION_ERROR", "details": exc.errors()}},
    )


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "bus-fare-clearing", "docs": "/docs"}
