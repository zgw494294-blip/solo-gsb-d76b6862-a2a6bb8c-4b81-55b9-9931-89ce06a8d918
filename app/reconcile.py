"""Reconcile an ordered series of HLS playlist snapshots into one timeline."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from .hls import PlaylistParseError, Segment, parse_playlist


class ReconcileError(Exception):
    """A 422-class problem, carrying the offending snapshot index and sequence."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        snapshot_index: Optional[int] = None,
        sequence: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.snapshot_index = snapshot_index
        self.sequence = sequence


@dataclass(frozen=True)
class ReconciledSegment:
    sequence: int
    uri: str
    duration: Decimal
    discontinuity: int
    byte_offset: Optional[int]
    byte_length: Optional[int]
    start: Decimal
    end: Decimal


# Encryption keys and init-segment maps cannot be carried into derived
# artifacts (VOD playlists, fetch plans), so snapshots carrying them are
# rejected before merging.
FORBIDDEN_TAGS = ("#EXT-X-KEY", "#EXT-X-MAP")


def reject_forbidden_tags(snapshots: Sequence[str]) -> None:
    """Raise ``unsupported_tag`` if any snapshot uses EXT-X-KEY / EXT-X-MAP."""

    for index, raw in enumerate(snapshots):
        for line in raw.splitlines():
            token = line.strip()
            if not token.startswith("#"):
                continue
            tag = token.split(":", 1)[0]
            if tag in FORBIDDEN_TAGS:
                raise ReconcileError(
                    "unsupported_tag",
                    f"snapshot {index} uses {tag}, which is not supported",
                    snapshot_index=index,
                )


def _segments_equal(existing: Segment, candidate: Segment) -> bool:
    return (
        existing.uri == candidate.uri
        and existing.duration == candidate.duration
        and existing.byte_range == candidate.byte_range
        and existing.discontinuity == candidate.discontinuity
    )


def reconcile(playlist_url: str, snapshots: Iterable[str]) -> list[ReconciledSegment]:
    """Merge ordered playlist text snapshots by media sequence number.

    Rules enforced (all violations raise :class:`ReconcileError`, mapped to 422):

    * a snapshot's starting sequence number may never be lower than the
      starting sequence number of the snapshot before it;
    * a snapshot that does not overlap merged content must begin at
      ``last_sequence + 1``;
    * every overlapping sequence number must agree on URI, decimal duration,
      resolved byte range and discontinuity counter.
    """

    merged: dict[int, Segment] = {}
    window_start: Optional[int] = None
    previous_start: Optional[int] = None
    last_sequence: Optional[int] = None

    for index, raw in enumerate(snapshots):
        try:
            segments = parse_playlist(raw, playlist_url)
        except PlaylistParseError as exc:
            raise ReconcileError(
                "invalid_playlist",
                f"snapshot {index} is invalid: {exc}",
                snapshot_index=index,
            ) from exc

        if not segments:
            raise ReconcileError(
                "empty_playlist",
                f"snapshot {index} contains no segments",
                snapshot_index=index,
            )
        if len(segments) > 100:
            raise ReconcileError(
                "too_many_segments",
                f"snapshot {index} has {len(segments)} segments; at most 100 are allowed",
                snapshot_index=index,
            )

        start = segments[0].sequence
        end = segments[-1].sequence

        if previous_start is None:
            window_start = start
        elif start < previous_start:
            raise ReconcileError(
                "sequence_rollback",
                (
                    f"snapshot {index} starts at sequence {start}, which is lower "
                    f"than the starting sequence {previous_start} of an earlier snapshot"
                ),
                snapshot_index=index,
                sequence=start,
            )

        if last_sequence is not None and start > last_sequence + 1:
            raise ReconcileError(
                "sequence_gap",
                (
                    f"snapshot {index} starts at sequence {start}, leaving a gap "
                    f"after the last merged sequence {last_sequence}"
                ),
                snapshot_index=index,
                sequence=start,
            )

        for seg in segments:
            existing = merged.get(seg.sequence)
            if existing is None:
                merged[seg.sequence] = seg
                continue
            if not _segments_equal(existing, seg):
                raise ReconcileError(
                    "overlap_conflict",
                    (
                        f"snapshot {index} conflicts with an earlier snapshot at "
                        f"sequence {seg.sequence}"
                    ),
                    snapshot_index=index,
                    sequence=seg.sequence,
                )

        if last_sequence is None or end > last_sequence:
            last_sequence = end
        previous_start = start

    if not merged:
        raise ReconcileError("no_snapshots", "no snapshots were provided")

    timeline: list[ReconciledSegment] = []
    cursor = Decimal("0")
    for sequence in range(window_start, last_sequence + 1):
        seg = merged.get(sequence)
        if seg is None:
            # Defensive: the gap rule above guarantees contiguous coverage.
            raise ReconcileError(
                "sequence_gap",
                f"missing segment at sequence {sequence}",
                sequence=sequence,
            )
        start = cursor
        cursor += seg.duration
        timeline.append(
            ReconciledSegment(
                sequence=seg.sequence,
                uri=seg.uri,
                duration=seg.duration,
                discontinuity=seg.discontinuity,
                byte_offset=seg.byte_range.offset if seg.byte_range else None,
                byte_length=seg.byte_range.length if seg.byte_range else None,
                start=start,
                end=cursor,
            )
        )

    return timeline
