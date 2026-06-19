# -*- coding: utf-8 -*-
"""Tests for the per-channel JSONL cache (lossless union, window filter, round-trip)."""

from agent_reach.stockyt import cache as cache_mod

CID = "UCtest"


def _row(vid, d):
    return {"video_id": vid, "upload_date": d, "title": f"t-{vid}", "channel_id": CID}


def test_merge_is_lossless_union(tmp_path):
    first = [_row("a", "20260601"), _row("b", "20260610")]
    added, total = cache_mod.merge(CID, first, str(tmp_path))
    assert (added, total) == (2, 2)

    # Overlapping batch: "b" repeats, "c" is new — existing rows must survive.
    second = [_row("b", "20260610"), _row("c", "20260615")]
    added, total = cache_mod.merge(CID, second, str(tmp_path))
    assert (added, total) == (1, 3)

    loaded = cache_mod.load(CID, str(tmp_path))
    assert set(loaded) == {"a", "b", "c"}


def test_round_trip_and_sort_desc(tmp_path):
    cache_mod.merge(CID, [_row("a", "20260601"), _row("c", "20260615"), _row("b", "20260610")], str(tmp_path))
    loaded = cache_mod.load(CID, str(tmp_path))
    assert loaded["a"]["title"] == "t-a"
    # file is written newest-first
    path = tmp_path / f"{CID}.jsonl"
    dates = [line.split('"upload_date": "')[1][:8] for line in path.read_text().splitlines()]
    assert dates == sorted(dates, reverse=True)


def test_all_in_window_filters_by_cutoff(tmp_path):
    cache_mod.merge(CID, [_row("a", "20260601"), _row("b", "20260610"), _row("c", "20260615")], str(tmp_path))
    window = cache_mod.all_in_window(CID, "20260605", str(tmp_path))
    assert [r["video_id"] for r in window] == ["c", "b"]  # newest first, "a" excluded


def test_load_missing_returns_empty(tmp_path):
    assert cache_mod.load("nope", str(tmp_path)) == {}
