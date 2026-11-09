# -*- coding: utf-8 -*-
"""Tests for stockyt channel listing (early-stop, window filter, bad-line tolerance)."""

from datetime import date

from agent_reach.stockyt.channels import list_channel_videos

CID = "UCtest"
CUTOFF = date(2026, 6, 5)

# Newest-first, as yt-dlp prints. Includes a bad line and an old (out-of-window) line.
LINES = [
    "vid_new\t20260618\tFresh video",
    "vid_bad\tNA\tDate missing",            # tolerated, skipped
    "vid_mid\t20260610\tStill in window",
    "vid_cached\t20260607\tAlready cached",  # cache hit → stop here
    "vid_old\t20260101\tWay past cutoff",    # never reached
]


def test_window_filter_and_bad_line_tolerance():
    rows = list_channel_videos(CID, cutoff=CUTOFF, _lines=iter(LINES))
    ids = [r["video_id"] for r in rows]
    # bad line skipped, old line excluded by cutoff
    assert ids == ["vid_new", "vid_mid", "vid_cached"]
    assert all(r["channel_id"] == CID for r in rows)


def test_early_stop_on_cache_hit():
    # Fast path: cache floor (20260101) is OLDER than the cutoff (20260605), so the
    # cache already reaches back past the window — the cached-id stop is safe.
    cache = {
        "vid_cached": {"video_id": "vid_cached", "upload_date": "20260607"},
        "vid_floor": {"video_id": "vid_floor", "upload_date": "20260101"},
    }
    lines = [
        "vid_new\t20260618\tFresh",
        "vid_mid\t20260610\tIn window",
        "vid_cached\t20260607\tCached hit → stop",
        "vid_extra\t20260606\tStill in window but never reached",  # not over-fetched
    ]
    rows = list_channel_videos(CID, cutoff=CUTOFF, cache=cache, _lines=iter(lines))
    # stops the moment it reaches the cached id — the in-window extra is not collected
    assert [r["video_id"] for r in rows] == ["vid_new", "vid_mid"]


def test_grown_window_backfills_past_cached_ids():
    # Cache was seeded from a ~1-week window (floor 20260615), now a 3-week window is
    # requested (cutoff 20260529). cache_floor is NEWER than the cutoff, so the
    # cached-id stop must be suppressed and the older in-window videos backfilled.
    cache = {
        "r1": {"video_id": "r1", "upload_date": "20260618"},
        "r2": {"video_id": "r2", "upload_date": "20260615"},
    }
    lines = [
        "r1\t20260618\tCached recent",
        "r2\t20260615\tCached recent",
        "older1\t20260603\tOlder but still in 3w window",
        "older2\t20260530\tOlder but still in 3w window",
        "past\t20260520\tPast the cutoff → stop",
    ]
    rows = list_channel_videos(CID, cutoff=date(2026, 5, 29), cache=cache, _lines=iter(lines))
    ids = [r["video_id"] for r in rows]
    # did NOT stop at the first cached id — backfilled the older in-window videos
    assert "older1" in ids and "older2" in ids
    # stopped at the cutoff — the past-cutoff video was excluded
    assert "past" not in ids


def test_early_stop_on_cutoff():
    lines = [
        "a\t20260618\tIn",
        "b\t20260601\tOut (before cutoff)",  # < 20260605 → stop
        "c\t20260617\tNever reached",
    ]
    rows = list_channel_videos(CID, cutoff=CUTOFF, _lines=iter(lines))
    assert [r["video_id"] for r in rows] == ["a"]


def test_cutoff_day_is_kept_and_the_day_before_is_not():
    lines = ["a\t20260617\tAfter", "b\t20260616\tOn the cutoff day", "c\t20260615\tDay before"]
    rows = list_channel_videos(CID, cutoff=date(2026, 6, 16), _lines=iter(lines))
    assert [r["video_id"] for r in rows] == ["a", "b"]
