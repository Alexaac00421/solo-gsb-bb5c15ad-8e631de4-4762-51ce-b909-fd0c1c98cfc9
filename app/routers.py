"""HTTP API layer."""
from __future__ import annotations

from datetime import date
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from . import service
from .db import get_conn
from .schemas import (
    DaySettlementResponse,
    HealthResponse,
    JournalPage,
    SubmitResponse,
    TripRecord,
    TripCorrectRequest,
    TripRevokeRequest,
    TripSubmitRequest,
    as_shanghai_iso,
)

router = APIRouter()


def _settings(request: Request):
    return request.app.state.settings


def _serialise_trip(trip: dict[str, Any]) -> dict[str, Any]:
    data = dict(trip)
    data["board_time"] = as_shanghai_iso(data["board_time"])
    return data


def _serialise_journal(item: dict[str, Any]) -> dict[str, Any]:
    data = dict(item)
    data["created_at"] = as_shanghai_iso(data["created_at"])
    return data


def _serialise_submit(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "trip": _serialise_trip(result["trip"]),
        "idempotent": result["idempotent"],
        "recalculated_dates": result["recalculated_dates"],
        "journals": [_serialise_journal(j) for j in result["journals"]],
    }


@router.get("/health", response_model=HealthResponse, tags=["meta"])
async def health(conn: psycopg.AsyncConnection = Depends(get_conn)):
    try:
        await conn.execute("SELECT 1")
        await conn.commit()
        return HealthResponse(status="ok", db="up")
    except Exception:
        await conn.rollback()
        return HealthResponse(status="degraded", db="down")


@router.post("/trips", response_model=SubmitResponse, tags=["trips"])
async def submit_trip(
    payload: TripSubmitRequest,
    request: Request,
    conn: psycopg.AsyncConnection = Depends(get_conn),
):
    result = await service.submit_trip(
        conn,
        account_id=payload.account_id,
        trip_no=payload.trip_no,
        board_time=payload.board_time,
        base_fare=payload.base_fare,
        settings=_settings(request),
    )
    # 201 for a newly recorded trip; 200 for an identical idempotent replay.
    return JSONResponse(
        content=SubmitResponse(**_serialise_submit(result)).model_dump(mode="json"),
        status_code=200 if result["idempotent"] else 201,
    )


@router.post("/trips/{trip_no}/correct", response_model=SubmitResponse, tags=["trips"])
async def correct_trip(
    trip_no: str,
    payload: TripCorrectRequest,
    request: Request,
    conn: psycopg.AsyncConnection = Depends(get_conn),
):
    result = await service.correct_trip(
        conn,
        trip_no=trip_no,
        expected_version=payload.expected_version,
        board_time=payload.board_time,
        base_fare=payload.base_fare,
        settings=_settings(request),
    )
    return _serialise_submit(result)


@router.post("/trips/{trip_no}/revoke", response_model=SubmitResponse, tags=["trips"])
async def revoke_trip(
    trip_no: str,
    payload: TripRevokeRequest,
    request: Request,
    conn: psycopg.AsyncConnection = Depends(get_conn),
):
    result = await service.revoke_trip(
        conn,
        trip_no=trip_no,
        expected_version=payload.expected_version,
        settings=_settings(request),
    )
    return _serialise_submit(result)


@router.get("/trips/{trip_no}", response_model=TripRecord, tags=["trips"])
async def get_trip(trip_no: str, conn: psycopg.AsyncConnection = Depends(get_conn)):
    trip = await service.get_trip(conn, trip_no=trip_no)
    return _serialise_trip(trip)


@router.get(
    "/accounts/{account_id}/operating-days/{op_date}",
    response_model=DaySettlementResponse,
    tags=["settlement"],
)
async def get_operating_day(
    account_id: str,
    op_date: date,
    request: Request,
    conn: psycopg.AsyncConnection = Depends(get_conn),
):
    return await service.get_day_settlement(
        conn,
        account_id=account_id,
        op_day=op_date,
        cap_cents=_settings(request).daily_cap_cents,
    )


@router.get("/journals", response_model=JournalPage, tags=["journals"])
async def list_journals(
    after: int = Query(0, ge=0, description="Return rows with seq greater than this cursor"),
    limit: int = Query(50, ge=1, le=500),
    account_id: str | None = Query(None, max_length=64),
    conn: psycopg.AsyncConnection = Depends(get_conn),
):
    page = await service.list_journals(
        conn, after_seq=after, limit=limit, account_id=account_id
    )
    page["items"] = [_serialise_journal(item) for item in page["items"]]
    return page
