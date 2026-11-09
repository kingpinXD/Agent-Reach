# -*- coding: utf-8 -*-
"""Tests for `agent-reach stockyt` command wiring and the channels / transcripts handlers."""

import json
from datetime import date
from unittest.mock import patch

import pytest

from agent_reach.runtime import cli as runtime_cli
from agent_reach.stockyt import cache as cache_mod
from agent_reach.stockyt import channels as channels_mod
from agent_reach.stockyt import config as stockyt_config
from agent_reach.stockyt.cli import main


def test_runtime_routes_stockyt_to_its_cli():
    with patch("agent_reach.stockyt.cli.main", return_value=0) as stockyt_main:
        assert runtime_cli.main(["stockyt", "grade", "--help"]) == 0
    stockyt_main.assert_called_once_with(["grade", "--help"])


def test_unknown_subcommand_is_an_argparse_error():
    with pytest.raises(SystemExit) as exc:
        main(["nope"])
    assert exc.value.code == 2


def test_transcripts_writes_jsonl_and_keeps_going_on_error(tmp_path, capsys):
    links = tmp_path / "links.txt"
    links.write_text("https://youtu.be/good\nhttps://youtu.be/bad\n", encoding="utf-8")
    out = tmp_path / "out.jsonl"

    def fake_fetch(url, *, lang, allow_whisper):
        status = "ok" if url.endswith("good") else "error"
        return {"url": url, "status": status, "lang": lang, "allow_whisper": allow_whisper}

    with patch("agent_reach.stockyt.transcripts.fetch_record", side_effect=fake_fetch):
        assert main(["transcripts", "--file", str(links), "-o", str(out), "--no-whisper"]) == 0

    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["url"] for r in records] == ["https://youtu.be/good", "https://youtu.be/bad"]
    assert all(r["lang"] == "en" and r["allow_whisper"] is False for r in records)
    assert "1 ok / 1 errors" in capsys.readouterr().err


def test_transcripts_requires_exactly_one_input(tmp_path):
    assert main(["transcripts"]) == 1
    assert main(["transcripts", "https://youtu.be/a", "--file", str(tmp_path / "x.txt")]) == 1


def test_channels_sweep_survives_one_failure(tmp_path, monkeypatch, capsys):
    good_id, bad_id = "UCgood", "UCbad"
    monkeypatch.setattr(stockyt_config, "CHANNELS", [("Good", good_id), ("Bad", bad_id)])

    # Dates are relative to the real today (the CLI derives the window cutoff from
    # date.today()), so the fixtures stay in-window whenever the suite runs.
    recent = date.today().strftime("%Y%m%d")

    # Pre-seed the failing channel's cache so it has a fallback window.
    cached = {"video_id": "cachedvid", "upload_date": recent, "title": "old", "channel_id": bad_id}
    cache_mod.merge(bad_id, [cached], str(tmp_path))

    def fake_list(channel_id, *, weeks, cache=None, today=None):
        if channel_id == bad_id:
            raise RuntimeError("yt-dlp boom")
        return [{"video_id": "freshvid", "upload_date": recent, "title": "new", "channel_id": good_id}]

    monkeypatch.setattr(channels_mod, "list_channel_videos", fake_list)

    out_file = tmp_path / "urls.txt"
    assert main(["channels", "--weeks", "2", "-o", str(out_file), "--cache", str(tmp_path)]) == 0

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
