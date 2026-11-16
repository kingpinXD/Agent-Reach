# -*- coding: utf-8 -*-
"""Tests for stockyt's yt-dlp calls, fed from recorded output through the replay hook."""

import pytest

from agent_reach.stockyt import youtube
from agent_reach.stockyt.upstream import (
    NOT_FOUND,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    TIMEOUT,
    ReachError,
)

VID = "abcdefghijk"
URL = youtube.watch_url(VID)
PRINTED = "Stock Moe\tUCoMzWLaPjDJBbipihD694pQ\t20261106\t1762462800\n"


def _vtt(text):
    return f"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n{text}\n\n00:00:02.000 --> 00:00:04.000\nsecond line\n"


@pytest.fixture
def replay(monkeypatch):
    """Install recordings: replay({url: recording}) swaps both yt-dlp hooks and records argv."""
    calls = []

    def install(recordings):
        run, stream = youtube.replay(recordings)

        def recording_run(args, timeout, cwd=None):
            calls.append(args)
            return run(args, timeout, cwd)

        monkeypatch.setattr(youtube, "run_ytdlp", recording_run)
        monkeypatch.setattr(youtube, "stream_ytdlp", stream)
        return calls

    return install


def test_fetch_captions_prefers_en_orig_and_reads_the_printed_fields(replay):
    calls = replay({URL: {"stdout": PRINTED, "files": {
        f"{VID}.en.vtt": _vtt("translated words"),
        f"{VID}.en-orig.vtt": _vtt("original words"),
    }}})
    assert youtube.fetch_captions(URL) == {
        "text": "original words second line",
        "lang": "en-orig",
        "channel": "Stock Moe",
        "channel_id": "UCoMzWLaPjDJBbipihD694pQ",
        "upload_date": "20261106",
        "timestamp": 1762462800,
    }
    assert len(calls) == 1  # en-orig exists, so en is never asked for
    args = calls[0]
    assert _sub_langs(args) == "en-orig"
    assert "--write-subs" in args and "--write-auto-subs" in args
    assert "--cookies-from-browser" not in args


def _sub_langs(args):
    return args[args.index("--sub-langs") + 1]


def test_fetch_captions_falls_back_to_en_when_there_is_no_en_orig_track(replay):
    calls = replay({URL: {"stdout": PRINTED, "files": {f"{VID}.en.vtt": _vtt("plain")}}})
    row = youtube.fetch_captions(URL)
    assert (row["lang"], row["text"]) == ("en", "plain second line")
    assert (row["channel"], row["timestamp"]) == ("Stock Moe", 1762462800)
    assert [_sub_langs(args) for args in calls] == ["en-orig", "en"]


def test_a_rate_limited_en_orig_call_does_not_try_en(replay):
    # YouTube 429s the auto-translated en track while en-orig downloads fine, so one language per call.
    calls = replay({URL: {"error": RATE_LIMITED, "message": "Unable to download video subtitles: HTTP Error 429"}})
    with pytest.raises(ReachError) as exc:
        youtube.fetch_captions(URL)
    assert youtube.error_kind(exc.value) == "rate_limited"
    assert [_sub_langs(args) for args in calls] == ["en-orig"]


def test_fetch_captions_passes_cookies_only_when_asked(replay):
    calls = replay({URL: {"stdout": PRINTED, "files": {f"{VID}.en.vtt": _vtt("x")}}})
    youtube.fetch_captions(URL, cookies_from_browser="chrome")
    assert calls[0][calls[0].index("--cookies-from-browser") + 1] == "chrome"


def test_no_caption_track_is_no_captions(replay):
    replay({URL: {"stdout": PRINTED, "files": {f"{VID}.de.vtt": _vtt("deutsch")}}})
    with pytest.raises(ReachError) as exc:
        youtube.fetch_captions(URL)
    assert exc.value.code == youtube.NO_CAPTIONS
    assert youtube.error_kind(exc.value) == "no_captions"


def test_na_fields_read_as_none(replay):
    replay({URL: {"stdout": "NA\tNA\tNA\tNA\n", "files": {f"{VID}.en.vtt": _vtt("x")}}})
    row = youtube.fetch_captions(URL)
    assert (row["channel"], row["channel_id"], row["upload_date"], row["timestamp"]) == (None, None, None, None)


def test_rate_limit_propagates_as_reach_error(replay):
    replay({URL: {"error": RATE_LIMITED, "message": "HTTP Error 429: Too Many Requests"}})
    with pytest.raises(ReachError) as exc:
        youtube.fetch_captions(URL)
    assert youtube.error_kind(exc.value) == "rate_limited"


@pytest.mark.parametrize("exc, kind", [
    (ReachError(RATE_LIMITED, "429"), "rate_limited"),
    (ReachError(PLATFORM_BLOCKED, "sign in to confirm you're not a bot"), "rate_limited"),
    (ReachError(youtube.NO_CAPTIONS, "none"), "no_captions"),
    (ReachError(NOT_FOUND, "video unavailable"), "unavailable"),
    (ReachError(TIMEOUT, "slow"), "other"),
    (RuntimeError("boom"), "other"),
])
def test_error_kind(exc, kind):
    assert youtube.error_kind(exc) == kind


def test_video_time(replay):
    replay({URL: {"stdout": "20261105\t1762380000\n"}})
    assert youtube.video_time(VID) == ("20261105", 1762380000)


def test_video_time_failure_is_none(replay):
    replay({URL: {"error": RATE_LIMITED, "message": "429"}})
    assert youtube.video_time(VID) == (None, None)


def test_list_channel_streams_the_recorded_lines(replay):
    replay({youtube.channel_url("UCx"): {"stdout": "a\t20261107\tA\nb\t20261106\tB\n"}})
    assert list(youtube.list_channel("UCx")) == ["a\t20261107\tA", "b\t20261106\tB"]


def test_replay_without_a_recording_is_loud(replay):
    replay({})
    with pytest.raises(KeyError, match="no recorded yt-dlp output"):
        youtube.fetch_captions(URL)


def test_pacer_sleeps_between_calls_not_before_the_first():
    slept = []
    wait = youtube.pacer(3.0, sleep=slept.append)
    for _ in range(3):
        wait()
    assert slept == [3.0, 3.0]


def test_caption_langs():
    assert youtube.caption_langs("en") == ("en-orig", "en")
