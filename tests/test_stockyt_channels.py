# -*- coding: utf-8 -*-
"""Tests for stockyt channel listing (early-stop, window filter, bad-line tolerance)."""

from argparse import Namespace
from datetime import date

from agent_reach import cli
from agent_reach.stockyt import cache as cache_mod
from agent_reach.stockyt import channels as channels_mod
from agent_reach.stockyt import config as stockyt_config
from agent_reach.stockyt.channels import list_channel_videos

CID = "UCtest"
TODAY = date(2026, 6, 19)  # cutoff for weeks=2 → 20260605

# Newest-first, as yt-dlp prints. Includes a bad line and an old (out-of-window) line.
LINES = [
    "vid_new\t20260618\tFresh video",
    "vid_bad\tNA\tDate missing",            # tolerated, skipped
    "vid_mid\t20260610\tStill in window",
    "vid_cached\t20260607\tAlready cached",  # cache hit → stop here
    "vid_old\t20260101\tWay past cutoff",    # never reached
]


def test_window_filter_and_bad_line_tolerance():
    rows = list_channel_videos(CID, weeks=2, today=TODAY, _lines=iter(LINES))
    ids = [r["video_id"] for r in rows]
    # bad line skipped, old line excluded by cutoff
    assert ids == ["vid_new", "vid_mid", "vid_cached"]
    assert all(r["channel_id"] == CID for r in rows)


def test_early_stop_on_cache_hit():
    cache = {"vid_cached": {"video_id": "vid_cached"}}
    rows = list_channel_videos(CID, weeks=2, today=TODAY, cache=cache, _lines=iter(LINES))
    # stops the moment it reaches the cached id — older lines never collected
    assert [r["video_id"] for r in rows] == ["vid_new", "vid_mid"]


def test_early_stop_on_cutoff():
    lines = [
        "a\t20260618\tIn",
        "b\t20260601\tOut (before cutoff)",  # < 20260605 → stop
        "c\t20260617\tNever reached",
    ]
    rows = list_channel_videos(CID, weeks=2, today=TODAY, _lines=iter(lines))
    assert [r["video_id"] for r in rows] == ["a"]


def test_channels_sweep_survives_one_failure(tmp_path, monkeypatch, capsys):
    good_id, bad_id = "UCgood", "UCbad"
    monkeypatch.setattr(stockyt_config, "CHANNELS", [("Good", good_id), ("Bad", bad_id)])

    # Pre-seed the failing channel's cache so it has a fallback window.
    cached = {"video_id": "cachedvid", "upload_date": "20260618", "title": "old", "channel_id": bad_id}
    cache_mod.merge(bad_id, [cached], str(tmp_path))

    def fake_list(channel_id, *, weeks, cache=None, today=None):
        if channel_id == bad_id:
            raise RuntimeError("yt-dlp boom")
        return [{"video_id": "freshvid", "upload_date": "20260618", "title": "new", "channel_id": good_id}]

    monkeypatch.setattr(channels_mod, "list_channel_videos", fake_list)

    out_file = tmp_path / "urls.txt"
    args = Namespace(weeks=2, output=str(out_file), cache=str(tmp_path),
                     channels_json=None, no_cache=False)
    cli._cmd_channels(args)

    captured = capsys.readouterr().out
    # sweep completed past the failure: both channels reported
    assert "Good: 1 videos" in captured
    assert "⚠️ Bad: fetch failed — yt-dlp boom (kept 1 cached videos in window)" in captured
    # good channel's fresh video + bad channel's cached fallback both emitted
    urls = out_file.read_text().split()
    assert "https://www.youtube.com/watch?v=freshvid" in urls
    assert "https://www.youtube.com/watch?v=cachedvid" in urls
    # integrity holds across the actually-emitted videos
    assert "Integrity: 2 URLs == 2 summed per-channel counts (ok)" in captured
