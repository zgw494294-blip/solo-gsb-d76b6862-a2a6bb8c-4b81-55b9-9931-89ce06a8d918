"""Offline HLS playlist parsing.

Only the subset required for window reconciliation is understood:

* ``#EXTM3U``
* ``#EXT-X-MEDIA-SEQUENCE``
* ``#EXT-X-DISCONTINUITY-SEQUENCE``
* ``#EXT-X-DISCONTINUITY``
* ``#EXTINF``
* ``#EXT-X-BYTERANGE``
* ``#EXT-X-PROGRAM-DATE-TIME``
* bare URI lines (media segments)

Every other tag / comment is ignored. The parser never performs any network
access; relative segment URIs are resolved against the playlist URL with the
standard-library URL joiner.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urljoin


class PlaylistParseError(ValueError):
    """Raised when a snapshot is not a parseable media playlist."""


@dataclass(frozen=True)
class ByteRange:
    """Resolved byte range of a segment (inclusive offset, explicit length)."""

    offset: int
    length: int

    @property
    def end(self) -> int:
        """Offset of the last byte covered by the range (inclusive)."""

        return self.offset + self.length - 1


@dataclass
class Segment:
    sequence: int
    uri: str
    duration: Decimal
    discontinuity: int
    byte_range: Optional[ByteRange] = None
    # Raw EXT-X-PROGRAM-DATE-TIME values attached to this segment (a segment
    # may carry more than one in malformed playlists); parsed later in locate.
    program_date_times: tuple[str, ...] = ()


def _non_negative_int(raw: str, tag: str) -> int:
    token = raw.strip()
    if not token or not token.isascii() or not token.isdigit():
        raise PlaylistParseError(f"{tag} must be a non-negative integer, got {raw!r}")
    return int(token)


def parse_duration(raw: str) -> Decimal:
    token = raw.strip()
    if not token:
        raise PlaylistParseError("EXTINF duration is empty")
    try:
        value = Decimal(token)
    except InvalidOperation as exc:
        raise PlaylistParseError(f"invalid EXTINF duration {raw!r}") from exc
    if not value.is_finite() or value < 0:
        raise PlaylistParseError(f"EXTINF duration must be a finite non-negative number, got {raw!r}")
    return value


def parse_byte_range(raw: str) -> tuple[int, Optional[int]]:
    """Parse ``n`` / ``n@o`` into ``(length, offset_or_none)`` (offset not resolved here)."""

    token = raw.strip()
    if not token:
        raise PlaylistParseError("EXT-X-BYTERANGE is empty")
    length_part, sep, offset_part = token.partition("@")
    if not length_part.isascii() or not length_part.isdigit() or not length_part:
        raise PlaylistParseError(f"invalid EXT-X-BYTERANGE length {raw!r}")
    length = int(length_part)
    if length <= 0:
        raise PlaylistParseError(f"EXT-X-BYTERANGE length must be positive, got {raw!r}")
    offset: Optional[int] = None
    if sep:
        if not offset_part or not offset_part.isascii() or not offset_part.isdigit():
            raise PlaylistParseError(f"invalid EXT-X-BYTERANGE offset {raw!r}")
        offset = int(offset_part)
    return length, offset


def parse_playlist(text: str, playlist_url: str) -> list[Segment]:
    """Parse a media playlist into ordered segments.

    Segment media sequence numbers start at ``EXT-X-MEDIA-SEQUENCE`` (default 0)
    and increment by one per URI. The discontinuity counter starts at
    ``EXT-X-DISCONTINUITY-SEQUENCE`` (default 0) and is incremented once for
    every ``EXT-X-BYTERANGE`` encountered *before* the segment it flags.
    """

    if text is None or not text.strip():
        raise PlaylistParseError("playlist is empty")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise PlaylistParseError("playlist must start with #EXTM3U")

    media_sequence = 0
    base_discontinuity = 0
    discontinuity = 0
    pending_duration: Optional[Decimal] = None
    pending_byte_range: Optional[tuple[int, Optional[int]]] = None
    pending_program_date_times: list[str] = []
    segments: list[Segment] = []

    for line in lines[1:]:
        if not line.startswith("#"):
            # Media segment URI line.
            if pending_duration is None:
                raise PlaylistParseError("segment URI without preceding EXTINF")

            uri = urljoin(playlist_url, line)
            if not uri:
                raise PlaylistParseError("empty segment URI")

            byte_range: Optional[ByteRange] = None
            if pending_byte_range is not None:
                length, offset = pending_byte_range
                if offset is None:
                    # No explicit offset: only the immediately preceding segment
                    # of THIS playlist, and only when it shares the same URI,
                    # may supply the continuation offset.
                    previous = segments[-1] if segments else None
                    if previous is None or previous.uri != uri or previous.byte_range is None:
                        raise PlaylistParseError(
                            "EXT-X-BYTERANGE without offset requires the immediately "
                            "preceding segment in the same playlist to use the same URI "
                            "and an explicit/resolved byte range"
                        )
                    offset = previous.byte_range.offset + previous.byte_range.length
                byte_range = ByteRange(offset=offset, length=length)

            sequence = media_sequence + len(segments)
            segments.append(
                Segment(
                    sequence=sequence,
                    uri=uri,
                    duration=pending_duration,
                    discontinuity=discontinuity,
                    byte_range=byte_range,
                    program_date_times=tuple(pending_program_date_times),
                )
            )
            pending_duration = None
            pending_byte_range = None
            pending_program_date_times = []
            continue

        if not line.startswith("#EXT"):
            # Plain comment (#... that is not a tag): ignore.
            continue

        if ":" in line:
            tag, value = line.split(":", 1)
        else:
            tag, value = line, ""

        if tag == "#EXT-X-MEDIA-SEQUENCE":
            media_sequence = _non_negative_int(value, tag)
            if segments:
                raise PlaylistParseError("EXT-X-MEDIA-SEQUENCE must appear before the first segment")
        elif tag == "#EXT-X-DISCONTINUITY-SEQUENCE":
            base_discontinuity = _non_negative_int(value, tag)
            discontinuity = base_discontinuity
            if segments:
                raise PlaylistParseError(
                    "EXT-X-DISCONTINUITY-SEQUENCE must appear before the first segment"
                )
        elif tag == "#EXT-X-DISCONTINUITY":
            discontinuity += 1
        elif tag == "#EXTINF":
            duration_part = value.split(",", 1)[0]
            pending_duration = parse_duration(duration_part)
        elif tag == "#EXT-X-BYTERANGE":
            pending_byte_range = parse_byte_range(value)
        elif tag == "#EXT-X-PROGRAM-DATE-TIME":
            token = value.strip()
            if not token:
                raise PlaylistParseError("EXT-X-PROGRAM-DATE-TIME is empty")
            pending_program_date_times.append(token)
        # Every other EXT tag is intentionally ignored.

    if pending_duration is not None or pending_byte_range is not None:
        raise PlaylistParseError("dangling EXTINF/EXT-X-BYTERANGE without segment URI")
    if pending_program_date_times:
        raise PlaylistParseError(
            "dangling EXT-X-PROGRAM-DATE-TIME without segment URI"
        )

    return segments
