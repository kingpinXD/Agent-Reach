# -*- coding: utf-8 -*-
"""Tests for stockyt settings: TOML over code defaults, and the run.json snapshot."""

import json
import os

import pytest

from agent_reach.stockyt import config


def test_no_file_gives_the_defaults(tmp_path):
    settings = config.load_settings(str(tmp_path / "missing.toml"))
    assert settings == config.Settings()
    assert settings.channels == config.CHANNELS
    assert settings.timezone == "America/New_York"
    assert settings.whisper is False
    assert settings.llm_backend == "claude-cli" and settings.llm_model is None
    assert settings.weights.coverage_smoothing_k == 1.0


def test_file_values_win_over_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'output_dir = "~/stockyt-out"\n'
        'pace_seconds = 0.5\n'
        'cookies_from_browser = "chrome"\n'
        'channels = [["Only", "UConly"]]\n'
        '[weights]\n'
        'thesis = 0.6\n',
        encoding="utf-8",
    )
    settings = config.load_settings(str(path))
    assert settings.output_dir == os.path.expanduser("~/stockyt-out")
    assert settings.cache_dir == os.path.join(settings.output_dir, ".cache", "channels")
    assert settings.pace_seconds == 0.5
    assert settings.cookies_from_browser == "chrome"
    assert settings.channels == [("Only", "UConly")]
    assert settings.weights.thesis == 0.6
    assert settings.weights.catalyst == 0.20  # untouched weights keep their default
    assert settings.lang == "en"


@pytest.mark.parametrize("text, message", [
    ("pace_secs = 1\n", "unknown setting(s) pace_secs"),
    ("[weights]\nthesys = 1\n", "unknown setting(s) thesys"),
])
def test_a_misspelled_key_is_an_error(tmp_path, text, message):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        config.load_settings(str(path))


def test_snapshot_is_plain_json():
    snap = config.Settings().snapshot()
    assert json.loads(json.dumps(snap)) == {**snap, "channels": [list(c) for c in config.CHANNELS]}
    assert snap["weights"]["coverage_smoothing_k"] == 1.0


def test_channel_name_comes_from_the_settings_channels():
    assert config.Settings().channel_name("UCoMzWLaPjDJBbipihD694pQ") == "Stock Moe"
    india = config.apply_market(config.Settings(), "india")
    assert india.channel_name("UCB7GnQlJPIL6rBBqEoX87vA") == "SOIC"
    assert india.channel_name("UCoMzWLaPjDJBbipihD694pQ") is None
    assert config.Settings().channel_name(None) is None


def test_india_preset_sets_channels_timezone_and_caption_tracks():
    india = config.apply_market(config.Settings(pace_seconds=1.0), "india")
    assert india.market == "india"
    assert len(india.channels) == 9 and india.channels == config.INDIA_CHANNELS
    assert india.timezone == "Asia/Kolkata"
    assert india.caption_langs == ["en-orig", "hi-orig"]
    assert india.pace_seconds == 1.0  # everything else is the config's


def test_us_is_the_settings_unchanged_and_an_unknown_market_is_an_error():
    settings = config.Settings(channels=[("Only", "UConly")], timezone="Europe/London")
    assert config.apply_market(settings, "us") == settings
    assert settings.market == "us" and settings.caption_langs is None
    with pytest.raises(ValueError, match="unknown market 'uk'"):
        config.apply_market(settings, "uk")


def test_market_is_not_a_config_file_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('market = "india"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="--market"):
        config.load_settings(str(path))


def test_a_snapshot_from_before_markets_loads_as_us():
    snap = config.Settings().snapshot()
    del snap["market"], snap["caption_langs"]
    assert config.Settings.from_snapshot(snap) == config.Settings()
    india = config.apply_market(config.Settings(), "india")
    assert config.Settings.from_snapshot(json.loads(json.dumps(india.snapshot()))) == india
