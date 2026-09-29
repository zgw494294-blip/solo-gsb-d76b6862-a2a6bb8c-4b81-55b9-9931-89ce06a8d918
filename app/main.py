"""FastAPI application: offline HLS window reconciliation."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic import field_validator

from .hls import PlaylistParseError
from .fetchplan import build_fetch_plan
from .locate import (
    LocateError,
    format_program_time,
    locate,
    parse_program_time,
)
from .reconcile import ReconcileError, reconcile
from .vod import export_vod


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    playlist_url: str = Field(..., min_length=1)
    snapshots: list[str] = Field(..., min_length=2, max_length=30)

    @field_validator("playlist_url")
    @classmethod
    def _url_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("playlist_url must not be blank")
        return value

    @field_validator("snapshots")
    @classmethod
    def _snapshots_non_empty(cls, value: list[str]) -> list[str]:
        for i, snapshot in enumerate(value):
            if not snapshot or not snapshot.strip():
                raise ValueError(f"snapshot {i} is empty")
        return value


class LocateRequest(ReconcileRequest):
    target_at: str = Field(..., min_length=1)

    @field_validator("target_at")
    @classmethod
    def _target_timezone_aware_rfc3339(cls, value: str) -> str:
        try:
            parse_program_time(value)
        except PlaylistParseError as exc:
            raise ValueError(
                "target_at must be an RFC 3339 date-time with a timezone offset"
            ) from exc
        return value


class VodRequest(ReconcileRequest):
    from_sec: str = Field(..., min_length=1)
    to_sec: str = Field(..., min_length=1)

    @field_validator("playlist_url")
    @classmethod
    def _url_absolute_http(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("playlist_url must be an absolute http(s) URL")
        return value

    @field_validator("from_sec", "to_sec", mode="before")
    @classmethod
    def _coerce_decimal_bound(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("must be a decimal number of seconds")
        if isinstance(value, (int, float, Decimal)):
            return str(value)
        return value

    @field_validator("from_sec", "to_sec")
    @classmethod
    def _decimal_seconds(cls, value: str) -> str:
        try:
            number = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError("must be a decimal number of seconds") from exc
        if not number.is_finite() or number < 0:
            raise ValueError("must be a finite, non-negative decimal number of seconds")
        return value


class FetchPlanRequest(ReconcileRequest):
    @field_validator("playlist_url")
    @classmethod
    def _url_absolute_http(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("playlist_url must be an absolute http(s) URL")
        return value


class SegmentOut(BaseModel):
    sequence: int
    uri: str
    duration: str
    discontinuity: int
    byte_offset: Optional[int] = None
    byte_length: Optional[int] = None
    start: str
    end: str


class ReconcileResponse(BaseModel):
    playlist_url: str
    count: int
    duration: str
    segments: list[SegmentOut]


class LocateResponse(BaseModel):
    sequence: int
    uri: str
    start_at: str
    end_at: str
    offset: str


class VodResponse(BaseModel):
    playlist_url: str
    from_sec: str
    to_sec: str
    covered_start: str
    covered_end: str
    head_extra_sec: str
    tail_extra_sec: str
    segment_count: int
    playlist: str


class FetchRequestOut(BaseModel):
    uri: str
    range: Optional[str] = None


class SegmentFetchOut(BaseModel):
    sequence: int
    request_index: int
    offset: Optional[int] = None
    length: Optional[int] = None


class FetchPlanResponse(BaseModel):
    playlist_url: str
    requests: list[FetchRequestOut]
    segments: list[SegmentFetchOut]


class ErrorDetail(BaseModel):
    snapshot_index: Optional[int] = None
    sequence: Optional[int] = None


class ErrorResponse(BaseModel):
    error: str
    detail: str
    snapshot_index: Optional[int] = None
    sequence: Optional[int] = None


app = FastAPI(
    title="Offline HLS Window Reconciliation API",
    version="1.0.0",
    description=(
        "Reconstruct a contiguous segment timeline from ordered HLS media "
        "playlist snapshots, locate segments by program time, export "
        "time ranges as VOD playlists, and plan the HTTP requests needed "
        "to fetch every segment — entirely offline."
    ),
)


def _error(code: str, detail: str, status: int = 422, **extra) -> JSONResponse:
    body: dict = {"error": code, "detail": detail}
    body.update({key: value for key, value in extra.items() if value is not None})
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(LocateError)
async def locate_error_handler(_request: Request, exc: LocateError) -> JSONResponse:
    return _error(
        exc.code,
        exc.message,
        status=exc.status,
        snapshot_index=exc.snapshot_index,
        sequence=exc.sequence,
        sequences=exc.sequences,
    )


@app.exception_handler(ReconcileError)
async def reconcile_error_handler(_request: Request, exc: ReconcileError) -> JSONResponse:
    return _error(
        exc.code,
        exc.message,
        snapshot_index=exc.snapshot_index,
        sequence=exc.sequence,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    safe_errors = [
        {
            "loc": [str(part) for part in error.get("loc", [])],
            "msg": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_request", "detail": safe_errors},
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/reconcile", response_model=ReconcileResponse)
async def reconcile_endpoint(payload: ReconcileRequest) -> ReconcileResponse:
    timeline = reconcile(payload.playlist_url, payload.snapshots)

    segments = [
        SegmentOut(
            sequence=seg.sequence,
            uri=seg.uri,
            duration=format(seg.duration, "f"),
            discontinuity=seg.discontinuity,
            byte_offset=seg.byte_offset,
            byte_length=seg.byte_length,
            start=format(seg.start, "f"),
            end=format(seg.end, "f"),
        )
        for seg in timeline
    ]
    total = timeline[-1].end if timeline else Decimal("0")
    return ReconcileResponse(
        playlist_url=payload.playlist_url,
        count=len(segments),
        duration=format(total, "f"),
        segments=segments,
    )


@app.post("/v1/locate", response_model=LocateResponse)
async def locate_endpoint(payload: LocateRequest) -> LocateResponse:
    found = locate(payload.playlist_url, payload.snapshots, payload.target_at)
    return LocateResponse(
        sequence=found.sequence,
        uri=found.uri,
        start_at=format_program_time(found.start_at),
        end_at=format_program_time(found.end_at),
        offset=format(found.offset, "f"),
    )


@app.post("/v1/vod", response_model=VodResponse)
async def vod_endpoint(payload: VodRequest) -> VodResponse:
    window = export_vod(
        payload.playlist_url,
        payload.snapshots,
        payload.from_sec,
        payload.to_sec,
    )
    return VodResponse(
        playlist_url=payload.playlist_url,
        from_sec=format(window.from_sec, "f"),
        to_sec=format(window.to_sec, "f"),
        covered_start=format(window.covered_start, "f"),
        covered_end=format(window.covered_end, "f"),
        head_extra_sec=format(window.head_extra, "f"),
        tail_extra_sec=format(window.tail_extra, "f"),
        segment_count=window.segment_count,
        playlist=window.playlist,
    )


@app.post("/v1/fetch-plan", response_model=FetchPlanResponse)
async def fetch_plan_endpoint(payload: FetchPlanRequest) -> FetchPlanResponse:
    plan = build_fetch_plan(payload.playlist_url, payload.snapshots)
    return FetchPlanResponse(
        playlist_url=payload.playlist_url,
        requests=[
            FetchRequestOut(uri=request.uri, range=request.range_header)
            for request in plan.requests
        ],
        segments=[
            SegmentFetchOut(
                sequence=item.sequence,
                request_index=item.request_index,
                offset=item.offset,
                length=item.length,
            )
            for item in plan.segments
        ],
    )
