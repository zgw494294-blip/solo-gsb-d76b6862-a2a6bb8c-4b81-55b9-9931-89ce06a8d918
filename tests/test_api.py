"""Tests for parsing, reconciliation and the HTTP API."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.hls import PlaylistParseError, parse_playlist
from app.locate import (
    LocateError,
    format_program_time,
    locate,
    parse_program_time,
)
from app.main import app
from app.reconcile import ReconcileError, reconcile

client = TestClient(app)

BASE_URL = "https://cdn.example.com/live/index.m3u8"
SEG_URL = "https://cdn.example.com/live/seg.ts"


def m3u(segments, *, media_sequence=None, discont_sequence=None, discont_before=(),
        byteranges=None, uris=None, pdt_before=None, durations=None):
    """Build a small media playlist.

    segments: number of segments (durations 1.10)
    discont_before: indexes (0-based within this playlist) flagged with
                    EXT-X-DISCONTINUITY.
    byteranges: {index: "100@0" | "100"} (None entry means unranged).
    uris: optional per-index URI strings.
    pdt_before: {index: "2024-01-02T03:04:05Z"} EXT-X-PROGRAM-DATE-TIME values.
    durations: optional per-index EXTINF duration strings (default "1.10").
    """

    lines = ["#EXTM3U"]
    if media_sequence is not None:
        lines.append(f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}")
    if discont_sequence is not None:
        lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{discont_sequence}")
    byteranges = byteranges or {}
    pdt_before = pdt_before or {}
    for i in range(segments):
        if i in discont_before:
            lines.append("#EXT-X-DISCONTINUITY")
        if i in pdt_before:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt_before[i]}")
        duration = durations[i] if durations is not None else "1.10"
        lines.append(f"#EXTINF:{duration},")
        if i in byteranges:
            lines.append(f"#EXT-X-BYTERANGE:{byteranges[i]}")
        uri = uris[i] if uris else "seg.ts"
        lines.append(uri)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- parser

def test_parse_defaults_sequences_and_discontinuity():
    segs = parse_playlist(m3u(3), BASE_URL)
    assert [s.sequence for s in segs] == [0, 1, 2]
    assert [s.discontinuity for s in segs] == [0, 0, 0]
    assert all(s.uri == SEG_URL for s in segs)
    assert all(s.duration == Decimal("1.10") for s in segs)


def test_parse_media_sequence_and_relative_uri_resolution():
    text = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:100",
        "#EXTINF:2.5,",
        "../v/clip.aac",
    ])
    segs = parse_playlist(text, "https://cdn.example.com/live/idx/index.m3u8")
    assert segs[0].sequence == 100
    assert segs[0].uri == "https://cdn.example.com/live/v/clip.aac"


def test_parse_discontinuity_counter_starts_at_base_and_increments():
    segs = parse_playlist(m3u(4, media_sequence=10, discont_sequence=5,
                              discont_before=(1, 3)), BASE_URL)
    assert [s.discontinuity for s in segs] == [5, 6, 6, 7]


def test_parse_byterange_explicit_offsets():
    pl = m3u(2, media_sequence=0, byteranges={0: "100@0", 1: "200@100"})
    segs = parse_playlist(pl, BASE_URL)
    assert (segs[0].byte_range.offset, segs[0].byte_range.length) == (0, 100)
    assert (segs[1].byte_range.offset, segs[1].byte_range.length) == (100, 200)


def test_parse_byterange_implicit_offset_tracks_same_uri_predecessor():
    text = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:100@0",
        "big.ts",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:50",
        "big.ts",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:25",
        "big.ts",
    ])
    segs = parse_playlist(text, BASE_URL)
    assert [s.byte_range.offset for s in segs] == [0, 100, 150]
    assert [s.byte_range.length for s in segs] == [100, 50, 25]


def test_parse_byterange_implicit_offset_rejected_for_different_uri():
    text = "\n".join([
        "#EXTM3U",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:100@0",
        "a.ts",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:50",
        "b.ts",
    ])
    with pytest.raises(PlaylistParseError):
        parse_playlist(text, BASE_URL)


def test_parse_byterange_implicit_offset_rejected_as_first_segment():
    text = "\n".join(["#EXTM3U", "#EXTINF:1.0,", "#EXT-X-BYTERANGE:50", "a.ts"])
    with pytest.raises(PlaylistParseError):
        parse_playlist(text, BASE_URL)


def test_parse_rejects_bad_playlist():
    with pytest.raises(PlaylistParseError):
        parse_playlist("#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\na.ts\n", BASE_URL)
    with pytest.raises(PlaylistParseError):
        parse_playlist("", BASE_URL)
    with pytest.raises(PlaylistParseError):
        parse_playlist("#EXTM3U\n#EXTINF:x,\na.ts\n", BASE_URL)


def test_parse_program_date_time_attaches_to_following_segment():
    pl = m3u(3, media_sequence=10,
             discont_before=(1,), discont_sequence=2,
             pdt_before={0: "2024-01-02T03:04:05Z", 1: "2024-01-02T03:04:10Z"})
    segs = parse_playlist(pl, BASE_URL)
    assert segs[0].program_date_times == ("2024-01-02T03:04:05Z",)
    assert segs[1].program_date_times == ("2024-01-02T03:04:10Z",)
    assert segs[2].program_date_times == ()


def test_parse_rejects_empty_and_dangling_program_date_time():
    with pytest.raises(PlaylistParseError):
        parse_playlist("#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:\n#EXTINF:1,\na.ts\n", BASE_URL)
    with pytest.raises(PlaylistParseError):
        parse_playlist("#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2024-01-02T03:04:05Z\n", BASE_URL)


# ------------------------------------------------------------- reconcile

def test_reconcile_sliding_window_with_overlap():
    snaps = [m3u(3, media_sequence=0), m3u(3, media_sequence=2), m3u(3, media_sequence=4)]
    timeline = reconcile(BASE_URL, snaps)
    assert [s.sequence for s in timeline] == [0, 1, 2, 3, 4, 5, 6]
    assert timeline[0].start == Decimal("0")
    assert timeline[1].start == Decimal("1.10")
    assert timeline[-1].end == Decimal("7.70")


def test_reconcile_exact_adjacent_windows():
    snaps = [m3u(2, media_sequence=0), m3u(2, media_sequence=2)]
    timeline = reconcile(BASE_URL, snaps)
    assert [s.sequence for s in timeline] == [0, 1, 2, 3]


def test_reconcile_rejects_rollback():
    snaps = [m3u(2, media_sequence=10), m3u(2, media_sequence=9)]
    with pytest.raises(ReconcileError) as exc:
        reconcile(BASE_URL, snaps)
    assert exc.value.code == "sequence_rollback"
    assert exc.value.snapshot_index == 1
    assert exc.value.sequence == 9


def test_reconcile_rejects_gap():
    snaps = [m3u(2, media_sequence=0), m3u(2, media_sequence=5)]
    with pytest.raises(ReconcileError) as exc:
        reconcile(BASE_URL, snaps)
    assert exc.value.code == "sequence_gap"
    assert exc.value.snapshot_index == 1
    assert exc.value.sequence == 5


def test_reconcile_rejects_overlap_conflict_on_duration():
    s0 = m3u(3, media_sequence=0)
    s1 = m3u(3, media_sequence=1)
    # Change duration of sequence 1 (overlap region) in the second snapshot.
    s1 = s1.replace("#EXTINF:1.10,", "#EXTINF:9.00,", 1)
    with pytest.raises(ReconcileError) as exc:
        reconcile(BASE_URL, [s0, s1])
    assert exc.value.code == "overlap_conflict"
    assert exc.value.snapshot_index == 1
    assert exc.value.sequence == 1


def test_reconcile_rejects_overlap_conflict_on_discontinuity():
    s0 = m3u(3, media_sequence=0)
    s1 = m3u(3, media_sequence=1, discont_before=(0,))
    with pytest.raises(ReconcileError) as exc:
        reconcile(BASE_URL, [s0, s1])
    assert exc.value.code == "overlap_conflict"
    assert exc.value.sequence == 1


def test_reconcile_rejects_overlap_conflict_on_byte_range():
    s0 = m3u(2, media_sequence=0, byteranges={0: "100@0", 1: "100@100"})
    s1 = m3u(2, media_sequence=1, byteranges={0: "100@100"})  # seq 1 same
    s1 = s1.replace("#EXT-X-BYTERANGE:100@100", "#EXT-X-BYTERANGE:100@200")
    with pytest.raises(ReconcileError) as exc:
        reconcile(BASE_URL, [s0, s1])
    assert exc.value.code == "overlap_conflict"
    assert exc.value.sequence == 1


def test_reconcile_accepts_identical_overlap_with_discontinuity():
    s0 = m3u(4, media_sequence=10, discont_sequence=2, discont_before=(2,))
    s1 = m3u(3, media_sequence=12, discont_sequence=2, discont_before=(0,))
    timeline = reconcile(BASE_URL, [s0, s1])
    by_seq = {s.sequence: s for s in timeline}
    assert by_seq[11].discontinuity == 2
    assert by_seq[12].discontinuity == 3
    assert by_seq[13].discontinuity == 3
    assert by_seq[14].discontinuity == 3


def test_reconcile_decimal_precision_is_exact():
    text = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXTINF:0.1,", "a.ts",
        "#EXTINF:0.2,", "a.ts",
        "#EXTINF:0.3,", "a.ts",
    ])
    timeline = reconcile(BASE_URL, [text])
    assert [format(s.end, "f") for s in timeline] == ["0.1", "0.3", "0.6"]


# ------------------------------------------------------------------- locate

def test_parse_program_time_normalizes_offsets_to_utc():
    assert parse_program_time("2024-01-02T11:04:05Z") == parse_program_time(
        "2024-01-02T03:04:05-08:00"
    )
    assert parse_program_time("2024-01-01T21:34:05Z") == parse_program_time(
        "2024-01-02T03:04:05+05:30"
    )


def test_parse_program_time_keeps_exact_decimal_fraction():
    assert format(parse_program_time("2024-01-02T03:04:05.123456789Z"), "f") == (
        "1704164645.123456789"
    )
    with pytest.raises(PlaylistParseError):
        parse_program_time("2024-01-02T03:04:05")  # naive, no offset
    with pytest.raises(PlaylistParseError):
        parse_program_time("2024-01-02 03:04:05Z")  # missing T separator


def test_format_program_time_roundtrip():
    raw = "2024-01-02T03:04:05.500Z"
    assert format_program_time(parse_program_time(raw)) == "2024-01-02T03:04:05.5Z"
    # Pre-epoch negative timestamp keeps its fraction.
    assert format_program_time(Decimal("-0.5")) == "1969-12-31T23:59:59.5Z"


def _anchored_playlist():
    # Anchor (UTC after +08:00 normalization): seq 0 at 2024-01-01T19:04:05Z.
    return "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-02T03:04:05+08:00",
        "#EXTINF:1.10,", "seg0.ts",
        "#EXTINF:2.20,", "seg1.ts",
        "#EXTINF:0.25,", "seg2.ts",
    ]) + "\n"


def test_locate_half_open_range_and_decimal_offset():
    pl = _anchored_playlist()
    found = locate(BASE_URL, [pl, pl], "2024-01-01T19:04:07.5Z")
    assert found.sequence == 1
    assert found.uri == "https://cdn.example.com/live/seg1.ts"
    assert format_program_time(found.start_at) == "2024-01-01T19:04:06.1Z"
    assert format_program_time(found.end_at) == "2024-01-01T19:04:08.3Z"
    assert format(found.offset, "f") == "1.40"

    # Left boundary belongs to the segment; right boundary does not.
    edge = locate(BASE_URL, [pl, pl], "2024-01-01T19:04:06.1Z")
    assert edge.sequence == 1
    assert format(edge.offset, "f") == "0.00"
    last = locate(BASE_URL, [pl, pl], "2024-01-01T19:04:08.3Z")
    assert last.sequence == 2  # seq2 spans [19:04:08.3, 19:04:08.55)


def test_locate_extrapolates_backward_before_anchor():
    # Anchor sits on seq 1; seq 0's start must be derived by subtraction.
    pl = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXTINF:1.10,", "seg0.ts",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T19:04:06.1Z",
        "#EXTINF:2.20,", "seg1.ts",
    ]) + "\n"
    found = locate(BASE_URL, [pl, pl], "2024-01-01T19:04:05.5Z")
    assert found.sequence == 0
    assert format(found.offset, "f") == "0.50"


def test_locate_outside_known_coverage_is_404():
    pl = _anchored_playlist()
    with pytest.raises(LocateError) as exc:
        locate(BASE_URL, [pl, pl], "2024-01-01T19:04:09Z")
    assert exc.value.status == 404
    assert exc.value.code == "program_time_uncovered"


def test_locate_without_any_anchor_is_404():
    pl = m3u(3, media_sequence=0)
    with pytest.raises(LocateError) as exc:
        locate(BASE_URL, [pl, pl], "2024-01-01T19:04:05Z")
    assert exc.value.status == 404


def test_locate_never_extrapolates_across_discontinuity():
    # Region 0 anchored at 19:04:05Z (3 segs of 1.10); region 1 unanchored.
    pl = m3u(5, media_sequence=0, discont_before=(3,),
             pdt_before={0: "2024-01-01T19:04:05Z"})
    # Inside region 0.
    assert locate(BASE_URL, [pl, pl], "2024-01-01T19:04:06Z").sequence == 0
    # Time that would belong to seq 3 if extrapolation crossed the break.
    with pytest.raises(LocateError) as exc:
        locate(BASE_URL, [pl, pl], "2024-01-01T19:04:09Z")
    assert exc.value.status == 404


def test_locate_multi_hit_across_discontinuities_is_422():
    pl = "\n".join([
        "#EXTM3U",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T19:04:05Z",
        "#EXTINF:10,", "a.ts",
        "#EXT-X-DISCONTINUITY",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T19:04:06Z",
        "#EXTINF:10,", "b.ts",
    ]) + "\n"
    with pytest.raises(LocateError) as exc:
        locate(BASE_URL, [pl, pl], "2024-01-01T19:04:07Z")
    assert exc.value.status == 422
    assert exc.value.code == "ambiguous_program_time"
    assert exc.value.sequences == [0, 1]


def test_locate_cross_snapshot_anchor_mismatch_is_422():
    s0 = m3u(1, media_sequence=0, pdt_before={0: "2024-01-01T19:04:05Z"})
    s1 = m3u(1, media_sequence=0, pdt_before={0: "2024-01-01T19:04:09Z"})
    with pytest.raises(LocateError) as exc:
        locate(BASE_URL, [s0, s1], "2024-01-01T19:04:05Z")
    assert exc.value.code == "program_time_conflict"
    assert exc.value.snapshot_index == 1
    assert exc.value.sequence == 0


def test_locate_cross_snapshot_anchor_agrees_after_utc_normalization():
    s0 = m3u(1, media_sequence=0, pdt_before={0: "2024-01-01T19:04:05Z"})
    s1 = m3u(1, media_sequence=0, pdt_before={0: "2024-01-02T03:04:05+08:00"})
    found = locate(BASE_URL, [s0, s1], "2024-01-01T19:04:05.5Z")
    assert found.sequence == 0


def test_locate_multiple_anchors_in_region_must_match_extrapolation():
    good = m3u(2, media_sequence=0,
               pdt_before={0: "2024-01-01T19:04:05Z", 1: "2024-01-01T19:04:06.1Z"})
    found = locate(BASE_URL, [good, good], "2024-01-01T19:04:06.5Z")
    assert found.sequence == 1

    bad = m3u(2, media_sequence=0,
              pdt_before={0: "2024-01-01T19:04:05Z", 1: "2024-01-01T19:04:07Z"})
    with pytest.raises(LocateError) as exc:
        locate(BASE_URL, [bad, bad], "2024-01-01T19:04:05Z")
    assert exc.value.code == "program_time_conflict"
    assert exc.value.snapshot_index == 0
    assert exc.value.sequence == 1


def test_locate_invalid_pdt_is_422():
    pl = "#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:not-a-date\n#EXTINF:1,\na.ts\n"
    with pytest.raises(LocateError) as exc:
        locate(BASE_URL, [pl, pl], "2024-01-01T19:04:05Z")
    assert exc.value.code == "invalid_playlist"
    assert exc.value.snapshot_index == 0
    assert exc.value.sequence == 0


def test_locate_reuses_reconcile_merge_rules():
    s0 = m3u(2, media_sequence=10)
    s1 = m3u(2, media_sequence=9)
    with pytest.raises(ReconcileError) as exc:
        locate(BASE_URL, [s0, s1], "2024-01-01T19:04:05Z")
    assert exc.value.code == "sequence_rollback"


def _counter_reuse_snapshots(*, anchor_first=True, anchor_second=None):
    """Two non-overlapping, sequence-adjacent snapshots that reuse counters.

    The merged timeline runs are [0,1] (counter 0) and [2,3] (counter 1)
    followed by [4,5] (counter 0 reused) and [6,7] (counter 1 reused), i.e.
    each counter value recurs in a separate contiguous sequence run.
    """

    pdt0 = {0: "2024-01-01T19:04:05Z"} if anchor_first else None
    s0 = m3u(
        4,
        media_sequence=0,
        discont_before=(2,),
        pdt_before=pdt0,
        uris=[f"seg{i}.ts" for i in range(4)],
    )
    pdt1 = {0: anchor_second} if anchor_second is not None else None
    s1 = m3u(
        4,
        media_sequence=4,
        discont_before=(2,),
        pdt_before=pdt1,
        uris=[f"seg{i}.ts" for i in range(4, 8)],
    )
    return s0, s1


def test_locate_non_overlapping_snapshots_with_counter_reuse_hit():
    # Regression: two snapshots with no overlap, joined purely by sequence,
    # reusing discontinuity counters, used to raise an unhandled KeyError.
    s0, s1 = _counter_reuse_snapshots()
    found = locate(BASE_URL, [s0, s1], "2024-01-01T19:04:07Z")
    assert found.sequence == 1
    assert found.uri == "https://cdn.example.com/live/seg1.ts"
    assert format_program_time(found.start_at) == "2024-01-01T19:04:06.1Z"
    assert format_program_time(found.end_at) == "2024-01-01T19:04:07.2Z"
    assert format(found.offset, "f") == "0.90"


def test_locate_counter_reuse_does_not_extrapolate_into_unanchored_run():
    # Only the first counter-0 run is anchored; the reused counter-0 run and
    # the unanchored counter-1 runs have no known UTC coverage.
    s0, s1 = _counter_reuse_snapshots()
    with pytest.raises(LocateError) as exc:
        locate(BASE_URL, [s0, s1], "2024-01-01T19:04:11Z")
    assert exc.value.status == 404
    assert exc.value.code == "program_time_uncovered"


def test_locate_counter_reuse_validates_reused_runs_independently():
    # Both counter-0 runs carry their own anchors. They must be validated and
    # extrapolated separately; the reused counter must not merge them into one
    # region (which would falsely flag program_time_conflict).
    s0, s1 = _counter_reuse_snapshots(anchor_second="2024-01-01T20:00:00Z")
    found = locate(BASE_URL, [s0, s1], "2024-01-01T20:00:01.5Z")
    assert found.sequence == 5
    assert format_program_time(found.start_at) == "2024-01-01T20:00:01.1Z"
    assert format(found.offset, "f") == "0.40"


def test_locate_counter_reuse_within_run_conflict_is_still_detected():
    # A second anchor inside the reused run must match *local* extrapolation.
    s0, _ = _counter_reuse_snapshots()
    s1_bad = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:4",
        "#EXT-X-DISCONTINUITY",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T20:00:00Z",
        "#EXTINF:1.10,", "seg4.ts",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T20:00:05Z",  # expected 20:00:01.1
        "#EXTINF:1.10,", "seg5.ts",
        "#EXT-X-DISCONTINUITY",
        "#EXTINF:1.10,", "seg6.ts",
        "#EXTINF:1.10,", "seg7.ts",
    ]) + "\n"
    with pytest.raises(LocateError) as exc:
        locate(BASE_URL, [s0, s1_bad], "2024-01-01T20:00:00.5Z")
    assert exc.value.code == "program_time_conflict"
    assert exc.value.snapshot_index == 1
    assert exc.value.sequence == 5

    # The correct second anchor (20:00:01.1) resolves within the reused run.
    s1_good = s1_bad.replace("2024-01-01T20:00:05Z", "2024-01-01T20:00:01.1Z")
    found = locate(BASE_URL, [s0, s1_good], "2024-01-01T20:00:00.5Z")
    assert found.sequence == 4


def test_locate_counter_reuse_multi_hit_across_runs_is_422():
    # The two independent counter-0 runs have overlapping UTC coverage, so a
    # target covered by both is ambiguous even though the counter is reused.
    s0, s1 = _counter_reuse_snapshots(anchor_second="2024-01-01T19:04:05.5Z")
    with pytest.raises(LocateError) as exc:
        locate(BASE_URL, [s0, s1], "2024-01-01T19:04:06Z")
    assert exc.value.status == 422
    assert exc.value.code == "ambiguous_program_time"
    assert exc.value.sequences == [0, 4]


# ------------------------------------------------------------------ API

def post(snapshots, url=BASE_URL):
    return client.post("/v1/reconcile", json={"playlist_url": url, "snapshots": snapshots})


def test_api_happy_path():
    resp = post([m3u(3, media_sequence=0), m3u(3, media_sequence=2)])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 5
    assert body["duration"] == "5.50"
    assert body["segments"][0] == {
        "sequence": 0,
        "uri": SEG_URL,
        "duration": "1.10",
        "discontinuity": 0,
        "byte_offset": None,
        "byte_length": None,
        "start": "0",
        "end": "1.10",
    }


def test_api_rollback_returns_422_with_snapshot_and_sequence():
    resp = post([m3u(2, media_sequence=10), m3u(2, media_sequence=9)])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "sequence_rollback"
    assert body["snapshot_index"] == 1
    assert body["sequence"] == 9


def test_api_overlap_conflict_returns_422_with_snapshot_and_sequence():
    s0 = m3u(2, media_sequence=0)
    s1 = m3u(2, media_sequence=1).replace("#EXTINF:1.10,", "#EXTINF:2.20,", 1)
    resp = post([s0, s1])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "overlap_conflict"
    assert body["snapshot_index"] == 1
    assert body["sequence"] == 1


def test_api_gap_returns_422():
    resp = post([m3u(1, media_sequence=0), m3u(1, media_sequence=7)])
    assert resp.status_code == 422
    assert resp.json()["error"] == "sequence_gap"


def test_api_invalid_playlist_returns_422():
    resp = post(["#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\nx.ts\n", m3u(2, media_sequence=0)])
    assert resp.status_code == 422
    assert resp.json()["snapshot_index"] == 0


def test_api_rejects_wrong_snapshot_count():
    assert post([m3u(1)]).status_code == 422  # only one snapshot
    assert post([m3u(1)] * 31).status_code == 422


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


# ------------------------------------------------------- API: /v1/locate

def locate_post(snapshots, target, url=BASE_URL):
    return client.post(
        "/v1/locate",
        json={"playlist_url": url, "snapshots": snapshots, "target_at": target},
    )


def test_api_locate_happy_path():
    pl = _anchored_playlist()
    resp = locate_post([pl, pl], "2024-01-01T19:04:07.5Z")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "sequence": 1,
        "uri": "https://cdn.example.com/live/seg1.ts",
        "start_at": "2024-01-01T19:04:06.1Z",
        "end_at": "2024-01-01T19:04:08.3Z",
        "offset": "1.40",
    }


def test_api_locate_timezone_offset_is_normalized():
    pl = _anchored_playlist()
    resp = locate_post([pl, pl], "2024-01-02T03:04:07.5+08:00")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sequence"] == 1
    assert body["start_at"] == "2024-01-01T19:04:06.1Z"


def test_api_locate_404_when_uncovered():
    resp = locate_post([m3u(3), m3u(3)], "2024-01-01T19:04:05Z")
    assert resp.status_code == 404
    assert resp.json()["error"] == "program_time_uncovered"


def test_api_locate_conflict_returns_422_with_snapshot_and_sequence():
    s0 = m3u(1, media_sequence=0, pdt_before={0: "2024-01-01T19:04:05Z"})
    s1 = m3u(1, media_sequence=0, pdt_before={0: "2024-01-01T19:04:09Z"})
    resp = locate_post([s0, s1], "2024-01-01T19:04:05Z")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "program_time_conflict"
    assert body["snapshot_index"] == 1
    assert body["sequence"] == 0


def test_api_locate_multi_hit_returns_422_with_sequences():
    pl = "\n".join([
        "#EXTM3U",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T19:04:05Z",
        "#EXTINF:10,", "a.ts",
        "#EXT-X-DISCONTINUITY",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T19:04:06Z",
        "#EXTINF:10,", "b.ts",
    ]) + "\n"
    resp = locate_post([pl, pl], "2024-01-01T19:04:07Z")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "ambiguous_program_time"
    assert body["sequences"] == [0, 1]


def test_api_locate_non_overlapping_counter_reuse_snapshots():
    # Regression: adjacent, non-overlapping snapshots reusing discontinuity
    # counters used to crash with an unhandled KeyError (HTTP 500).
    s0, s1 = _counter_reuse_snapshots()
    resp = locate_post([s0, s1], "2024-01-01T19:04:07Z")
    assert resp.status_code == 200, resp.text
    assert resp.json()["sequence"] == 1
    # The reused counter run carries no anchor of its own: no coverage -> 404.
    uncovered = locate_post([s0, s1], "2024-01-01T19:04:11Z")
    assert uncovered.status_code == 404


def test_api_locate_rejects_naive_target_at():
    resp = locate_post([m3u(1), m3u(1)], "2024-01-01T19:04:05")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"][-1] == "target_at"


def test_api_locate_rejects_missing_target_at():
    resp = client.post(
        "/v1/locate",
        json={"playlist_url": BASE_URL, "snapshots": [m3u(1), m3u(1)]},
    )
    assert resp.status_code == 422


def test_api_reconcile_still_available():
    resp = post([m3u(2, media_sequence=0), m3u(2, media_sequence=2)])
    assert resp.status_code == 200
    assert resp.json()["count"] == 4


# ------------------------------------------------------- API: /v1/vod

def vod_post(snapshots, from_sec, to_sec, url=BASE_URL):
    return client.post(
        "/v1/vod",
        json={
            "playlist_url": url,
            "snapshots": snapshots,
            "from_sec": from_sec,
            "to_sec": to_sec,
        },
    )


def test_api_vod_happy_path_selects_intersecting_window():
    snaps = [
        m3u(3, media_sequence=0, uris=["a0.ts", "a1.ts", "a2.ts"]),
        m3u(2, media_sequence=3, uris=["a3.ts", "a4.ts"]),
    ]
    # Boundaries: 0, 1.10, 2.20, 3.30, 4.40, 5.50. Window [1.10, 4.40)
    # covers exactly segments a1, a2, a3 (half-open on both ends).
    resp = vod_post(snaps, "1.10", "4.40")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["playlist_url"] == BASE_URL
    assert body["from_sec"] == "1.10"
    assert body["to_sec"] == "4.40"
    assert body["covered_start"] == "1.10"
    assert body["covered_end"] == "4.40"
    assert body["head_extra_sec"] == "0.00"
    assert body["tail_extra_sec"] == "0.00"
    assert body["segment_count"] == 3
    assert body["playlist"] == (
        "#EXTM3U\n"
        "#EXT-X-VERSION:4\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXT-X-TARGETDURATION:2\n"
        "#EXTINF:1.10,\n"
        "https://cdn.example.com/live/a1.ts\n"
        "#EXTINF:1.10,\n"
        "https://cdn.example.com/live/a2.ts\n"
        "#EXTINF:1.10,\n"
        "https://cdn.example.com/live/a3.ts\n"
        "#EXT-X-ENDLIST\n"
    )


def test_api_vod_partial_window_reports_extra_seconds_at_both_ends():
    snaps = [m3u(2, media_sequence=0), m3u(2, media_sequence=2)]
    # Total 4.40; window [0.5, 3.0) selects segments 0..2 -> covers [0, 3.30].
    resp = vod_post(snaps, "0.5", "3.0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["covered_start"] == "0"
    assert body["covered_end"] == "3.30"
    assert body["head_extra_sec"] == "0.5"
    assert body["tail_extra_sec"] == "0.30"
    assert body["segment_count"] == 3


def test_api_vod_full_window_has_zero_extra():
    snaps = [m3u(2, media_sequence=0), m3u(2, media_sequence=2)]
    resp = vod_post(snaps, "0", "4.40")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["segment_count"] == 4
    assert body["covered_start"] == "0"
    assert body["covered_end"] == "4.40"
    assert body["head_extra_sec"] == "0"
    assert body["tail_extra_sec"] == "0.00"


def test_api_vod_targetduration_ceils_longest_and_extinf_keeps_precision():
    text = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXTINF:2.000,", "a.ts",
        "#EXTINF:3.333,", "b.ts",
        "#EXTINF:0.25,", "c.ts",
    ]) + "\n"
    resp = vod_post([text, text], "0", "5.333")
    assert resp.status_code == 200, resp.text
    playlist = resp.json()["playlist"]
    assert "#EXT-X-TARGETDURATION:4\n" in playlist
    assert "#EXTINF:2.000,\n" in playlist
    assert "#EXTINF:3.333,\n" in playlist
    # Segment c starts exactly at to_sec and is excluded (half-open).
    assert "c.ts" not in playlist


def test_api_vod_inserts_discontinuity_only_on_counter_change():
    snap = m3u(4, media_sequence=0, discont_sequence=2, discont_before=(2,),
               uris=["d0.ts", "d1.ts", "d2.ts", "d3.ts"])
    resp = vod_post([snap, snap], "0", "4.40")
    assert resp.status_code == 200, resp.text
    playlist = resp.json()["playlist"]
    assert playlist.count("#EXT-X-DISCONTINUITY\n") == 1
    lines = playlist.splitlines()
    idx = lines.index("#EXT-X-DISCONTINUITY")
    assert lines[idx + 1] == "#EXTINF:1.10,"
    assert lines[idx + 2] == "https://cdn.example.com/live/d2.ts"


def test_api_vod_no_discontinuity_before_first_segment():
    # First selected segment carries counter 5, but nothing precedes it in the
    # output playlist, so no DISCONTINUITY tag may be emitted.
    snap = m3u(3, media_sequence=0, discont_sequence=5)
    resp = vod_post([snap, snap], "0", "3.30")
    assert resp.status_code == 200, resp.text
    assert "#EXT-X-DISCONTINUITY" not in resp.json()["playlist"]


def test_api_vod_byterange_always_written_as_length_at_offset():
    text = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:100@0",
        "big.ts",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:50",  # implicit offset, resolved against predecessor
        "big.ts",
    ]) + "\n"
    resp = vod_post([text, text], "0", "2.0")
    assert resp.status_code == 200, resp.text
    playlist = resp.json()["playlist"]
    assert "#EXT-X-BYTERANGE:100@0\n" in playlist
    assert "#EXT-X-BYTERANGE:50@100\n" in playlist


def test_api_vod_resolves_relative_uris_against_playlist_url():
    snap = m3u(2, media_sequence=0, uris=["../v/x.ts", "y.ts"])
    resp = vod_post([snap, snap], "0", "2.20",
                    url="https://cdn.example.com/live/idx/index.m3u8")
    assert resp.status_code == 200, resp.text
    playlist = resp.json()["playlist"]
    assert "https://cdn.example.com/live/v/x.ts\n" in playlist
    assert "https://cdn.example.com/live/idx/y.ts\n" in playlist


def test_api_vod_accepts_numeric_bounds():
    resp = vod_post([m3u(2), m3u(2)], 0, 2.2)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from_sec"] == "0"
    assert body["to_sec"] == "2.2"
    assert body["covered_end"] == "2.20"


def test_api_vod_rejects_ext_x_key():
    pl = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n'
          "#EXTINF:1,\na.ts\n")
    resp = vod_post([pl, pl], "0", "1")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unsupported_tag"
    assert body["snapshot_index"] == 0


def test_api_vod_rejects_ext_x_map():
    pl = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:1,\na.ts\n'
    resp = vod_post([m3u(1), pl], "0", "1")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unsupported_tag"
    assert body["snapshot_index"] == 1


def test_api_vod_invalid_ranges_are_422():
    snaps = [m3u(2, media_sequence=0), m3u(2, media_sequence=2)]  # total 4.40
    assert vod_post(snaps, "2.0", "2.0").status_code == 422   # from == to
    assert vod_post(snaps, "3.0", "2.0").status_code == 422   # from > to
    assert vod_post(snaps, "-1", "2").status_code == 422      # negative from
    assert vod_post(snaps, "0", "-2").status_code == 422      # negative to
    assert vod_post(snaps, "0", "4.41").status_code == 422    # beyond total
    assert vod_post(snaps, "x", "2").status_code == 422       # not a number
    body = vod_post(snaps, "0", "4.41").json()
    assert body["error"] == "invalid_range"
    body = vod_post(snaps, "3.0", "2.0").json()
    assert body["error"] == "invalid_range"


def test_api_vod_requires_absolute_http_playlist_url():
    snaps = [m3u(1), m3u(1)]
    assert vod_post(snaps, "0", "1", url="live/index.m3u8").status_code == 422
    assert vod_post(snaps, "0", "1", url="ftp://cdn.example.com/x.m3u8").status_code == 422
    assert vod_post(snaps, "0", "1", url="//cdn.example.com/x.m3u8").status_code == 422


def test_api_vod_merge_errors_still_return_422():
    resp = vod_post([m3u(1, media_sequence=0), m3u(1, media_sequence=7)], "0", "1")
    assert resp.status_code == 422
    assert resp.json()["error"] == "sequence_gap"

    resp = vod_post([m3u(2, media_sequence=10), m3u(2, media_sequence=9)], "0", "1")
    assert resp.status_code == 422
    assert resp.json()["error"] == "sequence_rollback"


def test_api_vod_rejects_wrong_snapshot_count():
    assert vod_post([m3u(1)], "0", "1").status_code == 422
    assert vod_post([m3u(1)] * 31, "0", "1").status_code == 422


# ------------------------------------------------------- API: /v1/fetch-plan

def fetch_plan_post(snapshots, url=BASE_URL):
    return client.post(
        "/v1/fetch-plan",
        json={"playlist_url": url, "snapshots": snapshots},
    )


def test_api_fetch_plan_merges_adjacent_ranges_and_orders_requests():
    s0 = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:100",
        "#EXTINF:2.000,",
        "#EXT-X-BYTERANGE:100@0",
        "big.ts",
        "#EXTINF:1.500,",
        "#EXT-X-BYTERANGE:50",  # implicit offset -> 100
        "big.ts",
        "#EXTINF:3.333,",
        "clip.ts",
    ]) + "\n"
    s1 = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:102",
        "#EXTINF:3.333,",
        "clip.ts",
        "#EXTINF:2.000,",
        "#EXT-X-BYTERANGE:40@200",
        "big.ts",
    ]) + "\n"
    resp = fetch_plan_post([s0, s1])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["playlist_url"] == BASE_URL
    # big.ts [0,100) and [100,150) are adjacent -> one bytes=0-149 request;
    # [200,240) stays separate. Requests follow first-involved sequence.
    assert body["requests"] == [
        {"uri": "https://cdn.example.com/live/big.ts", "range": "bytes=0-149"},
        {"uri": "https://cdn.example.com/live/clip.ts", "range": None},
        {"uri": "https://cdn.example.com/live/big.ts", "range": "bytes=200-239"},
    ]
    assert body["segments"] == [
        {"sequence": 100, "request_index": 0, "offset": 0, "length": 100},
        {"sequence": 101, "request_index": 0, "offset": 100, "length": 50},
        {"sequence": 102, "request_index": 1, "offset": None, "length": None},
        {"sequence": 103, "request_index": 2, "offset": 0, "length": 40},
    ]


def test_api_fetch_plan_full_resource_fetched_once_and_covers_fragments():
    s0 = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:100@0",
        "big.ts",
        "#EXTINF:1.0,",
        "big.ts",  # full resource -> the whole URI is fetched once
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:50@100",
        "big.ts",
    ]) + "\n"
    resp = fetch_plan_post([s0, s0])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requests"] == [
        {"uri": "https://cdn.example.com/live/big.ts", "range": None},
    ]
    # Fragments sit at their original offset inside the whole-resource body.
    assert body["segments"] == [
        {"sequence": 0, "request_index": 0, "offset": 0, "length": 100},
        {"sequence": 1, "request_index": 0, "offset": None, "length": None},
        {"sequence": 2, "request_index": 0, "offset": 100, "length": 50},
    ]


def test_api_fetch_plan_dedupes_full_resource_across_snapshots():
    snaps = [
        m3u(2, media_sequence=0, uris=["a.ts", "b.ts"]),
        m3u(2, media_sequence=2, uris=["a.ts", "c.ts"]),
    ]
    resp = fetch_plan_post(snaps)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requests"] == [
        {"uri": "https://cdn.example.com/live/a.ts", "range": None},
        {"uri": "https://cdn.example.com/live/b.ts", "range": None},
        {"uri": "https://cdn.example.com/live/c.ts", "range": None},
    ]
    assert body["segments"] == [
        {"sequence": 0, "request_index": 0, "offset": None, "length": None},
        {"sequence": 1, "request_index": 1, "offset": None, "length": None},
        {"sequence": 2, "request_index": 0, "offset": None, "length": None},
        {"sequence": 3, "request_index": 2, "offset": None, "length": None},
    ]


def test_api_fetch_plan_merges_overlapping_ranges():
    s0 = m3u(2, media_sequence=0, byteranges={0: "100@0", 1: "80@60"})
    resp = fetch_plan_post([s0, s0])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requests"] == [{"uri": SEG_URL, "range": "bytes=0-139"}]
    assert body["segments"] == [
        {"sequence": 0, "request_index": 0, "offset": 0, "length": 100},
        {"sequence": 1, "request_index": 0, "offset": 60, "length": 80},
    ]


def test_api_fetch_plan_keeps_disjoint_ranges_separate():
    s0 = m3u(2, media_sequence=0, byteranges={0: "10@0", 1: "10@20"})
    resp = fetch_plan_post([s0, s0])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requests"] == [
        {"uri": SEG_URL, "range": "bytes=0-9"},
        {"uri": SEG_URL, "range": "bytes=20-29"},
    ]
    assert body["segments"] == [
        {"sequence": 0, "request_index": 0, "offset": 0, "length": 10},
        {"sequence": 1, "request_index": 1, "offset": 0, "length": 10},
    ]


def test_api_fetch_plan_rejects_non_http_segment_uri():
    pl = "#EXTM3U\n#EXTINF:1.0,\nftp://cdn.example.com/seg.ts\n"
    resp = fetch_plan_post([pl, pl])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unsupported_uri"
    assert body["sequence"] == 0


def test_api_fetch_plan_rejects_ext_x_key_and_map():
    pl = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n'
          "#EXTINF:1,\na.ts\n")
    resp = fetch_plan_post([pl, pl])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unsupported_tag"
    assert body["snapshot_index"] == 0

    pl = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:1,\na.ts\n'
    resp = fetch_plan_post([m3u(1), pl])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unsupported_tag"
    assert body["snapshot_index"] == 1


def test_api_fetch_plan_requires_absolute_http_playlist_url():
    snaps = [m3u(1), m3u(1)]
    assert fetch_plan_post(snaps, url="live/index.m3u8").status_code == 422
    assert fetch_plan_post(snaps, url="ftp://cdn.example.com/x.m3u8").status_code == 422
    assert fetch_plan_post(snaps, url="//cdn.example.com/x.m3u8").status_code == 422


def test_api_fetch_plan_merge_errors_still_return_422():
    resp = fetch_plan_post([m3u(1, media_sequence=0), m3u(1, media_sequence=7)])
    assert resp.status_code == 422
    assert resp.json()["error"] == "sequence_gap"

    resp = fetch_plan_post([m3u(2, media_sequence=10), m3u(2, media_sequence=9)])
    assert resp.status_code == 422
    assert resp.json()["error"] == "sequence_rollback"

    s0 = m3u(2, media_sequence=0)
    s1 = m3u(2, media_sequence=1).replace("#EXTINF:1.10,", "#EXTINF:2.20,", 1)
    resp = fetch_plan_post([s0, s1])
    assert resp.status_code == 422
    assert resp.json()["error"] == "overlap_conflict"


def test_api_fetch_plan_rejects_wrong_snapshot_count():
    assert fetch_plan_post([m3u(1)]).status_code == 422
    assert fetch_plan_post([m3u(1)] * 31).status_code == 422

# ------------------------------------------------- library: intersect streams

from app.intersect import IntersectError, intersect_streams  # noqa: E402
from app.locate import format_program_time  # noqa: E402

URL_A = "https://a.example.com/live/index.m3u8"
URL_B = "https://b.example.com/live/index.m3u8"
T0 = "2024-01-01T19:04:00Z"


def _plain_stream(url, seq, durations, anchor=T0, *, discont_before=(),
                  pdt_extra=None, uris=None):
    """Build a one-snapshot (duplicated) anchored stream with given durations."""

    lines = ["#EXTM3U", f"#EXT-X-MEDIA-SEQUENCE:{seq}"]
    pdt_extra = pdt_extra or {}
    for i, duration in enumerate(durations):
        if i in discont_before:
            lines.append("#EXT-X-DISCONTINUITY")
        if i == 0 or i in pdt_extra:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt_extra.get(i, anchor)}")
        lines.append(f"#EXTINF:{duration},")
        lines.append((uris[i] if uris else f"s{seq + i}.ts"))
    text = "\n".join(lines) + "\n"
    return url, [text, text]


def _render(intervals):
    return [
        (
            item.sequence_a,
            item.sequence_b,
            format_program_time(item.start_at),
            format_program_time(item.end_at),
            format(item.offset_a, "f"),
            format(item.offset_b, "f"),
        )
        for item in intervals
    ]


def test_intersect_splits_at_both_streams_boundaries():
    # A: 3 segments of 2s -> boundaries :00 :02 :04 :06 (sequences 0..2)
    # B: 3 segments of 3s -> boundaries :00 :03 :06 :09 (sequences 10..12)
    ua, sa = _plain_stream(URL_A, 0, ["2", "2", "2"], uris=[f"a{i}.ts" for i in range(3)])
    ub, sb = _plain_stream(URL_B, 10, ["3", "3", "3"], uris=[f"b{i}.ts" for i in range(3)])
    result = _render(intersect_streams(ua, sa, ub, sb))
    assert result == [
        (0, 10, "2024-01-01T19:04:00Z", "2024-01-01T19:04:02Z", "0", "0"),
        (1, 10, "2024-01-01T19:04:02Z", "2024-01-01T19:04:03Z", "0", "2"),
        (1, 11, "2024-01-01T19:04:03Z", "2024-01-01T19:04:04Z", "1", "0"),
        (2, 11, "2024-01-01T19:04:04Z", "2024-01-01T19:04:06Z", "0", "1"),
    ]


def test_intersect_exact_decimal_offsets():
    # A segment [19:04:00.00, 19:04:02.50); B starts at 19:04:01.25 with
    # duration 0.75 -> common [19:04:01.25, 19:04:02.00).
    ua, sa = _plain_stream(URL_A, 0, ["2.50"])
    ub, sb = _plain_stream(URL_B, 7, ["0.75"], anchor="2024-01-01T19:04:01.25Z")
    result = intersect_streams(ua, sa, ub, sb)
    assert len(result) == 1
    item = result[0]
    assert (item.sequence_a, item.sequence_b) == (0, 7)
    assert format_program_time(item.start_at) == "2024-01-01T19:04:01.25Z"
    assert format_program_time(item.end_at) == "2024-01-01T19:04:02Z"
    assert format(item.offset_a, "f") == "1.25"
    assert format(item.offset_b, "f") == "0.00"


def test_intersect_zero_length_touch_is_not_output():
    # A covers [00, 06); B starts exactly at :06 -> ranges only touch.
    ua, sa = _plain_stream(URL_A, 0, ["2", "2", "2"])
    ub, sb = _plain_stream(URL_B, 10, ["2"], anchor="2024-01-01T19:04:06Z")
    assert intersect_streams(ua, sa, ub, sb) == []


def test_intersect_no_common_coverage_returns_empty():
    ua, sa = _plain_stream(URL_A, 0, ["2", "2"])
    ub, sb = _plain_stream(URL_B, 0, ["2", "2"], anchor="2024-01-01T20:00:00Z")
    assert intersect_streams(ua, sa, ub, sb) == []


def test_intersect_unanchored_region_is_not_filled():
    # A: anchored run seq0..2, then a discontinuity; seq3 is unanchored.
    # B covers exactly the UTC range the unanchored seq3 would occupy if
    # time were carried across the break — it must not pair.
    snap_a = m3u(4, media_sequence=0, discont_before=(3,),
                 durations=["2", "2", "2", "2"],
                 pdt_before={0: T0}, uris=[f"a{i}.ts" for i in range(4)])
    ua, sa = URL_A, [snap_a, snap_a]
    ub, sb = _plain_stream(URL_B, 5, ["2"], anchor="2024-01-01T19:04:06Z",
                           uris=["b5.ts"])
    result = intersect_streams(ua, sa, ub, sb)
    assert result == []


def test_intersect_timezone_offsets_are_normalized_to_utc():
    ua, sa = _plain_stream(URL_A, 0, ["2"], anchor="2024-01-02T03:04:00+08:00")
    ub, sb = _plain_stream(URL_B, 0, ["2"], anchor="2024-01-01T19:04:01Z")
    result = intersect_streams(ua, sa, ub, sb)
    assert len(result) == 1
    item = result[0]
    assert format_program_time(item.start_at) == "2024-01-01T19:04:01Z"
    assert format(item.offset_a, "f") == "1"
    assert format(item.offset_b, "f") == "0"


def test_intersect_self_overlapping_runs_in_one_stream_are_422():
    # Stream A: two anchored runs (across a discontinuity) whose known UTC
    # ranges overlap, so a common instant maps to two segments of A.
    snap_a = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T19:04:05Z",
        "#EXTINF:10,", "a0.ts",
        "#EXT-X-DISCONTINUITY",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T19:04:06Z",
        "#EXTINF:10,", "a1.ts",
    ]) + "\n"
    ub, sb = _plain_stream(URL_B, 0, ["10"], anchor="2024-01-01T19:04:05Z",
                           uris=["b0.ts"])
    with pytest.raises(IntersectError) as exc:
        intersect_streams(URL_A, [snap_a, snap_a], ub, sb)
    assert exc.value.code == "ambiguous_program_time"
    assert exc.value.stream == "a"
    assert exc.value.sequences == [0, 1]


def test_intersect_self_overlap_attributed_to_stream_b():
    snap_b = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T19:04:05Z",
        "#EXTINF:10,", "b0.ts",
        "#EXT-X-DISCONTINUITY",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T19:04:05Z",
        "#EXTINF:10,", "b1.ts",
    ]) + "\n"
    ua, sa = _plain_stream(URL_A, 0, ["10"], uris=["a0.ts"])
    with pytest.raises(IntersectError) as exc:
        intersect_streams(ua, sa, URL_B, [snap_b, snap_b])
    assert exc.value.stream == "b"
    assert exc.value.sequences == [0, 1]


def test_intersect_anchor_conflict_is_422_with_old_code():
    good = m3u(1, media_sequence=0, pdt_before={0: "2024-01-01T19:04:05Z"},
               uris=["a.ts"])
    bad = m3u(1, media_sequence=0, pdt_before={0: "2024-01-01T19:04:09Z"},
              uris=["a.ts"])
    ub, sb = _plain_stream(URL_B, 0, ["1"], anchor="2024-01-01T19:04:05Z",
                           uris=["b.ts"])
    with pytest.raises(IntersectError) as exc:
        intersect_streams(URL_A, [good, bad], ub, sb)
    assert exc.value.code == "program_time_conflict"
    assert exc.value.stream == "a"
    assert exc.value.snapshot_index == 1
    assert exc.value.sequence == 0


def test_intersect_merge_error_is_422_with_old_code():
    s10 = m3u(2, media_sequence=10, uris=["a10.ts", "a11.ts"])
    s9 = m3u(2, media_sequence=9, uris=["a9.ts", "a10.ts"])
    ub, sb = _plain_stream(URL_B, 0, ["2"], uris=["b.ts"])
    with pytest.raises(IntersectError) as exc:
        intersect_streams(URL_A, [s10, s9], ub, sb)
    assert exc.value.code == "sequence_rollback"
    assert exc.value.stream == "a"
    assert exc.value.snapshot_index == 1


def test_intersect_counter_reuse_runs_stay_independent_and_sorted():
    # Stream A reuses discontinuity counters across adjacent snapshots.
    # a0: seq0..1 anchored at :00 (:00..:04), break at index 2, seq2..3 in a
    # reused counter run but unanchored -> no known time.
    # a1: seq4..5 anchored at :10 (:10..:14), break at index 2, seq6..7
    # unanchored. Stream B anchored at :07 with 6s segments spanning :07..:19.
    # The unanchored A segments can never pair; B's :13 boundary falls
    # inside A5, so the pair is split there as well. Pairing order follows UTC:
    #   A4 [10,12) x B20 [07,13) -> [10,12)
    #   A5 [12,14) x B20         -> [12,13)
    #   A5 [12,14) x B21 [13,19) -> [13,14)
    a0 = m3u(4, media_sequence=0, discont_before=(2,),
             durations=["2", "2", "2", "2"],
             pdt_before={0: T0}, uris=[f"a{i}.ts" for i in range(4)])
    a1 = m3u(4, media_sequence=4, discont_before=(2,),
             durations=["2", "2", "2", "2"],
             pdt_before={0: "2024-01-01T19:04:10Z"},
             uris=[f"a{i}.ts" for i in range(4, 8)])
    ub, sb = _plain_stream(URL_B, 20, ["6", "6"], anchor="2024-01-01T19:04:07Z",
                           uris=["b20.ts", "b21.ts"])
    result = _render(intersect_streams(URL_A, [a0, a1], ub, sb))
    assert result == [
        (4, 20, "2024-01-01T19:04:10Z", "2024-01-01T19:04:12Z", "0", "3"),
        (5, 20, "2024-01-01T19:04:12Z", "2024-01-01T19:04:13Z", "0", "5"),
        (5, 21, "2024-01-01T19:04:13Z", "2024-01-01T19:04:14Z", "1", "0"),
    ]


# --------------------------------------------------- API: /v1/intersect

def intersect_post(a_url, a_snaps, b_url, b_snaps):
    return client.post(
        "/v1/intersect",
        json={
            "a": {"playlist_url": a_url, "snapshots": a_snaps},
            "b": {"playlist_url": b_url, "snapshots": b_snaps},
        },
    )


def test_api_intersect_happy_path_splits_pairs():
    snap_a = m3u(3, media_sequence=0, durations=["2", "2", "2"],
                 pdt_before={0: T0}, uris=[f"a{i}.ts" for i in range(3)])
    snap_b = m3u(3, media_sequence=10, durations=["3", "3", "3"],
                 pdt_before={0: T0}, uris=[f"b{i}.ts" for i in range(3)])
    resp = intersect_post(URL_A, [snap_a, snap_a], URL_B, [snap_b, snap_b])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "intervals": [
            {"sequence_a": 0, "sequence_b": 10,
             "start_at": "2024-01-01T19:04:00Z", "end_at": "2024-01-01T19:04:02Z",
             "offset_a": "0", "offset_b": "0"},
            {"sequence_a": 1, "sequence_b": 10,
             "start_at": "2024-01-01T19:04:02Z", "end_at": "2024-01-01T19:04:03Z",
             "offset_a": "0", "offset_b": "2"},
            {"sequence_a": 1, "sequence_b": 11,
             "start_at": "2024-01-01T19:04:03Z", "end_at": "2024-01-01T19:04:04Z",
             "offset_a": "1", "offset_b": "0"},
            {"sequence_a": 2, "sequence_b": 11,
             "start_at": "2024-01-01T19:04:04Z", "end_at": "2024-01-01T19:04:06Z",
             "offset_a": "0", "offset_b": "1"},
        ]
    }


def test_api_intersect_empty_coverage_returns_empty_list():
    snap_a = m3u(2, media_sequence=0, durations=["2", "2"],
                 pdt_before={0: T0}, uris=["a0.ts", "a1.ts"])
    snap_b = m3u(2, media_sequence=0, durations=["2", "2"],
                 pdt_before={0: "2024-01-01T20:00:00Z"}, uris=["b0.ts", "b1.ts"])
    resp = intersect_post(URL_A, [snap_a, snap_a], URL_B, [snap_b, snap_b])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"intervals": []}


def test_api_intersect_self_overlap_is_422():
    snap_a = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T19:04:05Z",
        "#EXTINF:10,", "a0.ts",
        "#EXT-X-DISCONTINUITY",
        "#EXT-X-PROGRAM-DATE-TIME:2024-01-01T19:04:06Z",
        "#EXTINF:10,", "a1.ts",
    ]) + "\n"
    snap_b = m3u(1, media_sequence=0, durations=["10"],
                 pdt_before={0: "2024-01-01T19:04:05Z"}, uris=["b0.ts"])
    resp = intersect_post(URL_A, [snap_a, snap_a], URL_B, [snap_b, snap_b])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "ambiguous_program_time"
    assert body["stream"] == "a"
    assert body["sequences"] == [0, 1]


def test_api_intersect_anchor_conflict_is_422():
    good = m3u(1, media_sequence=0, pdt_before={0: "2024-01-01T19:04:05Z"},
               uris=["x.ts"])
    bad = m3u(1, media_sequence=0, pdt_before={0: "2024-01-01T19:04:09Z"},
              uris=["x.ts"])
    snap_b = m3u(1, media_sequence=0, pdt_before={0: "2024-01-01T19:04:05Z"},
                 uris=["y.ts"])
    resp = intersect_post(URL_A, [good, bad], URL_B, [snap_b, snap_b])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "program_time_conflict"
    assert body["stream"] == "a"
    assert body["snapshot_index"] == 1
    assert body["sequence"] == 0


def test_api_intersect_merge_error_is_422_tagged_with_stream():
    s10 = m3u(1, media_sequence=10, uris=["a.ts"])
    s9 = m3u(1, media_sequence=9, uris=["a.ts"])
    snap_b = m3u(1, media_sequence=0, pdt_before={0: T0}, uris=["b.ts"])
    resp = intersect_post(URL_A, [s10, s9], URL_B, [snap_b, snap_b])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "sequence_rollback"
    assert body["stream"] == "a"
    assert body["snapshot_index"] == 1


def test_api_intersect_rejects_invalid_request_shapes():
    snap = m3u(1, media_sequence=0, pdt_before={0: T0})
    # Missing side b.
    resp = client.post("/v1/intersect",
                       json={"a": {"playlist_url": URL_A, "snapshots": [snap, snap]}})
    assert resp.status_code == 422
    # Only one snapshot per stream.
    resp = intersect_post(URL_A, [snap], URL_B, [snap])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any("snapshots" in err["loc"] for err in detail)
