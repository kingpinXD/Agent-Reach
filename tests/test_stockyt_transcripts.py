# -*- coding: utf-8 -*-
"""Tests for the transcripts stage: one video at a time, resume, reconcile, and the gate line."""

import json

import pytest

from agent_reach.stockyt import youtube
from agent_reach.stockyt.config import Settings, apply_market
from agent_reach.stockyt.transcripts import (
    check,
    dedupe_links,
    extract_video_id,
    fetch_all,
    fetch_row,
    read_link_file,
    read_rows,
    reconcile,
)
from agent_reach.stockyt.upstream import RATE_LIMITED, ReachError

SETTINGS = Settings(pace_seconds=2.0)
STOCK_MOE = "UCoMzWLaPjDJBbipihD694pQ"


def _url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


@pytest.fixture
def captions(monkeypatch):
    """Fake youtube.fetch_captions: outcomes[video_id] is a text, or an exception to raise."""
    state = {"outcomes": {}, "calls": [], "whisper": None}

    def fake(url, langs, cookies):
        video_id = extract_video_id(url)
        state["calls"].append((video_id, langs, cookies))
        outcome = state["outcomes"].get(video_id, f"words of {video_id}")
        if isinstance(outcome, Exception):
            raise outcome
        return {"text": outcome, "lang": "en-orig", "channel": "Stock Moe", "channel_id": STOCK_MOE,
                "upload_date": "20261106", "timestamp": 1762462800}

    def fake_whisper(url):
        if isinstance(state["whisper"], Exception):
            raise state["whisper"]
        return state["whisper"]

    monkeypatch.setattr(youtube, "fetch_captions", fake)
    monkeypatch.setattr(youtube, "whisper_text", fake_whisper)
    return state


def test_ok_row_has_every_field(captions):
    assert fetch_row(_url("v1"), SETTINGS) == {
        "url": _url("v1"), "video_id": "v1", "channel": "Stock Moe", "channel_id": STOCK_MOE,
        "upload_date": "20261106", "timestamp": 1762462800, "transcript": "words of v1",
        "source": "subtitles", "lang": "en-orig", "cookies": False, "status": "ok", "error": None,
        "error_kind": None, "attempts": 1,
    }
    assert captions["calls"] == [("v1", ("en-orig", "en"), None)]


def test_cookies_are_passed_and_recorded(captions):
    row = fetch_row(_url("v1"), Settings(cookies_from_browser="chrome"))
    assert row["cookies"] is True
    assert captions["calls"][0][2] == "chrome"


def test_error_row_carries_its_kind(captions):
    captions["outcomes"]["v1"] = ReachError(RATE_LIMITED, "HTTP Error 429")
    row = fetch_row(_url("v1"), SETTINGS)
    assert (row["status"], row["error"], row["error_kind"], row["transcript"]) == (
        "error", "HTTP Error 429", "rate_limited", None)


def test_whisper_is_off_by_default(captions):
    captions["outcomes"]["v1"] = ReachError(youtube.NO_CAPTIONS, "no captions")
    captions["whisper"] = "spoken words"
    row = fetch_row(_url("v1"), SETTINGS)
    assert (row["status"], row["error_kind"]) == ("error", "no_captions")


def test_whisper_fills_in_when_on(captions):
    captions["outcomes"]["v1"] = ReachError(youtube.NO_CAPTIONS, "no captions")
    captions["whisper"] = "spoken words"
    row = fetch_row(_url("v1"), Settings(whisper=True))
    assert (row["status"], row["source"], row["transcript"]) == ("ok", "whisper", "spoken words")
    assert (row["error"], row["error_kind"]) == (None, None)


def test_whisper_failure_reports_both_errors(captions):
    captions["outcomes"]["v1"] = ReachError(youtube.NO_CAPTIONS, "no captions")
    captions["whisper"] = ReachError("NEED_SETUP", "no Whisper API key configured")
    row = fetch_row(_url("v1"), Settings(whisper=True))
    assert row["error"] == "captions: no captions; whisper: no Whisper API key configured"


def test_a_non_youtube_link_is_an_error_row_without_a_call(captions):
    row = fetch_row("https://example.com/x", SETTINGS)
    assert (row["status"], row["error_kind"]) == ("error", "other")
    assert captions["calls"] == []


def test_fetch_all_dedupes_and_pauses_between_videos(captions, tmp_path):
    out = tmp_path / "t.jsonl"
    slept = []
    links = [_url("v1"), "https://youtu.be/v1", _url("v2")]
    rows = fetch_all(links, str(out), SETTINGS, sleep=slept.append)
    assert [r["video_id"] for r in rows] == ["v1", "v2"]
    assert slept == [2.0]  # one pause, between the two calls
    assert [json.loads(x)["video_id"] for x in out.read_text().splitlines()] == ["v1", "v2"]


def test_fetch_all_resumes_skipping_ok_rows_and_retrying_errors(captions, tmp_path):
    out = tmp_path / "t.jsonl"
    captions["outcomes"]["v2"] = ReachError("UPSTREAM_BROKEN", "boom")
    fetch_all([_url("v1"), _url("v2")], str(out), SETTINGS, sleep=lambda s: None)

    captions["calls"].clear()
    captions["outcomes"].clear()
    rows = fetch_all([_url("v1"), _url("v2")], str(out), SETTINGS, sleep=lambda s: None)
    assert [c[0] for c in captions["calls"]] == ["v2"]  # v1 was already ok
    assert [(r["video_id"], r["status"], r["attempts"]) for r in rows] == [("v1", "ok", 1), ("v2", "ok", 2)]
    assert len(out.read_text().splitlines()) == 2  # compacted to one row per video


def test_fetch_all_stops_at_the_first_rate_limit(captions, tmp_path):
    captions["outcomes"]["v2"] = ReachError(RATE_LIMITED, "429")
    logged = []
    rows = fetch_all([_url("v1"), _url("v2"), _url("v3")], str(tmp_path / "t.jsonl"), SETTINGS,
                     sleep=lambda s: None, log=lambda kind, vid, dur, outcome: logged.append((kind, vid, outcome)))
    assert [r["video_id"] for r in rows] == ["v1", "v2"]
    assert logged == [("captions", "v1", "ok"), ("captions", "v2", "rate_limited")]


# ── India: caption tracks by list, on recorded yt-dlp output ──

INDIA = apply_market(Settings(pace_seconds=0), "india")
SOIC = "UCB7GnQlJPIL6rBBqEoX87vA"


@pytest.fixture
def ytdlp(monkeypatch):
    """Swap youtube.run_ytdlp; `install(recordings)` answers from them, and `calls` logs each --sub-langs."""
    state: dict = {"calls": []}

    def install(recordings):
        run, _ = youtube.replay(recordings)

        def logged(args, timeout, cwd=None):
            state["calls"].append(args[args.index("--sub-langs") + 1])
            return run(args, timeout, cwd)

        monkeypatch.setattr(youtube, "run_ytdlp", logged)

    state["install"] = install
    return state


def test_india_takes_the_hindi_track_and_never_asks_for_translated_english(ytdlp):
    vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nरिलायंस में खरीदारी करें\n"
    ytdlp["install"]({_url("h1"): {"stdout": f"SOIC\t{SOIC}\t20261106\t1762462800\n",
                                   "files": {"h1.en.vtt": "WEBVTT\n\nmachine translated\n", "h1.hi-orig.vtt": vtt}}})
    row = fetch_row(_url("h1"), INDIA)
    assert (row["status"], row["lang"], row["transcript"], row["channel"]) == (
        "ok", "hi-orig", "रिलायंस में खरीदारी करें", "SOIC")
    assert ytdlp["calls"] == ["en-orig", "hi-orig"]


def test_a_members_only_video_is_an_error_row_and_the_stage_goes_on(ytdlp, tmp_path, monkeypatch):
    vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nwords\n"
    ytdlp["install"]({_url("ok1"): {"stdout": f"SOIC\t{SOIC}\t20261106\t1762462800\n",
                                    "files": {"ok1.en-orig.vtt": vtt}}})
    replayed = youtube.run_ytdlp

    def members_only(args, timeout, cwd=None):
        if args[-1] == _url("m1"):
            raise RuntimeError("Join this channel to get access to members-only content")
        return replayed(args, timeout, cwd)

    monkeypatch.setattr(youtube, "run_ytdlp", members_only)
    links = [_url("m1"), _url("ok1")]
    rows = fetch_all(links, str(tmp_path / "t.jsonl"), INDIA)
    assert [(r["video_id"], r["status"], r["error_kind"]) for r in rows] == [
        ("m1", "error", "other"), ("ok1", "ok", None)]
    assert check(links, rows)[0] == "done"


def test_read_rows_keeps_the_latest_row_per_video(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps({"url": _url("v1"), "video_id": "v1", "status": "error"}) + "\n"
        + json.dumps({"url": _url("v2"), "video_id": "v2", "status": "ok"}) + "\n"
        + json.dumps({"url": _url("v1"), "video_id": "v1", "status": "ok"}) + "\n",
        encoding="utf-8",
    )
    assert [(r["video_id"], r["status"]) for r in read_rows(str(path))] == [("v2", "ok"), ("v1", "ok")]
    assert read_rows(str(tmp_path / "missing.jsonl")) == []


def _row(video_id, status="ok", kind=None):
    return {"url": _url(video_id), "video_id": video_id, "status": status, "error_kind": kind}


def test_reconcile_counts():
    links = [_url(v) for v in ("a", "b", "c", "d", "e")] + [_url("a")]
    rows = [_row("a"), _row("b"), _row("c", "error", "no_captions"), _row("d", "error", "rate_limited"),
            _row("zz")]  # a row for a video not in the links does not count
    assert reconcile(links, rows) == {"n": 5, "ok": 2, "error": 2, "rate_limited": 1, "missing": 1}


def test_check_ok_when_every_video_has_a_row():
    status, lines = check([_url("a"), _url("b")], [_row("a"), _row("b", "error", "no_captions")])
    assert status == "done"
    assert lines == ["Transcripts: 2 links · 1 ok · 1 error (0 rate_limited) · 0 missing [OK]"]


def test_check_fails_on_a_missing_row():
    status, lines = check([_url("a"), _url("b")], [_row("a")])
    assert status == "failed"
    assert lines[0] == "Transcripts: 2 links · 1 ok · 0 error (0 rate_limited) · 1 missing [FAIL]"


def test_check_is_blocked_on_a_rate_limit_even_with_missing_rows():
    status, lines = check([_url("a"), _url("b")], [_row("a", "error", "rate_limited")])
    assert status == "blocked"
    assert "--cookies-from-browser" in lines[1]


def test_dedupe_links_keeps_first_per_video():
    assert dedupe_links([_url("a"), "https://youtu.be/a", _url("b"), "x", "x"]) == [_url("a"), _url("b"), "x"]


def test_extract_video_id():
    assert extract_video_id("https://www.youtube.com/watch?v=abc123") == "abc123"
    assert extract_video_id("https://youtu.be/xyz789") == "xyz789"
    assert extract_video_id("https://www.youtube.com/shorts/M6gat-hai0I") == "M6gat-hai0I"
    assert extract_video_id("https://example.com/foo") is None


def test_link_file_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "links.txt"
    f.write_text("https://youtu.be/a\n\n# a comment\n  \nhttps://youtu.be/b\n", encoding="utf-8")
    assert read_link_file(str(f)) == ["https://youtu.be/a", "https://youtu.be/b"]
