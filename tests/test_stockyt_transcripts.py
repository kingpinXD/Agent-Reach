# -*- coding: utf-8 -*-
"""Tests for stockyt transcript records (captions → Whisper fallback, never raises)."""

import pytest

from agent_reach.channels.youtube import YouTubeChannel
from agent_reach.runtime.result import NOT_FOUND, ReachError
from agent_reach.stockyt.transcripts import extract_video_id, fetch_record, read_link_file

URL = "https://www.youtube.com/watch?v=abc123"


@pytest.fixture
def fake_youtube(monkeypatch):
    """Patch the three yt-dlp/Whisper backends; tests set what each returns or raises."""
    state = {
        "read": [{"author": "Some Channel", "raw": {"channel_id": "UC123"}}],
        "subtitle": [{"text": "line one\nline two"}],
        "whisper": [{"text": "whisper text"}],
        "calls": [],
    }

    def backend(name):
        def call(self, **kwargs):
            state["calls"].append(name)
            value = state[name]
            if isinstance(value, Exception):
                raise value
            return value
        return call

    for name, attr in (("read", "_ytdlp_read"), ("subtitle", "_ytdlp_subtitle"), ("whisper", "_whisper_transcribe")):
        monkeypatch.setattr(YouTubeChannel, attr, backend(name))
    return state


def test_captions_make_an_ok_record(fake_youtube):
    assert fetch_record(URL) == {
        "url": URL,
        "video_id": "abc123",
        "channel": "Some Channel",
        "channel_id": "UC123",
        "transcript": "line one line two",
        "source": "subtitles",
        "status": "ok",
    }
    assert "whisper" not in fake_youtube["calls"]


def test_falls_back_to_whisper_when_no_captions(fake_youtube):
    fake_youtube["subtitle"] = ReachError(NOT_FOUND, "no subtitles in en for this video")
    record = fetch_record(URL)
    assert (record["status"], record["source"], record["transcript"]) == ("ok", "whisper", "whisper text")


def test_no_whisper_gives_error_record(fake_youtube):
    fake_youtube["subtitle"] = ReachError(NOT_FOUND, "no subtitles in en for this video")
    record = fetch_record(URL, allow_whisper=False)
    assert record["status"] == "error"
    assert record["error"] == "no subtitles in en for this video"
    assert record["transcript"] is None and record["source"] is None
    assert "whisper" not in fake_youtube["calls"]


def test_whisper_failure_gives_error_record(fake_youtube):
    fake_youtube["subtitle"] = ReachError(NOT_FOUND, "no subtitles")
    fake_youtube["whisper"] = ReachError("NEED_SETUP", "no Whisper API key configured")
    record = fetch_record(URL)
    assert record["status"] == "error"
    assert record["error"] == "captions: no subtitles; whisper: no Whisper API key configured"
    assert record["channel"] == "Some Channel"  # channel rides along on error records


def test_metadata_failure_keeps_record_ok(fake_youtube):
    fake_youtube["read"] = RuntimeError("metadata boom")
    record = fetch_record(URL)
    assert record["status"] == "ok"
    assert (record["channel"], record["channel_id"]) == (None, None)


def test_extract_video_id():
    assert extract_video_id("https://www.youtube.com/watch?v=abc123") == "abc123"
    assert extract_video_id("https://youtu.be/xyz789") == "xyz789"
    assert extract_video_id("https://www.youtube.com/shorts/M6gat-hai0I") == "M6gat-hai0I"
    assert extract_video_id("https://example.com/foo") is None


def test_link_file_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "links.txt"
    f.write_text("https://youtu.be/a\n\n# a comment\n  \nhttps://youtu.be/b\n", encoding="utf-8")
    assert read_link_file(str(f)) == ["https://youtu.be/a", "https://youtu.be/b"]
