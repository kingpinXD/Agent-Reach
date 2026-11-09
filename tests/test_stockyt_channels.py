# -*- coding: utf-8 -*-
"""Tests for the channels stage: window edges, reached_cutoff, time-of-day cutoffs, the sweep and its check."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from agent_reach.stockyt import cache as cache_mod
from agent_reach.stockyt import youtube
from agent_reach.stockyt.channels import (
    ChannelResult,
    Cutoff,
    check,
    list_channel_videos,
    parse_since,
    sweep,
)

CID = "UCtest"
CUTOFF = Cutoff(date(2026, 6, 5))
ET = ZoneInfo("America/New_York")


def _no_video_time(video_id):
    raise AssertionError(f"unexpected per-video check for {video_id}")


def _ids(listing):
    return [r["video_id"] for r in listing.rows]


def test_window_filter_and_bad_line_tolerance():
    lines = [
        "vid_new\t20260618\tFresh video",
        "vid_bad\tNA\tDate missing",
        "vid_mid\t20260610\tStill in window",
        "vid_old\t20260101\tWay past cutoff",
        "vid_never\t20260618\tNever read",
    ]
    listing = list_channel_videos(CID, cutoff=CUTOFF, lines=iter(lines), video_time=_no_video_time)
    assert _ids(listing) == ["vid_new", "vid_mid"]
    assert listing.reached_cutoff is True
    assert all(r["channel_id"] == CID and r["date_source"] == "listing" for r in listing.rows)


def test_listing_that_ends_inside_the_window_did_not_reach_the_cutoff():
    listing = list_channel_videos(CID, cutoff=CUTOFF, lines=iter(["a\t20260618\tA", "b\t20260610\tB"]),
                                  video_time=_no_video_time)
    assert _ids(listing) == ["a", "b"]
    assert listing.reached_cutoff is False


def test_no_usable_line_is_an_error():
    with pytest.raises(RuntimeError, match="no usable rows"):
        list_channel_videos(CID, cutoff=CUTOFF, lines=iter(["x\tNA\tbad"]))


def _asking(times):
    """A video_time that answers from `times` ((None, None) when absent) and records what it was asked."""
    asked = []

    def video_time(video_id):
        asked.append(video_id)
        return times.get(video_id, (None, None))

    return video_time, asked


def test_rows_listed_up_to_two_days_after_the_cutoff_day_are_checked_against_their_real_date():
    # The listing date is the real date or up to two days late (Stock Moe, 2026-11-09: +1 and +2).
    lines = [
        "far\t20260619\tListed D+3: real date is at least D, no check",
        "d2_early\t20260618\tListed D+2, really D-1",
        "d2_on\t20260618\tListed D+2, really D",
        "d1_early\t20260617\tListed D+1, really D-1",
        "d0_on\t20260616\tReally on the day",
        "unreadable\t20260616\tDate unreadable",
        "before\t20260615\tBefore",
    ]
    video_time, asked = _asking({
        "d2_early": ("20260615", 1), "d2_on": ("20260616", 2), "d1_early": ("20260615", 3), "d0_on": ("20260616", 4),
    })
    listing = list_channel_videos(CID, cutoff=Cutoff(date(2026, 6, 16)), lines=iter(lines), video_time=video_time)
    assert _ids(listing) == ["far", "d2_on", "d0_on", "unreadable"]
    assert asked == ["d2_early", "d2_on", "d1_early", "d0_on", "unreadable"]  # D+3 costs no call
    d2_on, unreadable = listing.rows[1], listing.rows[3]
    assert (d2_on["date_source"], d2_on["upload_date"], d2_on["timestamp"]) == ("video", "20260616", 2)
    assert "date_verified" not in d2_on
    assert unreadable["date_verified"] is False and unreadable["date_source"] == "listing"


def test_time_cutoff_checks_the_lag_days_and_keeps_rows_at_or_after_the_moment():
    at = datetime(2026, 11, 5, 16, 0, tzinfo=ET)  # Thursday 4pm Eastern
    cutoff = Cutoff(at.date(), at)
    ts = int(at.timestamp())
    lines = [
        "later\t20261108\tListed D+3: no check",
        "d2_wed\t20261107\tListed D+2, really Wednesday",
        "d2_thu_morning\t20261107\tListed D+2, really Thursday morning",
        "late_listed\t20261106\tReally Thursday evening",
        "friday\t20261106\tReally Friday",
        "thu_close\t20261105\tExactly at the close",
        "thu_morning\t20261105\tThursday morning",
        "wed\t20261104\tBefore the window",
    ]
    video_time, asked = _asking({
        "d2_wed": ("20261104", ts - 30 * 3600),
        "d2_thu_morning": ("20261105", ts - 6 * 3600),
        "late_listed": ("20261105", ts + 3600),
        "friday": ("20261106", ts + 86400),
        "thu_close": ("20261105", ts),
        "thu_morning": ("20261105", ts - 6 * 3600),
    })
    listing = list_channel_videos(CID, cutoff=cutoff, lines=iter(lines), video_time=video_time)
    assert _ids(listing) == ["later", "late_listed", "friday", "thu_close"]
    assert asked == ["d2_wed", "d2_thu_morning", "late_listed", "friday", "thu_close", "thu_morning"]
    assert listing.reached_cutoff is True


def test_time_cutoff_without_a_timestamp_falls_back_to_the_date():
    at = datetime(2026, 11, 5, 16, 0, tzinfo=ET)
    listing = list_channel_videos(CID, cutoff=Cutoff(at.date(), at), lines=iter(["a\t20261105\tA", "b\t20261101\tB"]),
                                  video_time=lambda v: ("20261105", None))
    assert _ids(listing) == ["a"]
    assert listing.rows[0]["date_verified"] is False


def test_until_caps_the_window_by_real_date():
    lines = [
        "far\t20260613\tListed until+3: skipped, no check",
        "u2_in\t20260612\tListed until+2, really until",
        "u2_out\t20260612\tListed until+2, really until+1",
        "u1_in\t20260611\tListed until+1, really until",
        "u1_out\t20260611\tListed until+1, really until+1",
        "inside\t20260609\tInside",
        "old\t20260601\tBefore cutoff",
    ]
    video_time, asked = _asking({
        "u2_in": ("20260610", 1), "u2_out": ("20260611", 2), "u1_in": ("20260610", 3), "u1_out": ("20260611", 4),
    })
    listing = list_channel_videos(CID, cutoff=CUTOFF, until="20260610", lines=iter(lines), video_time=video_time)
    assert _ids(listing) == ["u2_in", "u1_in", "inside"]
    assert asked == ["u2_in", "u2_out", "u1_in", "u1_out"]


@pytest.mark.parametrize("value, expected", [
    ("20261105", Cutoff(date(2026, 11, 5))),
    ("20261105T1600", Cutoff(date(2026, 11, 5), datetime(2026, 11, 5, 16, 0, tzinfo=ET))),
])
def test_parse_since(value, expected):
    assert parse_since(value, "America/New_York") == expected


@pytest.mark.parametrize("value", ["2026-11-05", "20261105T16", "20261305", "20261105X1600", "20261105T2500"])
def test_parse_since_rejects_bad_values(value):
    with pytest.raises(ValueError):
        parse_since(value, "America/New_York")


@pytest.fixture
def listings(monkeypatch):
    """Recorded channel listings and per-video times, through youtube.py's replay hook."""
    def install(recordings):
        run, stream = youtube.replay(recordings)
        monkeypatch.setattr(youtube, "run_ytdlp", run)
        monkeypatch.setattr(youtube, "stream_ytdlp", stream)
    return install


def test_sweep_builds_the_window_from_fresh_rows_not_the_cache(listings, tmp_path):
    # A wider earlier run cached "stale" as 20260606; a fresh listing no longer shows it in the window.
    cache_mod.merge("UCa", [{"video_id": "stale", "upload_date": "20260606", "title": "", "channel_id": "UCa"}],
                    str(tmp_path))
    listings({youtube.channel_url("UCa"): {"stdout": "new\t20260610\tNew\nold\t20260101\tOld\n"}})
    [result] = sweep([("A", "UCa")], CUTOFF, cache_dir=str(tmp_path))
    assert [r["video_id"] for r in result.rows] == ["new"]
    assert result.reached_cutoff is True and result.error is None
    assert set(cache_mod.load("UCa", str(tmp_path))) == {"stale", "new"}  # cache still merged


def test_sweep_falls_back_to_the_cache_when_a_listing_fails(listings, tmp_path):
    cache_mod.merge("UCbad", [{"video_id": "cached", "upload_date": "20260610", "title": "", "channel_id": "UCbad"}],
                    str(tmp_path))
    listings({
        youtube.channel_url("UCgood"): {"stdout": "fresh\t20260610\tF\nold\t20260101\tO\n"},
        youtube.channel_url("UCbad"): {"error": "UPSTREAM_BROKEN", "message": "yt-dlp boom"},
    })
    slept, logged = [], []
    results = sweep([("Good", "UCgood"), ("Bad", "UCbad")], CUTOFF, cache_dir=str(tmp_path), pace_seconds=3,
                    sleep=slept.append, log=lambda kind, item, dur, outcome: logged.append((kind, item, outcome)))
    good, bad = results
    assert [r["video_id"] for r in good.rows] == ["fresh"]
    assert ([r["video_id"] for r in bad.rows], bad.error) == (["cached"], "yt-dlp boom")
    assert slept == [3]  # a pause between the two channel listings
    assert logged == [("listing", "UCgood", "ok"), ("listing", "UCbad", "failed")]


def test_sweep_paces_and_logs_the_per_video_checks(listings):
    listings({
        youtube.channel_url("UCa"): {"stdout": "a\t20260605\tOn the cutoff day\nb\t20260601\tOld\n"},
        youtube.watch_url("a"): {"stdout": "20260605\t1780000000\n"},
    })
    slept, logged = [], []
    [result] = sweep([("A", "UCa")], CUTOFF, pace_seconds=1, sleep=slept.append,
                     log=lambda kind, item, dur, outcome: logged.append((kind, item, outcome)))
    assert result.rows[0]["date_source"] == "video"
    assert slept == [1]
    assert logged == [("video_time", "a", "ok"), ("listing", "UCa", "ok")]


def test_check_fails_on_an_unreached_cutoff_or_a_failed_fetch():
    ok = ChannelResult("Good", "UCg", [{"video_id": "a"}], reached_cutoff=True)
    short = ChannelResult("Short", "UCs", [], reached_cutoff=False)
    failed = ChannelResult("Bad", "UCb", [{"video_id": "c"}], reached_cutoff=False, error="boom")
    assert check([ok]) == ("done", ["Channels: 1 channels · 1 videos · 0 failed [OK]"])
    status, lines = check([ok, short, failed])
    assert status == "failed"
    assert lines == [
        "Channels: 3 channels · 2 videos · 2 failed [FAIL]",
        "❌ Short: listing ended before the window start",
        "❌ Bad: fetch failed — boom (kept 1 cached videos in window)",
    ]
