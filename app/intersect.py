"""Intersect the known program-time coverage of two live media playlists.

Each stream is processed independently with the exact same rules as
``/v1/locate``: snapshots are merged by media sequence,
``#EXT-X-PROGRAM-DATE-TIME`` anchors are validated and normalized to UTC, and
program time is extrapolated with exact decimal ``EXTINF`` durations inside
one contiguous discontinuity run only. Unanchored runs have no known time;
their gaps can never be filled from another time range or from the other
stream.

Only segments whose program time is known in *both* streams participate.
Every such segment is a half-open UTC range ``[start_at, end_at)``; the
common timeline is the pairwise intersection of those ranges. When the two
streams cut at different boundaries, each actually overlapping pair of
segments is emitted separately (boundaries on both sides are split), and
zero-length intersections are dropped.

If the known-time ranges of *different discontinuity runs within one stream*
overlap in UTC, a program instant does not identify a unique segment of that
stream, so no unique pairing exists and the request fails with 422.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from .locate import LocateError, TimedSegment, anchored_segments
from .reconcile import ReconcileError


class IntersectError(ReconcileError):
    """A failure while intersecting two streams; always mapped to HTTP 422.

    ``stream`` ("a"/"b") identifies which input caused the error when it is
    attributable to one side; ``sequences`` lists ambiguous sequences.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stream: Optional[str] = None,
        snapshot_index: Optional[int] = None,
        sequence: Optional[int] = None,
        sequences: Optional[list[int]] = None,
    ) -> None:
        super().__init__(
            code,
            message,
            snapshot_index=snapshot_index,
            sequence=sequence,
        )
        self.stream = stream
        self.sequences = sequences


@dataclass(frozen=True)
class CommonInterval:
    """One half-open UTC range covered by exactly one segment of each stream."""

    sequence_a: int
    sequence_b: int
    start_at: Decimal
    end_at: Decimal
    # Exact seconds from each segment's own start to the common range start.
    offset_a: Decimal
    offset_b: Decimal


def _timed_segments(
    stream: str, playlist_url: str, snapshots: Iterable[str]
) -> list[TimedSegment]:
    """Resolve one stream's anchored segments, tagging errors with the side."""

    try:
        return anchored_segments(playlist_url, snapshots)
    except LocateError as exc:
        raise IntersectError(
            exc.code,
            exc.message,
            stream=stream,
            snapshot_index=exc.snapshot_index,
            sequence=exc.sequence,
            sequences=exc.sequences,
        ) from exc
    except ReconcileError as exc:
        raise IntersectError(
            exc.code,
            exc.message,
            stream=stream,
            snapshot_index=exc.snapshot_index,
            sequence=exc.sequence,
        ) from exc


def _assert_self_disjoint(stream: str, segments: list[TimedSegment]) -> None:
    """Reject overlap between known-time ranges of different runs in one stream.

    Ranges inside one contiguous run are disjoint by construction (they are
    adjacent durations on one extrapolated axis). Two separate anchored runs
    may nevertheless cover the same UTC instants after a discontinuity; then
    a common instant would match more than one segment of this stream and no
    unique cross-stream pairing could exist.

    Comparison is restricted to distinct runs, so the at-most-100 segments
    per snapshot window stay cheap even when a run is long.
    """

    involved: set[int] = set()
    for i, earlier in enumerate(segments):
        for later in segments[i + 1 :]:
            if later.run_index == earlier.run_index:
                # Timeline order; different runs are never interleaved there.
                continue
            if earlier.start_at < later.end_at and later.start_at < earlier.end_at:
                involved.add(earlier.sequence)
                involved.add(later.sequence)

    if involved:
        sequences = sorted(involved)
        raise IntersectError(
            "ambiguous_program_time",
            (
                f"stream {stream!r} has program-time ranges from different "
                f"discontinuity regions overlapping at the same UTC instants; "
                f"a unique segment pairing cannot be determined; "
                f"candidate sequences: {sequences}"
            ),
            stream=stream,
            sequences=sequences,
        )


def _intersect(
    segments_a: list[TimedSegment], segments_b: list[TimedSegment]
) -> list[CommonInterval]:
    """Pairwise half-open intersection of UTC-sorted disjoint segment ranges."""

    ordered_a = sorted(segments_a, key=lambda seg: (seg.start_at, seg.end_at, seg.sequence))
    ordered_b = sorted(segments_b, key=lambda seg: (seg.start_at, seg.end_at, seg.sequence))

    result: list[CommonInterval] = []
    i = j = 0
    while i < len(ordered_a) and j < len(ordered_b):
        seg_a = ordered_a[i]
        seg_b = ordered_b[j]

        start = max(seg_a.start_at, seg_b.start_at)
        end = min(seg_a.end_at, seg_b.end_at)
        if start < end:  # half-open: a zero-length intersection is not output
            result.append(
                CommonInterval(
                    sequence_a=seg_a.sequence,
                    sequence_b=seg_b.sequence,
                    start_at=start,
                    end_at=end,
                    offset_a=start - seg_a.start_at,
                    offset_b=start - seg_b.start_at,
                )
            )

        # Advance the side whose range ends first; ranges are half-open and
        # disjoint within each stream, so equal ends advance together.
        if seg_a.end_at < seg_b.end_at:
            i += 1
        elif seg_b.end_at < seg_a.end_at:
            j += 1
        else:
            i += 1
            j += 1
    return result


def intersect_streams(
    playlist_url_a: str,
    snapshots_a: Iterable[str],
    playlist_url_b: str,
    snapshots_b: Iterable[str],
) -> list[CommonInterval]:
    """Compute the common known program-time intervals of two streams.

    Both inputs go through the same merge / anchor-validation / UTC
    extrapolation pipeline as :func:`app.locate.locate`. Every failure is an
    :class:`IntersectError` (HTTP 422), tagged with ``stream`` ("a" or "b")
    when it originates from one side. Returns an empty list when the two
    streams have no common known-time coverage.
    """

    segments_a = _timed_segments("a", playlist_url_a, snapshots_a)
    segments_b = _timed_segments("b", playlist_url_b, snapshots_b)

    _assert_self_disjoint("a", segments_a)
    _assert_self_disjoint("b", segments_b)

    return _intersect(segments_a, segments_b)
