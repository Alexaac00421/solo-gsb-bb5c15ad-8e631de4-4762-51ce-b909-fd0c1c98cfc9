"""Pydantic request/response models."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from .timeutil import SHANGHAI_TZ


class TripSubmitRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    trip_no: str = Field(min_length=1, max_length=64)
    # ISO-8601; naive values are interpreted as Beijing time.
    board_time: datetime
    base_fare: int = Field(ge=0)


class TripCorrectRequest(BaseModel):
    expected_version: int = Field(ge=1, description="Current version the caller saw")
    board_time: datetime
    base_fare: int = Field(ge=0)


class TripRevokeRequest(BaseModel):
    expected_version: int = Field(ge=1)


class TripRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trip_no: str
    account_id: str
    board_time: datetime
    base_fare: int
    version: int
    status: str
    op_date: date


class SubmitResponse(BaseModel):
    trip: TripRecord
    idempotent: bool = False
    recalculated_dates: list[date]
    journals: list["JournalRecord"] = Field(default_factory=list)


class DayGroupLeg(BaseModel):
    trip_no: str
    board_time: datetime
    base_fare: int


class DayGroup(BaseModel):
    group_index: int
    group_fare: int
    legs: list[DayGroupLeg]


class DaySettlementResponse(BaseModel):
    account_id: str
    op_date: date
    status: str
    raw_fare: int
    charged_fare: int
    cap_cents: int
    trip_count: int
    groups: list[DayGroup]


class JournalRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    account_id: str
    op_date: date
    amount: int
    entry_type: str
    reason: str
    trip_no: str | None
    created_at: datetime


class JournalPage(BaseModel):
    items: list[JournalRecord]
    next_cursor: int | None
    has_more: bool


class HealthResponse(BaseModel):
    status: str
    db: str


def as_shanghai_iso(dt: datetime) -> datetime:
    """Normalize a datetime for JSON output (Beijing time, tz-aware)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI_TZ)
    return dt.astimezone(SHANGHAI_TZ)
