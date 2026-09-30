"""Locate a segment by absolute program time.

Program time enters a playlist through ``#EXT-X-PROGRAM-DATE-TIME`` (PDT)
tags: every such tag anchors the start of the segment immediately following
it. All times are normalized to UTC and represented internally as *exact*
decimal POSIX timestamps (seconds since 1970-01-01T00:00:00Z), so no binary
floating point is ever involved in the date arithmetic.

Program time is only propagated inside one discontinuity region: a segment's
UTC start is derived from the nearest anchor within the same contiguous
sequence run sharing a discontinuity counter by adding/subtracting the exact
decimal ``EXTINF`` durations. Time is never carried across an
``#EXT-X-DISCONTINUITY`` boundary.

A discontinuity counter value is *not* a unique region identifier: across
non-overlapping snapshots the same counter may recur in a later, separate
sequence run. Anchors are therefore attributed to the contiguous run that
actually contains their sequence, never keyed by the counter value alone, so
two runs that happen to reuse a counter are validated and extrapolated
independently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Iterable, Optional

from .hls import PlaylistParseError, Segment, parse_playlist
from .reconcile import ReconcileError, reconcile

_EPOCH = date(1970, 1, 1)

# Strict RFC 3339 date-time: date, T/t separator, hh:mm:ss with optional
# dot-seconds, and a mandatory offset (Z/z or +/-hh:mm). Naive timestamps
# are rejected on purpose.
_RFC3339_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt]"
    r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"(Z|z|[+-]\d{2}:\d{2})$"
)


class LocateError(ReconcileError):
    """A locate-specific failure. Carries an HTTP status (404 or 422)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 422,
        snapshot_index: Optional[int] = None,
        sequence: Optional[int] = None,
        sequences: Optional[list[int]] = None,
        track: Optional[str] = None,
    ) -> None:
        super().__init__(
            code,
            message,
            snapshot_index=snapshot_index,
            sequence=sequence,
            track=track,
        )
        self.status = status
        self.sequences = sequences


def parse_program_time(raw: str) -> Decimal:
    """Parse a timezone-aware RFC 3339 string into an exact UTC POSIX timestamp."""

    match = _RFC3339_RE.match(raw.strip())
    if match is None:
        raise PlaylistParseError(
            "EXT-X-PROGRAM-DATE-TIME must be an RFC 3339 date-time with a "
            f"timezone offset, got {raw!r}"
        )

    year, month, day, hour, minute, second, fraction, offset = match.groups()
    try:
        moment = datetime.combine(
            date(int(year), int(month), int(day)),
            time(int(hour), int(minute), int(second)),
        )
    except ValueError as exc:
        raise PlaylistParseError(f"invalid EXT-X-PROGRAM-DATE-TIME {raw!r}: {exc}") from exc

    if offset in ("Z", "z"):
        offset_seconds = 0
    else:
        sign = 1 if offset[0] == "+" else -1
        offset_seconds = sign * (int(offset[1:3]) * 3600 + int(offset[4:6]) * 60)

    days = (moment.date() - _EPOCH).days
    seconds_in_day = moment.hour * 3600 + moment.minute * 60 + moment.second
    fraction_value = Decimal(f"0.{fraction}") if fraction else Decimal(0)

    # Local wall time minus its offset is the UTC instant.
    return Decimal(days * 86400 + seconds_in_day - offset_seconds) + fraction_value


def format_program_time(timestamp: Decimal) -> str:
    """Format an exact UTC POSIX timestamp as ``YYYY-MM-DDTHH:MM:SS[.frac]Z``."""

    # Floor toward -infinity so any negative timestamp keeps 0 <= frac < 1.
    total_seconds = int(timestamp.to_integral_value(rounding=ROUND_FLOOR))
    fraction = timestamp - Decimal(total_seconds)
    days, seconds_of_day = divmod(total_seconds, 86400)
    day = _EPOCH + timedelta(days=days)
    hours, remainder = divmod(seconds_of_day, 3600)
    minutes, seconds = divmod(remainder, 60)

    text = f"{day.year:04d}-{day.month:02d}-{day.day:02d}T"
    text += f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    if fraction:
        text += "." + format(fraction, "f").split(".", 1)[1].rstrip("0")
    return text + "Z"


@dataclass(frozen=True)
class Anchor:
    """A PDT anchor: the UTC start of segment ``sequence`` in one snapshot."""

    snapshot_index: int
    sequence: int
    utc_start: Decimal


@dataclass(frozen=True)
class LocatedSegment:
    sequence: int
    uri: str
    start_at: Decimal
    end_at: Decimal
    offset: Decimal


@dataclass(frozen=True)
class KnownProgramSegment:
    """A segment whose half-open UTC program-time range is known.

    ``run_index`` identifies the contiguous discontinuity run the segment
    belongs to (its position among the runs of *this* timeline); a later,
    separate run may reuse the same discontinuity counter and still gets a
    different index.
    """

    sequence: int
    uri: str
    start_at: Decimal
    end_at: Decimal
    run_index: int


@dataclass
class _Interval:
    """One contiguous run of segments sharing a discontinuity counter.

    Counter values may recur in separate runs across snapshots, so an interval
    is identified by its position in the timeline, not by ``discontinuity``.
    """

    discontinuity: int
    segments: list[Segment]
    # Exact duration of [first segment start, segment start), by sequence.
    prefix: dict[int, Decimal]
    # PDT anchors whose sequence belongs to this run.
    anchors: list[Anchor]


def _parse_anchors(
    playlist_url: str, snapshots: Iterable[str]
) -> list[Anchor]:
    """Parse every snapshot and collect its PDT anchors (in snapshot order)."""

    anchors: list[Anchor] = []
    for index, raw in enumerate(snapshots):
        try:
            segments = parse_playlist(raw, playlist_url)
        except PlaylistParseError as exc:
            raise LocateError(
                "invalid_playlist",
                f"snapshot {index} is invalid: {exc}",
                snapshot_index=index,
            ) from exc

        for seg in segments:
            for raw_pdt in seg.program_date_times:
                try:
                    utc_start = parse_program_time(raw_pdt)
                except PlaylistParseError as exc:
                    raise LocateError(
                        "invalid_playlist",
                        f"snapshot {index} is invalid: {exc}",
                        snapshot_index=index,
                        sequence=seg.sequence,
                    ) from exc
                anchors.append(
                    Anchor(
                        snapshot_index=index,
                        sequence=seg.sequence,
                        utc_start=utc_start,
                    )
                )
    return anchors


def _build_intervals(timeline) -> list[_Interval]:
    """Split the reconciled timeline into contiguous runs by counter change.

    A new run starts whenever the discontinuity counter changes between two
    adjacent segments. Runs that merely reuse an earlier counter value stay
    distinct: they are separate list entries, never merged by counter value.
    """

    intervals: list[_Interval] = []
    current: Optional[_Interval] = None

    for seg in timeline:
        if current is None or seg.discontinuity != current.discontinuity:
            current = _Interval(
                discontinuity=seg.discontinuity,
                segments=[],
                prefix={},
                anchors=[],
            )
            intervals.append(current)
        current.segments.append(seg)

    for interval in intervals:
        cursor = Decimal("0")
        for seg in interval.segments:
            interval.prefix[seg.sequence] = cursor
            cursor += seg.duration

    return intervals


def resolve_program_segments(
    playlist_url: str,
    snapshots: Iterable[str],
    timeline,
) -> list[KnownProgramSegment]:
    """Resolve the known UTC half-open range of every anchored segment.

    The timeline must already be reconciled from ``playlist_url`` /
    ``snapshots`` with :func:`reconcile` (the caller does so that the usual
    merge errors surface in their original form). Anchors are collected,
    attributed to the contiguous sequence runs that actually contain their
    sequence numbers (never keyed by discontinuity counter alone) and
    validated exactly as for ``/v1/locate``:

    * cross-snapshot anchors on the same sequence must denote the same UTC
      instant after normalization;
    * multiple anchors inside one run must agree with decimal-duration
      extrapolation from the first anchor.

    Only segments of runs carrying at least one anchor are returned; an
    unanchored run has no known time and is never filled in from another run
    or another track. The result is ordered by UTC start (and, on ties, by
    sequence number).
    """

    anchors = _parse_anchors(playlist_url, snapshots)
    intervals = _build_intervals(timeline)

    # Attribute every anchor to the contiguous sequence run that contains it.
    # Sequence is unique across the merged timeline, so this never merges two
    # separate runs merely because they reuse the same discontinuity counter.
    interval_by_sequence: dict[int, _Interval] = {}
    for interval in intervals:
        for seg in interval.segments:
            interval_by_sequence[seg.sequence] = interval

    for anchor in anchors:
        interval = interval_by_sequence.get(anchor.sequence)
        if interval is None:
            # Defensive: reconcile() guarantees contiguous coverage.
            raise LocateError(
                "program_time_conflict",
                f"anchor at sequence {anchor.sequence} is outside the merged timeline",
                snapshot_index=anchor.snapshot_index,
                sequence=anchor.sequence,
            )
        interval.anchors.append(anchor)

    known: list[KnownProgramSegment] = []
    # Anchors are validated independently within every contiguous run:
    # cross-snapshot anchors on the same sequence must denote the same UTC
    # instant, and multiple anchors inside one run must agree with
    # extrapolation from the first. A run with no anchor has no known time.
    for run_index, interval in enumerate(intervals):
        if not interval.anchors:
            continue
        interval.anchors.sort(key=lambda item: item.sequence)

        first: Optional[Anchor] = None
        seen_sequences: dict[int, Anchor] = {}
        for anchor in interval.anchors:
            previous_same = seen_sequences.get(anchor.sequence)
            if previous_same is not None:
                if anchor.utc_start != previous_same.utc_start:
                    raise LocateError(
                        "program_time_conflict",
                        (
                            f"snapshot {anchor.snapshot_index} program-date-time at "
                            f"sequence {anchor.sequence} disagrees with the anchor at "
                            f"snapshot {previous_same.snapshot_index} for the same "
                            "sequence after UTC normalization"
                        ),
                        snapshot_index=anchor.snapshot_index,
                        sequence=anchor.sequence,
                    )
                continue
            seen_sequences[anchor.sequence] = anchor

            if first is None:
                first = anchor
                continue

            expected = (
                first.utc_start
                + interval.prefix[anchor.sequence]
                - interval.prefix[first.sequence]
            )
            if anchor.utc_start != expected:
                raise LocateError(
                    "program_time_conflict",
                    (
                        f"snapshot {anchor.snapshot_index} program-date-time at "
                        f"sequence {anchor.sequence} ({anchor.utc_start}) is "
                        f"inconsistent with extrapolation from snapshot "
                        f"{first.snapshot_index} sequence {first.sequence} within "
                        f"discontinuity region {interval.discontinuity}; "
                        f"expected {expected}"
                    ),
                    snapshot_index=anchor.snapshot_index,
                    sequence=anchor.sequence,
                )

        # Extrapolate inside this run only; time never crosses a break into
        # the neighboring run, even when both runs reuse a counter.
        anchor = min(interval.anchors, key=lambda item: item.sequence)
        anchor_offset = interval.prefix[anchor.sequence]
        for seg in interval.segments:
            utc_start = (
                anchor.utc_start + interval.prefix[seg.sequence] - anchor_offset
            )
            known.append(
                KnownProgramSegment(
                    sequence=seg.sequence,
                    uri=seg.uri,
                    start_at=utc_start,
                    end_at=utc_start + seg.duration,
                    run_index=run_index,
                )
            )

    known.sort(key=lambda item: (item.start_at, item.sequence))
    return known


def locate(
    playlist_url: str,
    snapshots: Iterable[str],
    target_raw: str,
) -> LocatedSegment:
    """Locate the segment whose half-open UTC range contains ``target_raw``.

    ``target_raw`` must be a timezone-aware RFC 3339 date-time.

    Raises :class:`LocateError` (mapped to 404/422) on every failure.
    """

    # The same merge rules as /v1/reconcile apply (rollback, gap, overlap
    # conflicts on URI/duration/byte range/discontinuity).
    timeline = reconcile(playlist_url, snapshots)

    try:
        target = parse_program_time(target_raw)
    except PlaylistParseError as exc:
        raise LocateError("invalid_request", str(exc), status=422) from exc

    known = resolve_program_segments(playlist_url, snapshots, timeline)

    # Half-open [start, end) lookup over every anchored region.
    hits = [
        item for item in known if item.start_at <= target < item.end_at
    ]

    if not hits:
        raise LocateError(
            "program_time_uncovered",
            "target time is not covered by any program-date-time anchored region",
            status=404,
        )

    if len(hits) > 1:
        # Ranges inside one contiguous run are disjoint, so multiple hits can
        # only come from distinct anchored runs (they may even reuse the same
        # discontinuity counter on a later, separate sequence run).
        sequences = sorted(item.sequence for item in hits)
        raise LocateError(
            "ambiguous_program_time",
            (
                "target time matches segments in multiple discontinuity regions; "
                f"candidate sequences: {sequences}"
            ),
            status=422,
            sequences=sequences,
        )

    item = hits[0]
    return LocatedSegment(
        sequence=item.sequence,
        uri=item.uri,
        start_at=item.start_at,
        end_at=item.end_at,
        offset=target - item.start_at,
    )
