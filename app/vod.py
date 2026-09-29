"""Export a reconciled live window as a VOD media playlist.

The VOD export reuses the exact same merge rules as ``/v1/reconcile`` and
then cuts a half-open decimal-second window ``[from_sec, to_sec)`` out of
the merged timeline. Every segment intersecting the window is emitted (the
first and last may extend beyond the requested bounds); the surplus carried
at both ends is reported back to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Iterable, Optional, Sequence

from .reconcile import (
    ReconcileError,
    ReconciledSegment,
    reconcile,
    reject_forbidden_tags,
)


@dataclass(frozen=True)
class VodWindow:
    """A rendered VOD playlist plus its exact placement on the timeline."""

    from_sec: Decimal
    to_sec: Decimal
    covered_start: Decimal
    covered_end: Decimal
    head_extra: Decimal
    tail_extra: Decimal
    segment_count: int
    playlist: str


def _parse_bound(raw: str, field: str) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ReconcileError(
            "invalid_range",
            f"{field} must be a decimal number of seconds, got {raw!r}",
        ) from exc
    if not value.is_finite() or value < 0:
        raise ReconcileError(
            "invalid_range",
            f"{field} must be a finite, non-negative decimal number of seconds, got {raw!r}",
        )
    return value


def render_vod_playlist(segments: Sequence[ReconciledSegment]) -> str:
    """Render selected segments as a VOD media playlist.

    EXTINF keeps the exact decimal precision seen in the source snapshots;
    byte ranges are always written in explicit ``length@offset`` form; a
    DISCONTINUITY tag is inserted whenever the discontinuity counter changes
    between two adjacent emitted segments.
    """

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:4",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    longest = max(seg.duration for seg in segments)
    target = int(longest.to_integral_value(rounding=ROUND_CEILING))
    lines.append(f"#EXT-X-TARGETDURATION:{target}")

    previous_discontinuity: Optional[int] = None
    for seg in segments:
        if (
            previous_discontinuity is not None
            and seg.discontinuity != previous_discontinuity
        ):
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{format(seg.duration, 'f')},")
        if seg.byte_length is not None:
            lines.append(f"#EXT-X-BYTERANGE:{seg.byte_length}@{seg.byte_offset}")
        lines.append(seg.uri)
        previous_discontinuity = seg.discontinuity

    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def export_vod(
    playlist_url: str,
    snapshots: Iterable[str],
    from_raw: str,
    to_raw: str,
) -> VodWindow:
    """Build a VOD playlist covering the half-open window ``[from_sec, to_sec)``.

    All merge errors from :func:`reconcile` propagate unchanged; invalid
    windows and unsupported tags raise :class:`ReconcileError` (mapped to 422).
    """

    snapshot_list = list(snapshots)
    reject_forbidden_tags(snapshot_list)

    from_sec = _parse_bound(from_raw, "from_sec")
    to_sec = _parse_bound(to_raw, "to_sec")
    if from_sec >= to_sec:
        raise ReconcileError(
            "invalid_range",
            (
                f"from_sec ({format(from_sec, 'f')}) must be smaller than "
                f"to_sec ({format(to_sec, 'f')})"
            ),
        )

    timeline = reconcile(playlist_url, snapshot_list)
    total = timeline[-1].end if timeline else Decimal("0")
    if to_sec > total:
        raise ReconcileError(
            "invalid_range",
            (
                f"to_sec ({format(to_sec, 'f')}) exceeds the total duration "
                f"{format(total, 'f')} of the merged window"
            ),
        )

    # Half-open intersection: a segment is selected iff its [start, end)
    # overlaps [from_sec, to_sec). Contiguous coverage makes the selection a
    # single run: the intersecting first/last segments plus everything between.
    selected = [seg for seg in timeline if seg.start < to_sec and seg.end > from_sec]
    if not selected:
        # Defensive: 0 <= from_sec < to_sec <= total guarantees a hit.
        raise ReconcileError("invalid_range", "window does not intersect any segment")

    first, last = selected[0], selected[-1]
    return VodWindow(
        from_sec=from_sec,
        to_sec=to_sec,
        covered_start=first.start,
        covered_end=last.end,
        head_extra=from_sec - first.start,
        tail_extra=last.end - to_sec,
        segment_count=len(selected),
        playlist=render_vod_playlist(selected),
    )
