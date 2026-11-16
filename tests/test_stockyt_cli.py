# -*- coding: utf-8 -*-
"""Tests for `agent-reach stockyt` command wiring and the channels / transcripts / split-corpus handlers."""

import json
import os
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from agent_reach.runtime import cli as runtime_cli
from agent_reach.stockyt import cache as cache_mod
from agent_reach.stockyt import channels as channels_mod
from agent_reach.stockyt import config as stockyt_config
from agent_reach.stockyt import youtube
from agent_reach.stockyt.channels import Cutoff
from agent_reach.stockyt.cli import main
from agent_reach.stockyt.upstream import RATE_LIMITED, ReachError


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    """A config file with no pause between calls and outputs under tmp_path."""
    path = tmp_path / "config.toml"
    out = tmp_path / "out"  # TOML literal string: Windows backslashes are not escapes
    path.write_text(f"pace_seconds = 0\noutput_dir = '{out}'\n", encoding="utf-8")
    monkeypatch.setattr(stockyt_config, "DEFAULT_CONFIG_PATH", str(path))
    return path


def test_runtime_routes_stockyt_to_its_cli():
    with patch("agent_reach.stockyt.cli.main", return_value=0) as stockyt_main:
        assert runtime_cli.main(["stockyt", "grade", "--help"]) == 0
    stockyt_main.assert_called_once_with(["grade", "--help"])


def test_unknown_subcommand_is_an_argparse_error():
    with pytest.raises(SystemExit) as exc:
        main(["nope"])
    assert exc.value.code == 2


def _url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


@pytest.fixture
def captions(monkeypatch):
    """Fake youtube.fetch_captions: outcomes[url] is an exception to raise; anything else is ok."""
    state = {"outcomes": {}, "calls": []}

    def fake(url, langs, cookies):
        state["calls"].append((url, langs, cookies))
        outcome = state["outcomes"].get(url)
        if isinstance(outcome, Exception):
            raise outcome
        return {"text": "words", "lang": langs[0], "channel": "C", "channel_id": "UCc",
                "upload_date": "20261106", "timestamp": 1}

    monkeypatch.setattr(youtube, "fetch_captions", fake)
    return state


def _write_links(tmp_path, *video_ids):
    links = tmp_path / "links.txt"
    links.write_text("".join(_url(v) + "\n" for v in video_ids), encoding="utf-8")
    return links


def test_transcripts_writes_jsonl_and_keeps_going_on_error(tmp_path, capsys, captions):
    captions["outcomes"][_url("bad")] = ReachError(youtube.NO_CAPTIONS, "no captions")
    out = tmp_path / "out.jsonl"
    links = _write_links(tmp_path, "good", "bad")
    assert main(["transcripts", "--file", str(links), "-o", str(out), "--lang", "de",
                 "--cookies-from-browser", "chrome"]) == 0

    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [(r["video_id"], r["status"], r["error_kind"]) for r in records] == [
        ("good", "ok", None), ("bad", "error", "no_captions")]
    assert all(call[1:] == (("de-orig", "de"), "chrome") for call in captions["calls"])
    assert "Transcripts: 2 links · 1 ok · 1 error (0 rate_limited) · 0 missing [OK]" in capsys.readouterr().err


def test_transcripts_exits_2_on_a_rate_limit(tmp_path, capsys, captions):
    captions["outcomes"][_url("b")] = ReachError(RATE_LIMITED, "HTTP Error 429")
    links = _write_links(tmp_path, "a", "b", "c")
    assert main(["transcripts", "--file", str(links), "-o", str(tmp_path / "out.jsonl")]) == 2
    err = capsys.readouterr().err
    assert "Transcripts: 3 links · 1 ok · 1 error (1 rate_limited) · 1 missing [FAIL]" in err
    assert "--cookies-from-browser" in err


def test_transcripts_exits_1_on_a_missing_row(tmp_path, monkeypatch, capsys, captions):
    from agent_reach.stockyt import transcripts

    monkeypatch.setattr(transcripts, "fetch_all", lambda links, out, settings: [])
    links = _write_links(tmp_path, "a")
    assert main(["transcripts", "--file", str(links), "-o", str(tmp_path / "out.jsonl")]) == 1
    assert "0 ok · 0 error (0 rate_limited) · 1 missing [FAIL]" in capsys.readouterr().err


def test_transcripts_single_url_prints_the_row(capsys, captions):
    assert main(["transcripts", _url("a")]) == 0
    row = json.loads(capsys.readouterr().out)
    assert (row["video_id"], row["status"]) == ("a", "ok")


def test_transcripts_requires_exactly_one_input(tmp_path):
    assert main(["transcripts"]) == 1
    assert main(["transcripts", "https://youtu.be/a", "--file", str(tmp_path / "x.txt")]) == 1


def _listing(channel_id, rows, *, reached=True):
    return channels_mod.Listing([{"video_id": v, "upload_date": d, "title": "", "channel_id": channel_id,
                                  "date_source": "listing"} for v, d in rows], reached)


def test_channels_sweep_survives_one_failure_and_exits_1(tmp_path, monkeypatch, capsys):
    good_id, bad_id = "UCgood", "UCbad"
    monkeypatch.setattr(stockyt_config, "CHANNELS", [("Good", good_id), ("Bad", bad_id)])

    # Dates are relative to the real today (the CLI derives the window cutoff from
    # date.today()), so the fixtures stay in-window whenever the suite runs.
    recent = date.today().strftime("%Y%m%d")

    # Pre-seed the failing channel's cache so it has a fallback window.
    cached = {"video_id": "cachedvid", "upload_date": recent, "title": "old", "channel_id": bad_id}
    cache_mod.merge(bad_id, [cached], str(tmp_path))

    def fake_list(channel_id, *, cutoff, until=None, video_time=None):
        if channel_id == bad_id:
            raise RuntimeError("yt-dlp boom")
        return _listing(good_id, [("freshvid", recent)])

    monkeypatch.setattr(channels_mod, "list_channel_videos", fake_list)

    out_file = tmp_path / "urls.txt"
    assert main(["channels", "--weeks", "2", "-o", str(out_file), "--cache", str(tmp_path)]) == 1

    captured = capsys.readouterr().out
    assert "Good: 1 videos" in captured
    assert "❌ Bad: fetch failed — yt-dlp boom (kept 1 cached videos in window)" in captured
    assert "Channels: 2 channels · 2 videos · 1 failed [FAIL]" in captured
    urls = out_file.read_text().split()
    assert urls == [_url("freshvid"), _url("cachedvid")]


def test_channels_exits_1_when_a_listing_ends_before_the_window(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(stockyt_config, "CHANNELS", [("Short", "UCs")])
    monkeypatch.setattr(channels_mod, "list_channel_videos",
                        lambda channel_id, **kw: _listing("UCs", [("a", "20261107")], reached=False))
    assert main(["channels", "--since", "20261101", "--no-cache"]) == 1
    assert "❌ Short: listing ended before the window start" in capsys.readouterr().out


def test_channels_ok_writes_urls_and_json_and_exits_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(stockyt_config, "CHANNELS", [("Only", "UConly")])
    monkeypatch.setattr(channels_mod, "list_channel_videos",
                        lambda channel_id, **kw: _listing("UConly", [("a", "20261107"), ("b", "20261106")]))
    out_file, json_file = tmp_path / "urls.txt", tmp_path / "channels.json"
    assert main(["channels", "--since", "20261101", "--no-cache", "-o", str(out_file),
                 "--channels-json", str(json_file)]) == 0
    assert out_file.read_text().split() == [_url("a"), _url("b")]
    [entry] = json.loads(json_file.read_text())
    assert (entry["count"], entry["oldest_date"], entry["reached_cutoff"]) == (2, "20261106", True)
    assert "Channels: 1 channels · 2 videos · 0 failed [OK]" in capsys.readouterr().out


@pytest.fixture
def cutoffs(monkeypatch):
    """Record the cutoff and until each channel fetch receives; one channel, no network."""
    seen = []
    monkeypatch.setattr(stockyt_config, "CHANNELS", [("Only", "UConly")])

    def fake_list(channel_id, *, cutoff, until=None, video_time=None):
        seen.append((cutoff, until))
        return _listing(channel_id, [])

    monkeypatch.setattr(channels_mod, "list_channel_videos", fake_list)
    return seen


def test_channels_since_passes_that_day_as_cutoff(cutoffs, tmp_path):
    assert main(["channels", "--since", "20260616", "--no-cache"]) == 0
    assert cutoffs == [(Cutoff(date(2026, 6, 16)), None)]


def test_channels_since_with_a_time_reads_it_in_the_configured_timezone(cutoffs, settings_file):
    settings_file.write_text('pace_seconds = 0\ntimezone = "Asia/Tokyo"\n', encoding="utf-8")
    assert main(["channels", "--since", "20261105T1600", "--until", "20261107", "--no-cache"]) == 0
    [(cutoff, until)] = cutoffs
    assert cutoff.at == datetime(2026, 11, 5, 16, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert until == "20261107"


def test_channels_weeks_cutoff_is_n_weeks_before_today(cutoffs, tmp_path):
    assert main(["channels", "--weeks", "2", "--no-cache"]) == 0
    assert cutoffs == [(Cutoff(date.today() - timedelta(weeks=2)), None)]


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--weeks", "2", "--since", "20260616"], "not allowed with argument"),
        ([], "one of the arguments --weeks --since is required"),
        (["--since", "20261301"], "not a YYYYMMDD or YYYYMMDDTHHMM value: '20261301'"),
        (["--since", "2026-06-16"], "not a YYYYMMDD or YYYYMMDDTHHMM value"),
        (["--since", (date.today() + timedelta(days=1)).strftime("%Y%m%d")], "is in the future"),
        (["--since", "20260616", "--until", "2026-06-20"], "not a YYYYMMDD date"),
    ],
)
def test_channels_bad_window_is_an_error_before_any_fetch(cutoffs, tmp_path, capsys, argv, message):
    with pytest.raises(SystemExit) as exc:
        main(["channels", *argv, "--no-cache"])
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    assert cutoffs == []


def test_split_corpus_sizes_chunks_by_characters(tmp_path, capsys):
    corpus = tmp_path / "t.jsonl"
    corpus.write_text("".join(
        json.dumps({"video_id": v, "channel": "C", "status": "ok", "transcript": "x" * 10}) + "\n"
        for v in ("a", "b", "c", "a")), encoding="utf-8")
    out_dir = tmp_path / "chunks"
    assert main(["split-corpus", "--input", str(corpus), "--out-dir", str(out_dir), "--chunk-chars", "20"]) == 0
    assert sorted(p.name for p in out_dir.iterdir()) == ["authors.json", "chunk_01.jsonl", "chunk_02.jsonl"]
    assert "Kept 3 ok records (one per video)" in capsys.readouterr().out


# ── run / status / regrade / doctor ──

@pytest.fixture
def doctor_ok(monkeypatch):
    from agent_reach.stockyt import pipeline

    monkeypatch.setattr(pipeline, "doctor", lambda settings, backend: [(True, "fine")])


@pytest.mark.parametrize("argv, message", [
    (["run", "--resume", "x", "--since", "20261105"], "drop --since, --until and --label"),
    (["run", "--since", "20261105"], "needs --since and --label"),
    (["run", "--label", "x"], "needs --since and --label"),
    (["run", "--since", "20261105", "--label", "x", "--retry-failed"], "--retry-failed only goes with --resume"),
])
def test_run_rejects_a_bad_argument_mix(argv, message, capsys, doctor_ok):
    assert main(argv) == 1
    assert message in capsys.readouterr().err


def test_run_exits_1_on_a_doctor_fail_before_creating_the_run(tmp_path, monkeypatch, capsys):
    from agent_reach.stockyt import pipeline

    monkeypatch.setattr(pipeline, "doctor", lambda settings, backend: [(True, "a"), (False, "yt-dlp: missing")])
    assert main(["run", "--since", "20261105", "--label", "x", "--llm", "fake"]) == 1
    captured = capsys.readouterr()
    assert "[FAIL] yt-dlp: missing" in captured.out and "Fix the FAIL lines" in captured.err
    assert not (tmp_path / "out" / "runs" / "x").exists()


def test_run_refuses_an_existing_label_and_a_missing_resume(tmp_path, capsys, doctor_ok):
    from agent_reach.stockyt import run as run_mod

    run_mod.create(str(tmp_path / "out"), "x", {"since": "20261105", "llm": "fake"}, {})
    assert main(["run", "--since", "20261105", "--label", "x"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(["run", "--resume", "nope"]) == 1
    assert "no run 'nope'" in capsys.readouterr().err


def test_status_prints_each_stage_and_the_waiting_prompts(tmp_path, capsys):
    from agent_reach.stockyt import pipeline
    from agent_reach.stockyt import run as run_mod

    run = run_mod.create(str(tmp_path / "out"), "x", {"since": "20261105", "until": None, "llm": "session",
                                                      "cookies_from_browser": "chrome"}, {})
    run_mod.finish_stage(run, "channels", "done", ["Channels: 1 channels · 2 videos · 0 failed [OK]"], "h")
    run_mod.finish_stage(run, "extract", "waiting", ["Extract: 0/2 videos [FAIL]"], "h")
    prompt = os.path.join(pipeline.session_dir(run), "extract_01.prompt.md")
    run_mod.atomic_write_text(prompt, "p")

    assert main(["status", "x"]) == 0
    out = capsys.readouterr().out
    assert "Window: since 20261105 · LLM session · cookies chrome" in out
    assert "[channels] done · attempts 0\n  Channels: 1 channels · 2 videos · 0 failed [OK]" in out
    assert "[transcripts] pending · attempts 0" in out
    assert f"waiting: {prompt}" in out


def test_regrade_needs_a_finished_review(tmp_path, capsys):
    from agent_reach.stockyt import run as run_mod

    run_mod.create(str(tmp_path / "out"), "x", {"since": "20261105", "llm": "fake"}, {})
    assert main(["regrade", "x"]) == 1
    assert "no finished review yet" in capsys.readouterr().err


def test_doctor_exits_1_on_any_fail(monkeypatch, capsys):
    from agent_reach.stockyt import pipeline

    monkeypatch.setattr(pipeline, "doctor", lambda settings, backend: [(True, "a"), (None, "cache: none yet")])
    assert main(["doctor"]) == 0
    monkeypatch.setattr(pipeline, "doctor", lambda settings, backend: [(False, "JS runtime: none")])
    assert main(["doctor", "--llm", "session"]) == 1
    assert "[FAIL] JS runtime: none" in capsys.readouterr().out


def test_help_lists_run():
    assert "finance-YouTube pipeline (run, status, regrade, doctor;" in runtime_cli.HELP
