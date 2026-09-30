"""Compute the common program-time coverage of two live HLS tracks.

Each track is an independent ``playlist_url`` plus its own time-ordered
``snapshots``. Both tracks are first reconciled and resolved with exactly the
rules already used by ``/v1/reconcile`` and ``/v1/locate``:

* the same cross-snapshot merge rules (sequence rollback, gaps, overlap
  conflicts on URI/duration/byte range/discontinuity);
* the same ``#EXT-X-PROGRAM-DATE-TIME`` anchor attribution (the anchor marks
  the start of the segment immediately following it, and belongs to the
  contiguous sequence run that actually contains that sequence, never to a
  run keyed by discontinuity counter alone);
* the same anchor consistency checks (cross-snapshot anchors on one sequence
  must agree after UTC normalization; several anchors in one run must agree
  with exact decimal ``EXTINF`` extrapolation);
* the same UTC decimal timekeeping (all instants are exact decimal POSIX
  seconds, no binary floating point);
* time is never propagated across an ``#EXT-X-DISCONTINUITY`` boundary: an
  unanchored run has no known time and can never be completed from another
  run of the same track or from the other track.

The common schedule is the intersection of the two tracks' *known* UTC
half-open segment ranges ``[start_at, end_at)``. Segment boundaries need not
line up between the tracks, so each intersection is cut into one pair per
actual overlap of two concrete segments; ranges that merely touch (one's end
equals the other's start) produce nothing because the intervals are
half-open.

If the known time of one track overlaps itself across *different*
discontinuity runs (program times on the two sides of a break coincide, or a
reused-counter run reuses the same clock), a common instant would map to
several segments of that track and no unique pairing exists; the request is
rejected with 422 before the tracks are compared. Ranges inside one run are
already disjoint, so same-run overlaps cannot occur.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence

from .locate import KnownProgramSegment, LocateError, resolve_program_segments
from .reconcile import ReconcileError, reconcile


@dataclass(frozen=True)
class OverlapPair:
    """One common half-open program interval and its two source segments."""

    sequence_a: int
    sequence_b: int
    start_at: Decimal
    end_at: Decimal
    # Exact decimal offset of start_at inside each track's segment.
    offset_a: Decimal
    offset_b: Decimal


@dataclass(frozen=True)
class TrackOverlap:
    playlist_url: str
    pairs: list[OverlapPair]


def _resolve_track(
    track: str,
    playlist_url: str,
    snapshots: Iterable[str],
) -> list[KnownProgramSegment]:
    """Reconcile one track and resolve its anchored UTC segment ranges.

    Every failure is tagged with the track name (``"a"`` / ``"b"``) so the
    caller can report which side of the request caused it.
    """

    try:
        timeline = reconcile(playlist_url, snapshots)
    except ReconcileError as exc:
        # Covers LocateError as well: it subclasses ReconcileError, though
        # reconcile() itself only raises plain ReconcileError.
        exc.track = track
        raise

    try:
        return resolve_program_segments(playlist_url, snapshots, timeline)
    except LocateError as exc:
        exc.track = track
        raise


def _reject_self_overlap(track: str, known: Sequence[KnownProgramSegment]) -> None:
    """Reject runs of one track whose known UTC ranges overlap each other.

    ``known`` is ordered by UTC start. Segments of one contiguous run are
    disjoint by construction, so any live overlap (``start < end``) while
    sweeping can only come from different runs — the case that makes the
    segment pairing ambiguous. Ranges that merely meet at one instant
    (``start == end``) do not overlap because every range is half-open.
    """

    active: list[KnownProgramSegment] = []
    for seg in known:
        # Drop ranges whose half-open life ends at or before this start.
        active = [other for other in active if other.end_at > seg.start_at]
        if any(other.run_index != seg.run_index for other in active):
            sequences = sorted(
                {other.sequence for other in active if other.run_index != seg.run_index}
                | {seg.sequence}
            )
            raise LocateError(
                "ambiguous_program_time",
                (
                    f"track {track!r} has overlapping known program time in "
                    f"different discontinuity regions; candidate sequences: "
                    f"{sequences}"
                ),
                sequences=sequences,
                track=track,
            )
        active.append(seg)


def _intersect(
    known_a: Sequence[KnownProgramSegment],
    known_b: Sequence[KnownProgramSegment],
) -> list[OverlapPair]:
    """Pair up all positive-length overlaps of the two tracks' known ranges.

    Standard half-open interval sweep with two pointers: whenever the current
    ranges overlap with positive length, emit one pair for that concrete
    segment combination, then advance whichever pointer ends earliest (both
    when they end together). This splits the common coverage at *both*
    tracks' boundaries even when the two segmentations differ.
    """

    pairs: list[OverlapPair] = []
    i = j = 0
    while i < len(known_a) and j < len(known_b):
        seg_a = known_a[i]
        seg_b = known_b[j]
        start = max(seg_a.start_at, seg_b.start_at)
        end = min(seg_a.end_at, seg_b.end_at)
        if start < end:
            pairs.append(
                OverlapPair(
                    sequence_a=seg_a.sequence,
                    sequence_b=seg_b.sequence,
                    start_at=start,
                    end_at=end,
                    offset_a=start - seg_a.start_at,
                    offset_b=start - seg_b.start_at,
                )
            )
        if seg_a.end_at < seg_b.end_at:
            i += 1
        elif seg_b.end_at < seg_a.end_at:
            j += 1
        else:
            i += 1
            j += 1
    return pairs


def overlap_tracks(
    playlist_url_a: str,
    snapshots_a: Iterable[str],
    playlist_url_b: str,
    snapshots_b: Iterable[str],
) -> TrackOverlap:
    """Build the common program-time intervals of two independent tracks.

    Merge and anchor errors propagate with their usual codes/status, tagged
    with the offending track; self-overlap of one track's anchored regions is
    a 422 ``ambiguous_program_time``. With no common coverage the result
    simply carries an empty pair list.
    """

    known_a = _resolve_track("a", playlist_url_a, snapshots_a)
    known_b = _resolve_track("b", playlist_url_b, snapshots_b)

    _reject_self_overlap("a", known_a)
    _reject_self_overlap("b", known_b)

    pairs = _intersect(known_a, known_b)
    # Canonical output order: by UTC start, then by the two sequence numbers.
    pairs.sort(key=lambda pair: (pair.start_at, pair.sequence_a, pair.sequence_b))
    return TrackOverlap(playlist_url=playlist_url_a, pairs=pairs)
