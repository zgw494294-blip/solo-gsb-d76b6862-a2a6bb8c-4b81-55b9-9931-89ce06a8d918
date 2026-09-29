"""Build an offline media fetch plan for the reconciled live window.

The fetch plan reuses the exact same merge rules as ``/v1/reconcile`` and
then turns the merged timeline into a minimal list of HTTP resource
requests plus a per-segment mapping into those requests:

* a URI referenced by at least one *full-resource* segment (no
  ``EXT-X-BYTERANGE``) is fetched whole, exactly once; that single response
  also covers every byte-range fragment of the same URI;
* a URI referenced only through byte ranges is fetched through merged
  ``Range: bytes=start-end`` requests: its fragment intervals are sorted by
  offset and overlapping or adjacent intervals are coalesced, one request
  per coalesced interval;
* requests are ordered by the sequence number of the first segment that
  needs them.

Every segment maps to exactly one request. Byte-range fragments report
their offset *within the response body* (original offset minus the request
start) and their length; full-resource segments report ``None`` for both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .reconcile import (
    ReconcileError,
    reconcile,
    reject_forbidden_tags,
)


@dataclass(frozen=True)
class FetchRequest:
    """One planned HTTP GET: a whole resource or one merged byte range."""

    uri: str
    # None -> the whole resource; otherwise the inclusive [start, end] range.
    range_start: Optional[int]
    range_end: Optional[int]

    @property
    def range_header(self) -> Optional[str]:
        """Value for the ``Range`` header, e.g. ``bytes=0-149`` (or None)."""

        if self.range_start is None:
            return None
        return f"bytes={self.range_start}-{self.range_end}"


@dataclass(frozen=True)
class SegmentFetch:
    """Where one segment's media data lives inside a planned response."""

    sequence: int
    request_index: int
    # Byte offset within the response body and fragment length; both None
    # for full-resource segments (the whole response is the segment).
    offset: Optional[int]
    length: Optional[int]


@dataclass(frozen=True)
class FetchPlan:
    requests: list[FetchRequest]
    segments: list[SegmentFetch]


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Coalesce overlapping or adjacent inclusive ``[start, end]`` intervals."""

    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def build_fetch_plan(playlist_url: str, snapshots: Iterable[str]) -> FetchPlan:
    """Plan the HTTP requests needed to fetch every merged segment.

    All merge errors from :func:`reconcile` propagate unchanged; snapshots
    using ``EXT-X-KEY``/``EXT-X-MAP`` and segments resolving to non-http(s)
    URIs raise :class:`ReconcileError` (mapped to 422).
    """

    snapshot_list = list(snapshots)
    reject_forbidden_tags(snapshot_list)
    timeline = reconcile(playlist_url, snapshot_list)

    for seg in timeline:
        parsed = urlsplit(seg.uri)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ReconcileError(
                "unsupported_uri",
                (
                    f"segment at sequence {seg.sequence} resolves to a "
                    f"non-http(s) URI and cannot be fetched: {seg.uri!r}"
                ),
                sequence=seg.sequence,
            )

    # A URI with at least one full-resource segment is fetched whole, once;
    # that response also covers every byte-range fragment of the same URI.
    full_uris = {seg.uri for seg in timeline if seg.byte_length is None}

    # URIs referenced only through byte ranges: coalesce overlapping or
    # adjacent fragment intervals into one read range per merged interval.
    fragments: dict[str, list[tuple[int, int]]] = {}
    for seg in timeline:
        if seg.byte_length is None or seg.uri in full_uris:
            continue
        fragments.setdefault(seg.uri, []).append(
            (seg.byte_offset, seg.byte_offset + seg.byte_length - 1)
        )
    merged_ranges: dict[str, list[tuple[int, int]]] = {
        uri: _merge_intervals(intervals) for uri, intervals in fragments.items()
    }

    def _containing_range(uri: str, offset: int, end: int) -> tuple[int, int]:
        for start, merged_end in merged_ranges[uri]:
            if start <= offset and end <= merged_end:
                return start, merged_end
        raise ReconcileError(  # defensive: every fragment was merged above
            "invalid_request",
            f"no merged byte range contains {uri} [{offset}, {end}]",
        )

    requests: list[FetchRequest] = []
    request_index: dict[tuple, int] = {}
    segment_fetches: list[SegmentFetch] = []

    # Timeline order is sequence order, so requests are created in the
    # order of the first segment that involves them.
    for seg in timeline:
        if seg.uri in full_uris:
            key = ("full", seg.uri)
            index = request_index.get(key)
            if index is None:
                index = len(requests)
                request_index[key] = index
                requests.append(FetchRequest(uri=seg.uri, range_start=None, range_end=None))
            if seg.byte_length is None:
                segment_fetches.append(SegmentFetch(seg.sequence, index, None, None))
            else:
                # The whole-resource response starts at offset 0, so the
                # fragment sits at its original offset within the body.
                segment_fetches.append(
                    SegmentFetch(seg.sequence, index, seg.byte_offset, seg.byte_length)
                )
            continue

        end = seg.byte_offset + seg.byte_length - 1
        start, merged_end = _containing_range(seg.uri, seg.byte_offset, end)
        key = ("range", seg.uri, start, merged_end)
        index = request_index.get(key)
        if index is None:
            index = len(requests)
            request_index[key] = index
            requests.append(
                FetchRequest(uri=seg.uri, range_start=start, range_end=merged_end)
            )
        segment_fetches.append(
            SegmentFetch(seg.sequence, index, seg.byte_offset - start, seg.byte_length)
        )

    return FetchPlan(requests=requests, segments=segment_fetches)
